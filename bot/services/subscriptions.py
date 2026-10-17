"""Subscriptions as seen through the subscription bot's API. Pure functions, no I/O.

The subscription bot is the source of truth: dates come from its responses, never recomputed
here. A charge is done once it appears in /payments (key: subscription_id + charge_date).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from bot.services.money import RUB, round_minor, to_rub_minor

log = logging.getLogger(__name__)

RateLookup = Callable[[str, date], Decimal | None]


@dataclass(frozen=True)
class SubInfo:
    id: int
    name: str
    amount_minor: int
    currency: str
    next_charge_date: date
    status: str
    auto_renew: bool
    period_unit: str = "month"  # day / week / month / year
    period_count: int = 1


@dataclass(frozen=True)
class UpcomingCharge:
    subscription_id: int
    name: str
    charge_date: date
    amount_minor: int
    currency: str
    auto_renew: bool


@dataclass(frozen=True)
class SubPayment:
    subscription_id: int
    charge_date: date
    amount_minor: int
    currency: str


def _parse_list(payload, build, what: str) -> list:
    if not isinstance(payload, list):
        return []
    result = []
    for item in payload:
        try:
            result.append(build(item))
        except (KeyError, TypeError, ValueError):
            # One malformed record must not hide all the others
            log.warning("Skipping malformed %s: %r", what, item)
    return result


def parse_subscriptions(payload) -> list[SubInfo]:
    return _parse_list(
        payload,
        lambda s: SubInfo(
            id=int(s["id"]),
            name=str(s["name"]),
            amount_minor=int(s["amount_minor"]),
            currency=str(s["currency"]).upper(),
            next_charge_date=date.fromisoformat(s["next_charge_date"]),
            # Unknown future statuses (e.g. trial) are kept as is and simply not counted
            status=str(s.get("status") or ""),
            auto_renew=bool(s.get("auto_renew", True)),
            period_unit=str(s.get("period_unit") or "month"),
            period_count=max(int(s.get("period_count") or 1), 1),
        ),
        "subscription",
    )


def parse_upcoming(payload) -> list[UpcomingCharge]:
    return _parse_list(
        payload,
        lambda u: UpcomingCharge(
            subscription_id=int(u["subscription_id"]),
            name=str(u["name"]),
            charge_date=date.fromisoformat(u["charge_date"]),
            amount_minor=int(u["amount_minor"]),
            currency=str(u["currency"]).upper(),
            auto_renew=bool(u.get("auto_renew", True)),
        ),
        "upcoming charge",
    )


def parse_payments(payload) -> list[SubPayment]:
    return _parse_list(
        payload,
        lambda p: SubPayment(
            subscription_id=int(p["subscription_id"]),
            charge_date=date.fromisoformat(p["charge_date"]),
            amount_minor=int(p["amount_minor"]),
            currency=str(p["currency"]).upper(),
        ),
        "payment",
    )


def convert(amount_minor: int, currency: str, on: date, rates: RateLookup) -> int | None:
    if currency == RUB:
        return amount_minor
    rate = rates(currency, on)
    return None if rate is None else to_rub_minor(amount_minor, currency, rate)


@dataclass(frozen=True)
class SubCharge:
    subscription_id: int
    name: str
    charge_date: date
    amount_minor: int
    currency: str
    # None when there is no exchange rate at all yet
    rub_minor: int | None
    auto_renew: bool
    # Manual payment whose date has passed without a payment record
    awaiting: bool


def pending_charges(
    subs: list[SubInfo],
    upcoming: list[UpcomingCharge],
    payments: list[SubPayment],
    today: date,
    rates: RateLookup,
) -> list[SubCharge]:
    """Charges that still have to be paid: overdue manual ones plus the upcoming ones."""
    paid = {(p.subscription_id, p.charge_date) for p in payments}
    active = {s.id for s in subs if s.status == "active"}
    result = []
    for s in subs:
        key = (s.id, s.next_charge_date)
        if s.id in active and not s.auto_renew and s.next_charge_date < today and key not in paid:
            result.append(
                SubCharge(
                    s.id, s.name, s.next_charge_date, s.amount_minor, s.currency,
                    convert(s.amount_minor, s.currency, today, rates), False, awaiting=True,
                )
            )
    for u in upcoming:
        if (u.subscription_id, u.charge_date) in paid or u.charge_date < today:
            continue
        # Without the subscription list (never fetched) /upcoming is trusted as is
        if subs and u.subscription_id not in active:
            continue
        result.append(
            SubCharge(
                u.subscription_id, u.name, u.charge_date, u.amount_minor, u.currency,
                convert(u.amount_minor, u.currency, u.charge_date, rates), u.auto_renew,
                awaiting=False,
            )
        )
    return result


def paid_between(
    payments: list[SubPayment], start: date, end: date, rates: RateLookup
) -> tuple[int, list[SubPayment]]:
    """Total in kopecks of charges with start < charge_date <= end, and those charges."""
    chosen = [p for p in payments if start < p.charge_date <= end]
    total = 0
    for p in chosen:
        rub = convert(p.amount_minor, p.currency, p.charge_date, rates)
        total += rub or 0
    return total, chosen


# Average months in a period unit: 365.25 days a year
_MONTHS_PER_UNIT = {
    "day": Decimal("12") / Decimal("365.25"),
    "week": Decimal("12") / (Decimal("365.25") / 7),
    "month": Decimal(1),
    "year": Decimal(12),
}


@dataclass(frozen=True)
class MonthlyCost:
    sub: SubInfo
    # Kopecks a month; None without an exchange rate or with an unknown period unit
    rub_minor: int | None


def monthly_costs(subs: list[SubInfo], today: date, rates: RateLookup) -> list[MonthlyCost]:
    """What each active subscription costs a month on average, most expensive first."""
    result = []
    for s in subs:
        if s.status != "active":
            continue
        months = _MONTHS_PER_UNIT.get(s.period_unit)
        rub = convert(s.amount_minor, s.currency, today, rates)
        monthly = None
        if months is not None and rub is not None:
            monthly = round_minor(Decimal(rub) / (months * s.period_count))
        result.append(MonthlyCost(s, monthly))
    result.sort(key=lambda c: (c.rub_minor is None, -(c.rub_minor or 0), c.sub.id))
    return result
