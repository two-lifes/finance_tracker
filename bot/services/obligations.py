"""One list of mandatory payments: loan dues and subscription charges. Pure functions."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from bot.services.loans import overdue_days, penalty
from bot.services.subscriptions import SubCharge


class State(StrEnum):
    UPCOMING = "upcoming"
    AWAITING = "awaiting"  # date passed, not paid yet (manual subscription, failed auto-debit)
    OVERDUE = "overdue"  # loan payment missed: penalties may accrue


class Source(StrEnum):
    LOAN = "loan"
    SUB = "sub"


@dataclass(frozen=True)
class DueInfo:
    """A scheduled loan payment, detached from the DB."""

    id: int
    loan_id: int
    title: str
    due_date: date
    amount: int
    paid: int
    status: str  # expected / paid / not_debited
    auto: bool
    penalty_rate: Decimal | None = None


@dataclass(frozen=True)
class Obligation:
    source: Source
    ref: int  # loan due id / subscription id
    title: str
    date: date
    # Kopecks; None when a foreign-currency charge has no exchange rate yet
    amount: int | None
    state: State
    auto: bool
    penalty: int = 0
    # Original amount for foreign-currency subscriptions
    original: tuple[int, str] | None = None

    @property
    def is_late(self) -> bool:
        return self.state != State.UPCOMING


def loan_obligations(dues: list[DueInfo], today: date) -> list[Obligation]:
    result = []
    for d in dues:
        remaining = d.amount - d.paid
        if d.status == "paid" or remaining <= 0:
            continue
        if d.status == "not_debited":
            state = State.AWAITING
        elif not d.auto and d.due_date < today:
            state = State.OVERDUE
        else:
            # An auto-debit is settled when the next balance arrives, so until then it is
            # still expected even if its date has passed
            state = State.UPCOMING
        fine = 0
        if state != State.UPCOMING and d.penalty_rate:
            fine = penalty(remaining, d.penalty_rate, overdue_days(d.due_date, today))
        result.append(
            Obligation(
                Source.LOAN, d.id, d.title, d.due_date, remaining + fine, state, d.auto, fine
            )
        )
    return result


def sub_obligations(charges: list[SubCharge]) -> list[Obligation]:
    return [
        Obligation(
            Source.SUB,
            c.subscription_id,
            c.name,
            c.charge_date,
            c.rub_minor,
            State.AWAITING if c.awaiting else State.UPCOMING,
            c.auto_renew,
            original=None if c.currency == "RUB" else (c.amount_minor, c.currency),
        )
        for c in charges
    ]


def merge(*groups: list[Obligation]) -> list[Obligation]:
    """Late ones first (they need attention), then by date."""
    items = [o for g in groups for o in g]
    items.sort(key=lambda o: (not o.is_late, o.date, o.source, o.ref))
    return items


def before(obligations: list[Obligation], horizon: date) -> list[Obligation]:
    """What must be paid before ``horizon``: every late item plus upcoming ones before it."""
    return [o for o in obligations if o.is_late or o.date < horizon]


def total(obligations: list[Obligation]) -> tuple[int, bool]:
    """Sum in kopecks, and whether some amounts were unknown (no exchange rate)."""
    known = [o.amount for o in obligations if o.amount is not None]
    return sum(known), len(known) != len(obligations)
