"""Shortfall forecast: will the money cover the payments of the next few days. Pure functions."""

from dataclasses import dataclass
from datetime import date, timedelta

from bot.services.obligations import Obligation

WINDOW_DAYS = 5


@dataclass(frozen=True)
class Shortfall:
    day: date  # first day the money runs out
    amount: int  # the deepest gap within the window, kopecks
    items: list[Obligation]  # payments from that day on: the ones at risk


def forecast(
    balance: int,
    obligations: list[Obligation],
    incomes: list[tuple[date, int]],
    today: date,
    days: int = WINDOW_DAYS,
) -> Shortfall | None:
    """Day by day: money + incomes − payments. Late payments are due today. Income comes
    before the same day's payments: salary usually arrives in the morning."""
    end = today + timedelta(days=days)
    money, lowest, first = balance, 0, None
    for offset in range(days):
        day = today + timedelta(days=offset)
        money += sum(amount for on, amount in incomes if on == day)
        money -= sum(o.amount or 0 for o in obligations if _due_on(o, today) == day)
        if money < 0 and first is None:
            first = day
        lowest = min(lowest, money)
    if first is None:
        return None
    items = [o for o in obligations if first <= _due_on(o, today) < end]
    return Shortfall(first, -lowest, items)


def _due_on(o: Obligation, today: date) -> date:
    return today if o.is_late else max(o.date, today)
