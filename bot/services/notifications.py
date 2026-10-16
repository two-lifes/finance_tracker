"""Decides which messages are due. DB only, no Telegram I/O.

Planning inserts ``pending`` rows, delivery (bot/scheduler.py) sends them and marks ``sent``.
The unique key keeps every reminder at-most-once across restarts.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BalanceSnapshot, Loan, LoanDue, Notification, Payday, User
from bot.services.budget import CARD, get_payday, income_rules, payday_records, rule_info
from bot.services.dates import local_today
from bot.services.work_calendar import due_today

KIND_LOAN = "loan_{}"  # days before the due date
KIND_BALANCE = "balance"
KIND_PAYDAY = "payday"
LOAN_OFFSETS = (3, 1)


def moment(day: date, at: time, tz: str) -> datetime:
    return datetime.combine(day, at, tzinfo=ZoneInfo(tz))


async def _ensure(
    session: AsyncSession, user: User, kind: str, ref: int, for_date: date, status: str,
    now: datetime,
) -> None:
    exists = await session.scalar(
        select(Notification.id).where(
            Notification.user_id == user.id,
            Notification.kind == kind,
            Notification.ref_id == ref,
            Notification.for_date == for_date,
        )
    )
    if exists is None:
        session.add(Notification(user_id=user.id, kind=kind, ref_id=ref, for_date=for_date,
                                 status=status, created_at=now))
        await session.flush()


async def _plan_loans(session: AsyncSession, user: User, today: date, now: datetime) -> None:
    tz = user.timezone
    rows = await session.execute(
        select(LoanDue)
        .join(Loan, Loan.id == LoanDue.loan_id)
        .where(
            Loan.user_id == user.id,
            Loan.status == "active",
            LoanDue.status == "expected",
            LoanDue.due_date > today,
            LoanDue.due_date <= today + timedelta(days=max(LOAN_OFFSETS)),
        )
    )
    for due in rows.scalars():
        for offset in LOAN_OFFSETS:
            day = due.due_date - timedelta(days=offset)
            kind = KIND_LOAN.format(offset)
            if day < today:
                # Missed while the bot was down, or the loan was added late: not useful now
                await _ensure(session, user, kind, due.id, due.due_date, "skipped", now)
            elif day == today and now >= moment(day, user.remind_time, tz):
                await _ensure(session, user, kind, due.id, due.due_date, "pending", now)


async def _plan_balance(session: AsyncSession, user: User, today: date, now: datetime) -> None:
    if now < moment(today, user.evening_time, user.timezone):
        return
    has_balance = await session.scalar(
        select(BalanceSnapshot.id).where(
            BalanceSnapshot.user_id == user.id,
            BalanceSnapshot.account == CARD,
            BalanceSnapshot.on_date == today,
        )
    )
    if has_balance is None:
        await _ensure(session, user, KIND_BALANCE, 0, today, "pending", now)


async def _plan_paydays(session: AsyncSession, user: User, today: date, now: datetime) -> None:
    if now < moment(today, user.remind_time, user.timezone):
        return
    rules = [rule_info(r) for r in await income_rules(session, user)]
    records = await payday_records(session, user)
    promised = {(r.rule_id, r.scheduled): r.delayed_until for r in records}
    for expected in due_today(rules, records, today):
        # Delayed: wait until the promised date. Otherwise ask every day until answered,
        # since the limit keeps expecting the money "tomorrow"
        until = promised.get((expected.rule.id, expected.scheduled))
        if expected.delayed and until is not None and until > today:
            continue
        payday = await get_payday(session, expected.rule.id, expected.scheduled)
        await _ensure(session, user, KIND_PAYDAY, payday.id, today, "pending", now)


async def plan(session: AsyncSession, now: datetime) -> None:
    users = await session.scalars(select(User).where(User.timezone.is_not(None)))
    for user in users:
        today = local_today(now, user.timezone)
        await _plan_loans(session, user, today, now)
        await _plan_balance(session, user, today, now)
        await _plan_paydays(session, user, today, now)


async def pending(session: AsyncSession) -> list[Notification]:
    rows = await session.scalars(
        select(Notification).where(Notification.status == "pending").order_by(Notification.id)
    )
    return list(rows)


async def is_still_relevant(session: AsyncSession, n: Notification, user: User,
                            today: date) -> bool:
    if n.kind.startswith("loan_"):
        due = await session.get(LoanDue, n.ref_id)
        if due is None or due.status != "expected" or due.due_date <= today:
            return False
        loan = await session.get(Loan, due.loan_id)
        return loan.status == "active"
    if n.kind == KIND_BALANCE:
        if n.for_date != today:
            return False
        has_balance = await session.scalar(
            select(BalanceSnapshot.id).where(
                BalanceSnapshot.user_id == user.id,
                BalanceSnapshot.account == CARD,
                BalanceSnapshot.on_date == today,
            )
        )
        return has_balance is None
    if n.kind == KIND_PAYDAY:
        payday = await session.get(Payday, n.ref_id)
        return payday is not None and payday.status != "received" and n.for_date == today
    return False
