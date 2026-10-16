"""Loans in the DB: schedule generation, payments, early repayment, auto-debits."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import Loan, LoanDue, LoanPayment, User
from bot.services.dates import add_months
from bot.services.loans import (
    ACCRUING,
    Allocation,
    EarlyMode,
    EarlyRepayment,
    LoanKind,
    RateUnit,
    Schedule,
    accrued_interest,
    allocate,
    early_repayment,
    monthly_due_dates,
    remaining_months,
)
from bot.services.obligations import DueInfo

DUES_HORIZON_DAYS = 62


@dataclass(frozen=True)
class LoanDraft:
    lender: str
    kind: LoanKind
    principal: int
    rate: Decimal
    rate_unit: RateUnit
    payment: int  # monthly payment, or the amount to return for a single-payment loan
    first_due: date
    pay_method: str  # auto / manual
    insurance: int = 0


def rate_of(loan: Loan) -> Decimal:
    return Decimal(loan.rate)


def accrues(loan: Loan) -> bool:
    return LoanKind(loan.kind) in ACCRUING


def interest_now(loan: Loan, on: date) -> int:
    """Interest owed on ``on``: carried over plus accrued since ``interest_from``."""
    if not accrues(loan):
        return 0
    return loan.unpaid_interest_minor + accrued_interest(
        loan.principal_minor, rate_of(loan), RateUnit(loan.rate_unit), loan.interest_from, on
    )


def debt_of(loan: Loan, on: date) -> int:
    if loan.status != "active":
        return 0
    return loan.principal_minor + interest_now(loan, on)


async def create_loan(
    session: AsyncSession, user: User, draft: LoanDraft, today: date, now: datetime
) -> Loan:
    once = draft.kind == LoanKind.MFO
    loan = Loan(
        user_id=user.id,
        lender=draft.lender,
        kind=draft.kind,
        principal_minor=draft.principal,
        rate=str(draft.rate),
        rate_unit=draft.rate_unit,
        schedule=Schedule.ONCE if once else Schedule.MONTHLY,
        payment_minor=draft.payment,
        insurance_minor=draft.insurance,
        payment_day=None if once else draft.first_due.day,
        first_due=draft.first_due,
        pay_method=draft.pay_method,
        unpaid_interest_minor=0,
        # Interest of the current period runs from the previous payment date
        interest_from=today if once else min(add_months(draft.first_due, -1), today),
        created_at=now,
    )
    if once and accrues(loan):
        # The amount to return is known; what's above principal + future interest
        # has already accrued before today
        future = accrued_interest(
            draft.principal, draft.rate, draft.rate_unit, today, draft.first_due
        )
        loan.unpaid_interest_minor = max(draft.payment - draft.principal - future, 0)
    session.add(loan)
    await session.flush()
    await ensure_dues(session, loan, today)
    return loan


async def _load_dues(session: AsyncSession, loan: Loan) -> list[LoanDue]:
    return list(
        await session.scalars(
            select(LoanDue).where(LoanDue.loan_id == loan.id).order_by(LoanDue.due_date)
        )
    )


def _max_unpaid_dues(loan: Loan) -> int | None:
    """How many more payments the debt needs; None if it can't be told."""
    kind = LoanKind(loan.kind)
    if kind == LoanKind.CARD:
        return None
    rate = rate_of(loan) if accrues(loan) else Decimal(0)
    base = loan.payment_minor - loan.insurance_minor
    return remaining_months(loan.principal_minor, rate, RateUnit(loan.rate_unit), base)


async def ensure_dues(session: AsyncSession, loan: Loan, today: date) -> None:
    """Creates scheduled payments up to ~2 months ahead, no more than the debt needs."""
    if loan.status != "active":
        return
    dues = await _load_dues(session, loan)
    existing = {d.due_date for d in dues}
    if loan.schedule == Schedule.ONCE:
        if loan.first_due not in existing:
            session.add(LoanDue(loan_id=loan.id, due_date=loan.first_due,
                                amount_minor=loan.payment_minor))
            await session.flush()
        return
    limit = _max_unpaid_dues(loan)
    unpaid = sum(1 for d in dues if d.status != "paid")
    start = max(loan.first_due, max(existing, default=loan.first_due))
    for d in monthly_due_dates(loan.payment_day, start, today + timedelta(days=DUES_HORIZON_DAYS)):
        if d in existing:
            continue
        if limit is not None and unpaid >= limit:
            break
        amount = loan.payment_minor
        if LoanKind(loan.kind) == LoanKind.CARD:
            amount = min(amount, loan.principal_minor)
        elif limit is not None and unpaid == limit - 1 and not accrues(loan):
            # The last installment is whatever is left
            already = sum(x.amount_minor - x.paid_minor for x in dues if x.status != "paid")
            amount = max(loan.principal_minor - already, 0) + loan.insurance_minor
        if amount <= 0:
            break
        session.add(LoanDue(loan_id=loan.id, due_date=d, amount_minor=amount))
        unpaid += 1
    await session.flush()


async def regenerate_dues(session: AsyncSession, loan: Loan, today: date) -> None:
    """After the terms changed: future untouched payments are rebuilt from the new terms."""
    for due in await _load_dues(session, loan):
        if due.status == "expected" and due.paid_minor == 0 and due.due_date >= today:
            await session.delete(due)
    await session.flush()
    if loan.schedule == Schedule.MONTHLY:
        loan.first_due = max(loan.first_due, today)
    await ensure_dues(session, loan, today)


def _apply(loan: Loan, amount: int, on: date, insurance: int) -> Allocation:
    accrued = interest_now(loan, on)
    alloc = allocate(amount, accrued, loan.principal_minor, insurance)
    loan.principal_minor -= alloc.principal
    if accrues(loan):
        loan.unpaid_interest_minor = accrued - alloc.interest
        loan.interest_from = max(loan.interest_from, on)
    return alloc


async def _close_if_repaid(session: AsyncSession, loan: Loan, now: datetime) -> bool:
    if loan.principal_minor > 0 or loan.unpaid_interest_minor > 0:
        return False
    await close_loan(session, loan, now)
    return True


async def close_loan(session: AsyncSession, loan: Loan, now: datetime) -> None:
    loan.status = "closed"
    loan.closed_at = now
    for due in await _load_dues(session, loan):
        if due.status != "paid":
            await session.delete(due)
    await session.flush()


@dataclass(frozen=True)
class PaymentResult:
    allocation: Allocation
    closed: bool


async def pay_due(
    session: AsyncSession,
    loan: Loan,
    due: LoanDue,
    amount: int,
    paid_on: date,
    source: str,
    now: datetime,
) -> PaymentResult:
    """A regular (possibly partial) payment of ``due``."""
    insurance = loan.insurance_minor if due.paid_minor == 0 else 0
    alloc = _apply(loan, amount, paid_on, insurance)
    due.paid_minor += amount
    if due.paid_minor >= due.amount_minor:
        due.status = "paid"
    session.add(
        LoanPayment(
            loan_id=loan.id, due_id=due.id, paid_on=paid_on, amount_minor=amount,
            kind="regular", source=source, interest_minor=alloc.interest,
            principal_minor=alloc.principal, insurance_minor=alloc.insurance, created_at=now,
        )
    )
    await session.flush()
    closed = await _close_if_repaid(session, loan, now)
    if not closed and due.status == "paid":
        await ensure_dues(session, loan, paid_on)
    return PaymentResult(alloc, closed)


@dataclass(frozen=True)
class EarlyResult:
    allocation: Allocation
    schedule: EarlyRepayment | None
    closed: bool


async def early_repay(
    session: AsyncSession,
    loan: Loan,
    amount: int,
    mode: EarlyMode,
    paid_on: date,
    now: datetime,
) -> EarlyResult:
    """Extra money to a loan: interest first, the rest reduces principal; then the
    schedule is recalculated by reducing either the term or the payment."""
    principal_before = loan.principal_minor
    alloc = _apply(loan, amount, paid_on, insurance=0)
    session.add(
        LoanPayment(
            loan_id=loan.id, paid_on=paid_on, amount_minor=amount, kind="early",
            source="manual", interest_minor=alloc.interest, principal_minor=alloc.principal,
            created_at=now,
        )
    )
    await session.flush()
    if await _close_if_repaid(session, loan, now):
        return EarlyResult(alloc, None, True)
    schedule = None
    if loan.schedule == Schedule.MONTHLY and LoanKind(loan.kind) != LoanKind.CARD:
        rate = rate_of(loan) if accrues(loan) else Decimal(0)
        schedule = early_repayment(
            principal_before, rate, RateUnit(loan.rate_unit), loan.payment_minor,
            loan.insurance_minor, alloc.principal, mode,
        )
        loan.payment_minor = schedule.payment_after
    await regenerate_dues(session, loan, paid_on + timedelta(days=1))
    return EarlyResult(alloc, schedule, False)


async def settle_auto_debits(
    session: AsyncSession, user: User, up_to: date, now: datetime
) -> list[tuple[Loan, LoanDue]]:
    """Auto-debits due by ``up_to`` are assumed done (the balance already reflects them).
    The user can undo one with "не списалось"."""
    rows = await session.execute(
        select(Loan, LoanDue)
        .join(LoanDue, LoanDue.loan_id == Loan.id)
        .where(
            Loan.user_id == user.id,
            Loan.status == "active",
            Loan.pay_method == "auto",
            LoanDue.status == "expected",
            LoanDue.due_date <= up_to,
        )
        .order_by(LoanDue.due_date)
    )
    settled = []
    for loan, due in rows.all():
        if loan.status != "active":
            continue  # closed by an earlier due in this loop
        remaining = due.amount_minor - due.paid_minor
        await pay_due(session, loan, due, remaining, due.due_date, "auto", now)
        settled.append((loan, due))
    return settled


async def undo_auto_debit(session: AsyncSession, due: LoanDue) -> bool:
    """'Не списалось': reverts the assumed payment; the due becomes awaiting payment."""
    payment = await session.scalar(
        select(LoanPayment).where(LoanPayment.due_id == due.id, LoanPayment.source == "auto")
    )
    if payment is None:
        return False
    loan = await session.get(Loan, due.loan_id)
    loan.principal_minor += payment.principal_minor
    if accrues(loan):
        loan.unpaid_interest_minor += payment.interest_minor
    if loan.status == "closed":
        loan.status = "active"
        loan.closed_at = None
    due.paid_minor -= payment.amount_minor
    due.status = "not_debited"
    await session.delete(payment)
    await session.flush()
    return True


async def active_loans(session: AsyncSession, user: User) -> list[Loan]:
    return list(
        await session.scalars(
            select(Loan)
            .where(Loan.user_id == user.id, Loan.status == "active")
            .options(selectinload(Loan.dues))
            .order_by(Loan.id)
        )
    )


def due_infos(loans: list[Loan]) -> list[DueInfo]:
    result = []
    for loan in loans:
        penalty_rate = Decimal(loan.penalty_rate) if loan.penalty_rate else None
        for d in loan.dues:
            if d.status == "paid":
                continue
            result.append(
                DueInfo(
                    d.id, loan.id, loan.lender, d.due_date, d.amount_minor, d.paid_minor,
                    d.status, loan.pay_method == "auto", penalty_rate,
                )
            )
    return result


def next_due(loan: Loan) -> LoanDue | None:
    unpaid = [d for d in loan.dues if d.status != "paid"]
    return min(unpaid, key=lambda d: d.due_date) if unpaid else None
