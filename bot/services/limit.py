"""'Можно тратить сегодня', daily spending and the payday plan. Pure functions."""

from dataclasses import dataclass
from datetime import date, timedelta

from bot.services.obligations import Obligation, before, total

# Without any income rules the limit is spread over this many days
DEFAULT_HORIZON_DAYS = 30


@dataclass(frozen=True)
class Limit:
    per_day: int  # whole rubles, in kopecks; 0 when money is short
    free: int  # balance minus mandatory payments; negative means a shortfall
    mandatory: int
    days: int
    horizon: date
    items: list[Obligation]
    unknown_amounts: bool

    @property
    def shortfall(self) -> int:
        return max(-self.free, 0)


def default_horizon(today: date) -> date:
    return today + timedelta(days=DEFAULT_HORIZON_DAYS)


def compute_limit(
    balance: int, obligations: list[Obligation], today: date, horizon: date
) -> Limit:
    """(balance − mandatory payments until ``horizon``) ÷ days until it, today included."""
    items = before(obligations, horizon)
    mandatory, unknown = total(items)
    days = max((horizon - today).days, 1)
    free = balance - mandatory
    # Rounded down to whole rubles: kopecks in a daily allowance are noise
    per_day = (free // days) // 100 * 100 if free > 0 else 0
    return Limit(per_day, free, mandatory, days, horizon, items, unknown)


def spending(prev_balance: int, balance: int, incomes: int, outflows: int) -> int:
    """What was spent between two balances: money that left without a known reason.
    Negative means more money arrived than was recorded."""
    return prev_balance + incomes - outflows - balance


@dataclass(frozen=True)
class PaydayPlan:
    received: int
    set_aside: int
    items: list[Obligation]
    unknown_amounts: bool
    next_horizon: date

    @property
    def free(self) -> int:
        return self.received - self.set_aside


def payday_plan(received: int, obligations: list[Obligation], next_horizon: date) -> PaydayPlan:
    """On payday: how much of the income to put aside for payments until the next payday."""
    items = before(obligations, next_horizon)
    amount, unknown = total(items)
    return PaydayPlan(received, amount, items, unknown, next_horizon)
