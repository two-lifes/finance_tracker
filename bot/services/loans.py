"""Loan math: interest accrual, payment allocation, early repayment, penalties.

Amounts are kopecks (int); rates are Decimal percents (24.9 = 24.9 %).
Interest accrues on actual days: bank loans per ``rate/100 * days / days_in_year``,
МФО per ``rate/100 * days`` with a daily rate.
"""

import calendar
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from bot.services.dates import add_months
from bot.services.money import round_minor


class LoanKind(StrEnum):
    BANK = "bank"
    MFO = "mfo"
    CARD = "card"
    INSTALLMENT = "installment"


class RateUnit(StrEnum):
    YEAR = "year"
    DAY = "day"


class Schedule(StrEnum):
    MONTHLY = "monthly"  # every month on payment_day
    ONCE = "once"  # a single due date (typical МФО loan)


class EarlyMode(StrEnum):
    TERM = "term"  # same payment, fewer months
    PAYMENT = "payment"  # same term, smaller payment


KIND_TITLES = {
    LoanKind.BANK: "Банковский кредит",
    LoanKind.MFO: "МФО",
    LoanKind.CARD: "Кредитная карта",
    LoanKind.INSTALLMENT: "Рассрочка",
}

# Credit cards and installments are not accrued by the bot: card interest depends on the
# grace period and bank rules, installments are interest-free. The user updates their debt.
ACCRUING = {LoanKind.BANK, LoanKind.MFO}


def _days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def accrued_interest(
    principal: int, rate: Decimal, unit: RateUnit, start: date, end: date
) -> int:
    """Interest on ``principal`` for days in [start, end)."""
    if principal <= 0 or rate <= 0 or end <= start:
        return 0
    if unit == RateUnit.DAY:
        return round_minor(Decimal(principal) * rate / 100 * (end - start).days)
    # Days in a leap year are divided by 366, others by 365, as Russian banks do
    total = Decimal(0)
    cursor = start
    while cursor < end:
        year_end = min(date(cursor.year + 1, 1, 1), end)
        total += Decimal((year_end - cursor).days) / _days_in_year(cursor.year)
        cursor = year_end
    return round_minor(Decimal(principal) * rate / 100 * total)


@dataclass(frozen=True)
class Allocation:
    insurance: int
    interest: int
    principal: int
    # Money beyond the whole debt
    overpaid: int


def allocate(amount: int, accrued: int, principal: int, insurance: int = 0) -> Allocation:
    """Splits a payment: insurance/fees first (they don't reduce the debt), then accrued
    interest, then principal."""
    to_insurance = min(amount, insurance)
    rest = amount - to_insurance
    to_interest = min(rest, accrued)
    rest -= to_interest
    to_principal = min(rest, principal)
    return Allocation(to_insurance, to_interest, to_principal, rest - to_principal)


def monthly_rate(rate: Decimal, unit: RateUnit) -> Decimal:
    if unit == RateUnit.DAY:
        return rate / 100 * Decimal(365) / 12
    return rate / 100 / 12


def annuity_payment(principal: int, rate: Decimal, unit: RateUnit, months: int) -> int:
    if months <= 0:
        return principal
    r = monthly_rate(rate, unit)
    if r == 0:
        return -(-principal // months)
    k = r * (1 + r) ** months / ((1 + r) ** months - 1)
    return round_minor(Decimal(principal) * k)


def remaining_months(principal: int, rate: Decimal, unit: RateUnit, payment: int) -> int | None:
    """Months to repay with a fixed payment (interest part only); None if it never ends."""
    if principal <= 0:
        return 0
    if payment <= 0:
        return None
    r = float(monthly_rate(rate, unit))
    if r == 0:
        return -(-principal // payment)
    ratio = principal * r / payment
    if ratio >= 1:
        return None
    # The payment is rounded to kopecks, which leaves a few kopecks for an extra month;
    # those are paid with the last payment, not as one more month
    return math.ceil(-math.log(1 - ratio) / math.log(1 + r) - 0.01)


@dataclass(frozen=True)
class EarlyRepayment:
    principal_after: int
    payment_after: int
    months_before: int | None
    months_after: int | None


def early_repayment(
    principal: int,
    rate: Decimal,
    unit: RateUnit,
    payment: int,
    insurance: int,
    extra: int,
    mode: EarlyMode,
) -> EarlyRepayment:
    """Recalculates the schedule after ``extra`` goes to principal (accrued interest must
    already be settled). ``payment`` includes ``insurance``, which never changes."""
    base = payment - insurance
    months_before = remaining_months(principal, rate, unit, base)
    after = max(principal - extra, 0)
    if after == 0:
        return EarlyRepayment(0, 0, months_before, 0)
    if mode == EarlyMode.TERM or months_before is None:
        return EarlyRepayment(
            after, payment, months_before, remaining_months(after, rate, unit, base)
        )
    new_base = annuity_payment(after, rate, unit, months_before)
    return EarlyRepayment(after, new_base + insurance, months_before, months_before)


def penalty(overdue_amount: int, annual_rate: Decimal, days: int) -> int:
    """Пеня on the overdue amount for ``days`` days, at an annual contract rate."""
    if overdue_amount <= 0 or days <= 0 or annual_rate <= 0:
        return 0
    return round_minor(Decimal(overdue_amount) * annual_rate / 100 * days / 365)


def card_min_payment(debt: int, percent: Decimal, floor: int = 0) -> int:
    """Typical card rule: a percent of the debt, but not less than ``floor`` nor the debt."""
    value = -(-round_minor(Decimal(debt) * percent * 100) // 10000)
    return min(debt, max(value, floor))


def monthly_due_dates(payment_day: int, start: date, end: date) -> list[date]:
    """Due dates on ``payment_day`` (clamped to month length) in [start, end]."""
    result = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        d = add_months(cursor, 0, payment_day)
        if start <= d <= end:
            result.append(d)
        cursor = add_months(cursor, 1)
    return result


def overdue_days(due: date, today: date) -> int:
    return max((today - due).days, 0)


def effective_annual_rate(rate: Decimal, unit: RateUnit) -> Decimal:
    """Comparable yearly rate, to rank loans by cost (МФО 0.8 %/day -> 292 %)."""
    if unit == RateUnit.DAY:
        return rate * 365
    return rate


def last_due_before(day: int, before: date) -> date:
    """The most recent monthly due date strictly before ``before``."""
    d = add_months(before, 0, day)
    if d >= before:
        d = add_months(before, -1, day)
    return d


def next_due_on_or_after(day: int, after: date) -> date:
    d = add_months(after, 0, day)
    if d < after:
        d = add_months(after, 1, day)
    return d

