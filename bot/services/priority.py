"""Which payment to keep first when money is short. Pure functions."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import IntEnum

from bot.services.loans import LoanKind
from bot.services.obligations import Obligation, Source, State


class Group(IntEnum):
    # Daily rates and collectors; a missed payment already accrues penalties
    URGENT = 1
    BANK = 2  # credit history, penalties
    CARD = 3  # at least the minimum payment
    INSTALLMENT = 4
    SUBSCRIPTION = 5  # can be cancelled or paused


@dataclass(frozen=True)
class LoanTerms:
    kind: LoanKind
    annual_rate: Decimal  # comparable yearly rate, see loans.effective_annual_rate


_KIND_GROUPS = {
    LoanKind.MFO: Group.URGENT,
    LoanKind.BANK: Group.BANK,
    LoanKind.CARD: Group.CARD,
    LoanKind.INSTALLMENT: Group.INSTALLMENT,
}


def group_of(o: Obligation, terms: dict[int, LoanTerms]) -> Group:
    if o.source == Source.SUB:
        return Group.SUBSCRIPTION
    if o.state == State.OVERDUE:
        return Group.URGENT
    t = terms.get(o.loan_id)
    return _KIND_GROUPS[t.kind] if t is not None else Group.BANK


def rank(obligations: list[Obligation], terms: dict[int, LoanTerms],
         today: date) -> list[Obligation]:
    """Most critical first: by group, then the costlier and the longer overdue."""

    def key(o: Obligation):
        t = terms.get(o.loan_id)
        rate = t.annual_rate if t is not None else Decimal(0)
        late = (today - o.date).days if o.is_late else 0
        return group_of(o, terms), -rate, -late, o.date, -(o.amount or 0)

    return sorted(obligations, key=key)


def cancel_candidates(obligations: list[Obligation]) -> list[Obligation]:
    """Subscriptions among the payments, most expensive first."""
    subs = [o for o in obligations if o.source == Source.SUB]
    return sorted(subs, key=lambda o: (o.amount is None, -(o.amount or 0), o.date))
