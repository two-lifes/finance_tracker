"""Where to put spare money: the avalanche strategy. Pure functions."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Candidate:
    loan_id: int
    title: str
    annual_rate: Decimal
    debt: int


@dataclass(frozen=True)
class Advice:
    free: int
    target: Candidate


def free_money(available: int, mandatory: int, reserve_per_day: int, days: int) -> int:
    """Money left after the payments and a reserve for living until the next payday."""
    return available - mandatory - reserve_per_day * days


def avalanche(candidates: list[Candidate]) -> Candidate | None:
    """The costliest loan; on a tie the smaller debt, which closes sooner.
    Interest-free loans gain nothing from early repayment."""
    paying = [c for c in candidates if c.annual_rate > 0 and c.debt > 0]
    if not paying:
        return None
    return min(paying, key=lambda c: (-c.annual_rate, c.debt, c.loan_id))


def advise(free: int, threshold: int, candidates: list[Candidate]) -> Advice | None:
    if free <= 0 or free < threshold:
        return None
    target = avalanche(candidates)
    return Advice(min(free, target.debt), target) if target is not None else None
