"""Puts it all together from the DB: balances, spending, paydays, the main screen."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.clock import as_utc
from bot.db.models import (
    BalanceSnapshot,
    DebtHistory,
    ExpenseNote,
    Income,
    IncomeRule,
    Loan,
    LoanDue,
    LoanPayment,
    Payday,
    User,
)
from bot.services import work_calendar as wc
from bot.services.external import SubsData, load_subs, rate_lookup
from bot.services.forecast import Shortfall, forecast
from bot.services.limit import Limit, compute_limit, default_horizon, spending
from bot.services.loan_ops import (
    active_loans,
    debt_of,
    due_infos,
    loan_progress,
    settle_auto_debits,
)
from bot.services.loans import LoanKind, RateUnit, Schedule, effective_annual_rate
from bot.services.obligations import Obligation, loan_obligations, merge, sub_obligations
from bot.services.priority import LoanTerms
from bot.services.strategy import Advice, Candidate, advise, free_money
from bot.services.subscriptions import SubPayment, paid_between, pending_charges

CARD = "card"
CASH = "cash"


# --- balances -----------------------------------------------------------------------------


async def latest_snapshot(
    session: AsyncSession, user: User, account: str, on_or_before: date | None = None
) -> BalanceSnapshot | None:
    query = select(BalanceSnapshot).where(
        BalanceSnapshot.user_id == user.id, BalanceSnapshot.account == account
    )
    if on_or_before is not None:
        query = query.where(BalanceSnapshot.on_date <= on_or_before)
    return await session.scalar(query.order_by(BalanceSnapshot.on_date.desc()).limit(1))


async def save_snapshot(
    session: AsyncSession, user: User, account: str, on: date, amount: int, now: datetime
) -> BalanceSnapshot:
    row = await session.scalar(
        select(BalanceSnapshot).where(
            BalanceSnapshot.user_id == user.id,
            BalanceSnapshot.account == account,
            BalanceSnapshot.on_date == on,
        )
    )
    if row is None:
        row = BalanceSnapshot(user_id=user.id, account=account, on_date=on)
        session.add(row)
    row.amount_minor = amount
    row.updated_at = now
    await session.flush()
    return row


async def _cash_on(session: AsyncSession, user: User, on: date) -> int:
    row = await latest_snapshot(session, user, CASH, on)
    return row.amount_minor if row is not None else 0


# --- spending -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SpendingReport:
    since: date
    days: int
    spent: int  # negative: more money arrived than recorded
    incomes: int
    loan_payments: int
    sub_payments: int
    sub_charges: list[SubPayment]


def _after(model_date, model_created, prev_date: date, prev_at: datetime, on: date):
    """Rows dated after the previous balance, or the same day but entered after it."""
    return or_(
        (model_date > prev_date) & (model_date <= on),
        (model_date == prev_date) & (model_created > prev_at),
    )


async def spending_report(
    session: AsyncSession, user: User, on: date, subs: SubsData | None = None
) -> SpendingReport | None:
    """Spending between the card balance of ``on`` and the previous one."""
    current = await latest_snapshot(session, user, CARD, on)
    if current is None or current.on_date != on:
        return None
    prev = await session.scalar(
        select(BalanceSnapshot)
        .where(
            BalanceSnapshot.user_id == user.id,
            BalanceSnapshot.account == CARD,
            BalanceSnapshot.on_date < on,
        )
        .order_by(BalanceSnapshot.on_date.desc())
        .limit(1)
    )
    if prev is None:
        return None
    prev_at = as_utc(prev.updated_at)
    incomes = await session.scalar(
        select(func.coalesce(func.sum(Income.amount_minor), 0)).where(
            Income.user_id == user.id,
            _after(Income.on_date, Income.created_at, prev.on_date, prev_at, on),
        )
    )
    # Auto-debits are dated by their due date; manual payments by when they were entered
    loan_paid = await session.scalar(
        select(func.coalesce(func.sum(LoanPayment.amount_minor), 0))
        .join(Loan, Loan.id == LoanPayment.loan_id)
        .where(
            Loan.user_id == user.id,
            or_(
                (LoanPayment.source == "auto")
                & (LoanPayment.paid_on > prev.on_date)
                & (LoanPayment.paid_on <= on),
                (LoanPayment.source == "manual")
                & _after(LoanPayment.paid_on, LoanPayment.created_at, prev.on_date, prev_at, on),
            ),
        )
    )
    if subs is None:
        subs = await load_subs(session)
    sub_total, sub_charges = paid_between(
        subs.payments, prev.on_date, on, await rate_lookup(session)
    )
    prev_total = prev.amount_minor + await _cash_on(session, user, prev.on_date)
    total = current.amount_minor + await _cash_on(session, user, on)
    return SpendingReport(
        since=prev.on_date,
        days=(on - prev.on_date).days,
        spent=spending(prev_total, total, incomes, loan_paid + sub_total),
        incomes=incomes,
        loan_payments=loan_paid,
        sub_payments=sub_total,
        sub_charges=sub_charges,
    )


@dataclass(frozen=True)
class BalanceResult:
    settled: list[tuple[Loan, LoanDue]]
    report: SpendingReport | None
    # Manual payments due by today that are not marked as paid
    unconfirmed: list[tuple[Loan, LoanDue]]


async def record_card_balance(
    session: AsyncSession, user: User, amount: int, today: date, now: datetime
) -> BalanceResult:
    await save_snapshot(session, user, CARD, today, amount, now)
    settled = await settle_auto_debits(session, user, today, now)
    report = await spending_report(session, user, today)
    rows = await session.execute(
        select(Loan, LoanDue)
        .join(LoanDue, LoanDue.loan_id == Loan.id)
        .where(
            Loan.user_id == user.id,
            Loan.status == "active",
            Loan.pay_method == "manual",
            LoanDue.status == "expected",
            LoanDue.due_date <= today,
        )
        .order_by(LoanDue.due_date)
    )
    return BalanceResult(settled, report, list(rows.all()))


# --- incomes and paydays ------------------------------------------------------------------


async def add_income(
    session: AsyncSession, user: User, amount: int, kind: str, comment: str | None,
    on: date, now: datetime,
) -> Income:
    income = Income(user_id=user.id, on_date=on, amount_minor=amount, kind=kind,
                    comment=comment, created_at=now)
    session.add(income)
    await session.flush()
    return income


async def add_expense_note(
    session: AsyncSession, user: User, amount: int, category: str, on: date, now: datetime
) -> None:
    session.add(ExpenseNote(user_id=user.id, on_date=on, amount_minor=amount,
                            category=category, created_at=now))
    await session.flush()


async def income_rules(session: AsyncSession, user: User) -> list[IncomeRule]:
    return list(
        await session.scalars(
            select(IncomeRule).where(IncomeRule.user_id == user.id).order_by(IncomeRule.day)
        )
    )


def rule_info(rule: IncomeRule) -> wc.IncomeRule:
    return wc.IncomeRule(rule.id, rule.title, rule.day, rule.amount_minor)


def work_pattern(user: User) -> wc.WorkPattern | None:
    if not user.shift_work or user.shift_anchor is None:
        return None
    return wc.WorkPattern(user.shift_work, user.shift_off or 0, user.shift_anchor)


def hourly_pay(user: User) -> wc.HourlyPay | None:
    """None unless hourly pay is fully set up (it needs the shift pattern too)."""
    pattern = work_pattern(user)
    if (pattern is None or user.hourly_rate_minor is None or user.shift_hours is None
            or user.pay_delay_days is None):
        return None
    return wc.HourlyPay(user.hourly_rate_minor, Decimal(user.shift_hours),
                        Decimal(user.tax_percent or "0"), user.pay_delay_days, pattern)


async def pay_schedule(session: AsyncSession, user: User) -> wc.PaySchedule:
    rules = [rule_info(r) for r in await income_rules(session, user)]
    weekend = wc.Weekend(user.weekend_shift or wc.Weekend.BEFORE)
    if user.income_mode == "hourly":
        hourly = hourly_pay(user)
        # Half-configured hourly pay must not be read as fixed paydays of 0 ₽
        return wc.PaySchedule(rules if hourly else [], weekend, hourly)
    return wc.PaySchedule(rules, weekend)


async def save_rules(
    session: AsyncSession, user: User, rules: list[tuple[int, int, str]]
) -> None:
    """Replaces income rules as (day, amount, title), reusing existing rows so paydays
    already confirmed keep their history."""
    existing = await income_rules(session, user)
    for row, (day, amount, title) in zip(existing, rules, strict=False):
        row.day, row.amount_minor, row.title = day, amount, title
    for row in existing[len(rules):]:
        await session.delete(row)
    for day, amount, title in rules[len(existing):]:
        session.add(IncomeRule(user_id=user.id, title=title, day=day, amount_minor=amount))
    await session.flush()


async def payday_records(session: AsyncSession, user: User) -> list[wc.PaydayRecord]:
    rows = await session.scalars(
        select(Payday).join(IncomeRule).where(IncomeRule.user_id == user.id)
    )
    return [wc.PaydayRecord(p.rule_id, p.scheduled, p.status, p.delayed_until) for p in rows]


async def get_payday(
    session: AsyncSession, rule_id: int, scheduled: date, create: bool = True
) -> Payday | None:
    payday = await session.scalar(
        select(Payday).where(Payday.rule_id == rule_id, Payday.scheduled == scheduled)
    )
    if payday is None and create:
        payday = Payday(rule_id=rule_id, scheduled=scheduled, status="expected")
        session.add(payday)
        await session.flush()
    return payday


async def upcoming_paydays(
    session: AsyncSession, user: User, today: date
) -> list[wc.ExpectedPayday]:
    schedule = await pay_schedule(session, user)
    return wc.expected_paydays(schedule, await payday_records(session, user), today)


async def payout_of(session: AsyncSession, user: User, payday: Payday) -> wc.Payout:
    """What a payday row is expected to bring; falls back to the rule's own amount if the
    settings changed since and the date no longer matches the schedule."""
    schedule = await pay_schedule(session, user)
    payout = wc.find_payout(schedule, payday.rule_id, payday.scheduled)
    if payout is None:
        payout = wc.Payout(rule_info(payday.rule), payday.scheduled, payday.rule.amount_minor)
    return payout


async def receive_payday(
    session: AsyncSession, user: User, payday: Payday, amount: int, today: date, now: datetime
) -> Income:
    income = await add_income(session, user, amount, "salary", payday.rule.title, today, now)
    payday.status = "received"
    payday.delayed_until = None
    payday.income_id = income.id
    await session.flush()
    return income


# --- the whole picture --------------------------------------------------------------------


@dataclass(frozen=True)
class View:
    today: date
    card: BalanceSnapshot | None
    cash: BalanceSnapshot | None
    obligations: list[Obligation]
    payday: wc.ExpectedPayday | None
    limit: Limit | None
    debt: int
    loans_count: int
    subs: SubsData
    shortfall: Shortfall | None
    # Early repayment suggestion; never while money is short
    advice: Advice | None
    closed_loans: int
    total_loans: int
    terms: dict[int, LoanTerms]

    @property
    def balance(self) -> int:
        return (self.card.amount_minor if self.card else 0) + (
            self.cash.amount_minor if self.cash else 0
        )


async def obligations_for(
    session: AsyncSession, user: User, today: date, subs: SubsData, loans: list[Loan]
) -> list[Obligation]:
    charges = pending_charges(
        subs.subscriptions, subs.upcoming, subs.payments, today, await rate_lookup(session)
    )
    return merge(loan_obligations(due_infos(loans), today), sub_obligations(charges))


def annual_rate(loan: Loan) -> Decimal:
    return effective_annual_rate(Decimal(loan.rate), RateUnit(loan.rate_unit))


def loan_terms(loans: list[Loan]) -> dict[int, LoanTerms]:
    return {loan.id: LoanTerms(LoanKind(loan.kind), annual_rate(loan)) for loan in loans}


def prepay_candidates(loans: list[Loan], today: date) -> list[Candidate]:
    return [Candidate(loan.id, loan.lender, annual_rate(loan), debt_of(loan, today))
            for loan in loans]


def expected_incomes(paydays: list[wc.ExpectedPayday]) -> list[tuple[date, int]]:
    """Paydays the forecast may count on: a delayed salary is not money yet."""
    return [(p.effective, p.payout.amount) for p in paydays if not p.delayed]


def advice_for(user: User, loans: list[Loan], spare: int, days: int,
               today: date) -> Advice | None:
    free = free_money(spare, 0, user.reserve_per_day_minor, days)
    return advise(free, user.prepay_threshold_minor, prepay_candidates(loans, today))


async def build_view(session: AsyncSession, user: User, today: date) -> View:
    loans = await active_loans(session, user)
    subs = await load_subs(session)
    obligations = await obligations_for(session, user, today, subs, loans)
    paydays = await upcoming_paydays(session, user, today)
    payday = paydays[0] if paydays else None
    card = await latest_snapshot(session, user, CARD)
    cash = await latest_snapshot(session, user, CASH)
    limit = shortfall = advice = None
    if card is not None:
        horizon = payday.effective if payday else default_horizon(today)
        balance = card.amount_minor + (cash.amount_minor if cash else 0)
        limit = compute_limit(balance, obligations, today, horizon)
        shortfall = forecast(balance, obligations, expected_incomes(paydays), today)
        if shortfall is None and not limit.shortfall:
            advice = advice_for(user, loans, limit.free, limit.days, today)
    penalties = sum(o.penalty for o in obligations)
    debt = sum(debt_of(loan, today) for loan in loans) + penalties
    closed, total = await loan_progress(session, user)
    return View(today, card, cash, obligations, payday, limit, debt, len(loans), subs,
                shortfall, advice, closed, total, loan_terms(loans))


# --- subscriptions budget -----------------------------------------------------------------


async def monthly_income(session: AsyncSession, user: User, today: date) -> int:
    """Average income a month from the pay settings (hourly pay: by the shift pattern)."""
    schedule = await pay_schedule(session, user)
    year = wc.payouts(schedule, today, today + timedelta(days=364))
    return sum(p.amount for p in year) // 12


def monthly_loan_payments(loans: list[Loan]) -> int:
    """Regular monthly payments; one-off loans (МФО) are not a monthly expense."""
    return sum(loan.payment_minor for loan in loans if loan.schedule == Schedule.MONTHLY)


async def record_debt_history(session: AsyncSession, user: User, today: date) -> None:
    exists = await session.scalar(
        select(DebtHistory.id).where(DebtHistory.user_id == user.id, DebtHistory.on_date == today)
    )
    if exists is not None:
        return
    loans = await active_loans(session, user)
    if not loans:
        return
    total = sum(debt_of(loan, today) for loan in loans)
    session.add(DebtHistory(user_id=user.id, on_date=today, total_minor=total))
    await session.flush()

