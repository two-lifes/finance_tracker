"""Background loop: reminders, payday questions, subscription sync, exchange rates."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.clock import utcnow
from bot.db.models import Loan, LoanDue, Notification, Payday, User
from bot.integrations.cbr import CbrError, fetch_daily
from bot.integrations.subs_client import SubsApiError, SubsClient
from bot.keyboards import due_pay_keyboard, payday_keyboard, shortfall_keyboard
from bot.services import external, notifications
from bot.services.budget import build_view, payout_of, record_debt_history
from bot.services.dates import local_today
from bot.services.deployments import pending_deployments
from bot.views import (
    BALANCE_REMINDER,
    deployment_text,
    loan_reminder,
    payday_question,
    shortfall_notice,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 60
_RETRYABLE = (TelegramNetworkError, TelegramRetryAfter, TelegramServerError)


async def _render(
    session: AsyncSession, n: Notification, user: User, today
) -> tuple[str, InlineKeyboardMarkup | None]:
    if n.kind.startswith("loan_"):
        due = await session.get(LoanDue, n.ref_id)
        loan = await session.get(Loan, due.loan_id)
        markup = due_pay_keyboard(due) if loan.pay_method == "manual" else None
        return loan_reminder(loan, due, today), markup
    if n.kind == notifications.KIND_PAYDAY:
        payday = await session.get(Payday, n.ref_id)
        await session.refresh(payday, ["rule"])
        payout = await payout_of(session, user, payday)
        return (payday_question(payout, user, today),
                payday_keyboard(payday.id, payout.amount))
    if n.kind == notifications.KIND_SHORTFALL:
        view = await build_view(session, user, today)
        return shortfall_notice(view.shortfall, today), shortfall_keyboard()
    return BALANCE_REMINDER, None


async def deliver(session_factory: async_sessionmaker, bot: Bot, now: datetime) -> None:
    async with session_factory() as session:
        for n in await notifications.pending(session):
            user = await session.get(User, n.user_id)
            today = local_today(now, user.timezone or "Europe/Moscow")
            try:
                if not await notifications.is_still_relevant(session, n, user, today):
                    n.status = "skipped"
                    await session.commit()
                    continue
                text, markup = await _render(session, n, user, today)
            except Exception:
                # A bug in one notification must not block the rest of the queue forever
                log.exception("Cannot render notification %s; dropping it", n.id)
                await session.rollback()
                n.status = "skipped"
                await session.commit()
                continue
            try:
                await bot.send_message(user.tg_id, text, reply_markup=markup)
            except (TelegramForbiddenError, TelegramBadRequest):
                log.exception("Dropping notification %s", n.id)
                n.status = "skipped"
            except _RETRYABLE as exc:
                log.warning("Telegram unavailable (%s); will retry", exc)
                await session.rollback()
                return
            else:
                n.status = "sent"
                n.sent_at = now
            await session.commit()


async def deliver_deployments(session_factory: async_sessionmaker, bot: Bot, now: datetime) -> None:
    async with session_factory() as session:
        deployments = await pending_deployments(session)
        if not deployments:
            return
        users = list(await session.scalars(select(User)))
        if not users:
            return  # a fresh DB has no user until /start
        for d in deployments:
            try:
                for user in users:
                    await bot.send_message(user.tg_id, deployment_text(d))
            except (TelegramForbiddenError, TelegramBadRequest):
                log.exception("Dropping deployment notice %s", d.id)
            except _RETRYABLE as exc:
                log.warning("Telegram unavailable (%s); will retry", exc)
                return
            d.notified_at = now
            await session.commit()


async def sync_external(
    session_factory: async_sessionmaker,
    subs_client: SubsClient | None,
    now: datetime,
    db_lock: asyncio.Lock,
) -> None:
    """Network first, without the DB lock, so a slow API never blocks the bot."""
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.timezone.is_not(None)))
        if user is None:
            return
        tz = user.timezone
        today = local_today(now, tz)
        subs_due = subs_client is not None and await external.subs_sync_due(session, now)
    if subs_due:
        try:
            payload, error = await external.fetch_subs(subs_client, today), None
        except SubsApiError as exc:
            payload, error = None, str(exc)
            log.warning("Subscription API unavailable: %s", exc)
        async with db_lock, session_factory() as session, session.begin():
            if payload is not None:
                await external.store_subs(session, payload, now)
            else:
                await external.store_error(session, external.SUBS_KEY, error, now)

    async with session_factory() as session:
        currencies = (await external.load_subs(session)).currencies
        fx_due = await external.fx_refresh_due(session, currencies, today, tz, now)
    if fx_due:
        try:
            on, rates = await fetch_daily(today)
        except CbrError as exc:
            log.warning("CBR rates unavailable: %s", exc)
            async with db_lock, session_factory() as session, session.begin():
                await external.store_error(session, external.FX_KEY, str(exc), now)
            return
        async with db_lock, session_factory() as session, session.begin():
            await external.store_rates(session, on, rates, currencies, now)


async def tick(
    session_factory: async_sessionmaker,
    bot: Bot,
    now: datetime,
    db_lock: asyncio.Lock,
    subs_client: SubsClient | None = None,
) -> None:
    await sync_external(session_factory, subs_client, now, db_lock)
    # Same lock as update handlers: no lost updates or "database is locked" between them
    async with db_lock:
        async with session_factory() as session, session.begin():
            await notifications.plan(session, now)
            for user in await session.scalars(select(User).where(User.timezone.is_not(None))):
                await record_debt_history(session, user, local_today(now, user.timezone))
        await deliver(session_factory, bot, now)
        await deliver_deployments(session_factory, bot, now)


async def run_scheduler(
    session_factory: async_sessionmaker,
    bot: Bot,
    db_lock: asyncio.Lock,
    subs_client: SubsClient | None,
    clock: Callable[[], datetime] = utcnow,
    interval: float = TICK_SECONDS,
) -> None:
    while True:
        try:
            await tick(session_factory, bot, clock(), db_lock, subs_client)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad tick must not stop reminders for good
            log.exception("Scheduler tick failed")
        await asyncio.sleep(interval)
