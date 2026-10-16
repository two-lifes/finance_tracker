"""Cached data from outside: the subscription bot's API and CBR exchange rates.

Network calls happen without holding the DB lock: ``fetch_*`` talks to the network,
``store_*`` writes the result. The bot always works from the cache.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.clock import as_utc
from bot.db.models import ApiCache, FxRate
from bot.integrations.cbr import CbrError, fetch_daily
from bot.integrations.subs_client import SubsApiError, SubsClient
from bot.services.dates import local_today
from bot.services.fx import pick_rate
from bot.services.money import RUB
from bot.services.subscriptions import (
    RateLookup,
    SubInfo,
    SubPayment,
    UpcomingCharge,
    parse_payments,
    parse_subscriptions,
    parse_upcoming,
)

log = logging.getLogger(__name__)

SUBS_KEY = "subs"
FX_KEY = "fx"
SYNC_INTERVAL = timedelta(hours=1)
# Covers the payday after next: the payday summary looks that far
UPCOMING_DAYS = 70
# Spending looks back at most this far for subscription charges
PAYMENTS_DAYS = 45


@dataclass(frozen=True)
class SubsPayload:
    subscriptions: list
    upcoming: list
    payments: list


async def fetch_subs(client: SubsClient, today: date) -> SubsPayload:
    return SubsPayload(
        await client.subscriptions(),
        await client.upcoming(today + timedelta(days=UPCOMING_DAYS)),
        await client.payments(today - timedelta(days=PAYMENTS_DAYS), today),
    )


async def _cache_row(session: AsyncSession, key: str) -> ApiCache:
    row = await session.get(ApiCache, key)
    if row is None:
        row = ApiCache(key=key, payload=None)
        session.add(row)
    return row


async def store_subs(session: AsyncSession, payload: SubsPayload, now: datetime) -> None:
    row = await _cache_row(session, SUBS_KEY)
    row.payload = {
        "subscriptions": payload.subscriptions,
        "upcoming": payload.upcoming,
        "payments": payload.payments,
    }
    row.fetched_at = now
    row.last_attempt_at = now
    row.last_error = None
    await session.flush()


async def store_error(session: AsyncSession, key: str, error: str, now: datetime) -> None:
    row = await _cache_row(session, key)
    row.last_attempt_at = now
    row.last_error = error[:500]
    await session.flush()


async def sync_subs(
    session: AsyncSession, client: SubsClient | None, today: date, now: datetime
) -> bool:
    """Fetch-and-store in one go, for handlers (they already hold the DB lock)."""
    if client is None:
        return False
    try:
        payload = await fetch_subs(client, today)
    except SubsApiError as exc:
        log.warning("Subscription API unavailable: %s", exc)
        await store_error(session, SUBS_KEY, str(exc), now)
        return False
    await store_subs(session, payload, now)
    return True


async def subs_sync_due(session: AsyncSession, now: datetime) -> bool:
    row = await session.get(ApiCache, SUBS_KEY)
    return row is None or row.last_attempt_at is None or (
        now - as_utc(row.last_attempt_at) >= SYNC_INTERVAL
    )


@dataclass(frozen=True)
class SubsData:
    subscriptions: list[SubInfo]
    upcoming: list[UpcomingCharge]
    payments: list[SubPayment]
    fetched_at: datetime | None
    last_error: str | None

    @property
    def currencies(self) -> set[str]:
        items = [*self.subscriptions, *self.upcoming, *self.payments]
        return {i.currency for i in items} - {RUB}


async def load_subs(session: AsyncSession) -> SubsData:
    row = await session.get(ApiCache, SUBS_KEY)
    payload = row.payload if row is not None and isinstance(row.payload, dict) else {}
    return SubsData(
        parse_subscriptions(payload.get("subscriptions")),
        parse_upcoming(payload.get("upcoming")),
        parse_payments(payload.get("payments")),
        as_utc(row.fetched_at) if row is not None and row.fetched_at else None,
        row.last_error if row is not None else None,
    )


async def store_rates(
    session: AsyncSession, on: date, rates: dict[str, Decimal], currencies: set[str],
    now: datetime,
) -> None:
    for code in currencies:
        rate = rates.get(code)
        if rate is None:
            continue
        exists = await session.scalar(
            select(FxRate).where(FxRate.currency == code, FxRate.rate_date == on)
        )
        if exists is None:
            session.add(FxRate(currency=code, rate_date=on, rate=str(rate)))
        else:
            exists.rate = str(rate)
    row = await _cache_row(session, FX_KEY)
    row.fetched_at = now
    row.last_attempt_at = now
    row.last_error = None
    await session.flush()


async def fx_refresh_due(
    session: AsyncSession, currencies: set[str], today: date, tz: str, now: datetime
) -> bool:
    """Once a day, or right away when a currency has no rate at all."""
    if not currencies:
        return False
    known = set(await session.scalars(select(FxRate.currency).distinct()))
    if currencies - known:
        row = await session.get(ApiCache, FX_KEY)
        # Still retry no more than every 10 minutes while CBR is down
        return row is None or row.last_attempt_at is None or (
            now - as_utc(row.last_attempt_at) >= timedelta(minutes=10)
        )
    row = await session.get(ApiCache, FX_KEY)
    if row is None or row.last_attempt_at is None:
        return True
    if now - as_utc(row.last_attempt_at) < timedelta(hours=1):
        return False
    return row.fetched_at is None or local_today(as_utc(row.fetched_at), tz) < today


async def refresh_rates(
    session: AsyncSession, currencies: set[str], today: date, now: datetime
) -> bool:
    try:
        on, rates = await fetch_daily(today)
    except CbrError as exc:
        log.warning("CBR rates unavailable: %s", exc)
        await store_error(session, FX_KEY, str(exc), now)
        return False
    await store_rates(session, on, rates, currencies, now)
    return True


async def rate_lookup(session: AsyncSession) -> RateLookup:
    history: dict[str, list[tuple[date, Decimal]]] = {}
    for row in await session.scalars(select(FxRate)):
        history.setdefault(row.currency, []).append((row.rate_date, Decimal(row.rate)))

    def lookup(currency: str, on: date) -> Decimal | None:
        return pick_rate(history.get(currency, []), on)

    return lookup
