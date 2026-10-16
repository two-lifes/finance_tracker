"""Shift patterns (2/2, 5/2, ...) and paydays: fixed or hourly pay. Pure functions, no I/O."""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from bot.services.dates import add_months
from bot.services.money import round_minor

# An unconfirmed payday older than this is assumed to be forgotten, not delayed
PAYDAY_LOOKBACK_DAYS = 14
PAYDAY_HORIZON_DAYS = 70

_PATTERN_RE = re.compile(r"^\s*(\d{1,2})\s*[/xх×]\s*(\d{1,2})\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class WorkPattern:
    work: int
    off: int
    # First working day of some cycle
    anchor: date

    @property
    def title(self) -> str:
        return f"{self.work}/{self.off}"

    def is_work_day(self, d: date) -> bool:
        return (d - self.anchor).days % (self.work + self.off) < self.work

    def work_days(self, start: date, end: date) -> int:
        """Working days in [start, end)."""
        return sum(self.is_work_day(start + timedelta(days=i)) for i in range((end - start).days))


def parse_pattern(text: str) -> tuple[int, int] | None:
    """'2/2', '5/2', '1/3' -> (work, off)."""
    match = _PATTERN_RE.match(text)
    if not match:
        return None
    work, off = int(match.group(1)), int(match.group(2))
    if not (1 <= work <= 14 and 0 <= off <= 14):
        return None
    return work, off


def default_anchor(work: int, off: int, today: date) -> date:
    # A 7-day cycle is a regular week: start it on Monday so 5/2 means Mon-Fri
    if work + off == 7:
        return today - timedelta(days=today.weekday())
    return today


class Weekend(StrEnum):
    """What happens to a payday that falls on Saturday or Sunday."""

    BEFORE = "before"  # the Friday before (ТК РФ ст. 136)
    AFTER = "after"  # the Monday after
    SAME = "same"  # paid on the weekend anyway


@dataclass(frozen=True)
class IncomeRule:
    id: int
    title: str
    # Fixed pay: day of the payday. Hourly pay: last day of a pay period.
    # Clamped to the month's length (31 = the last day).
    day: int
    amount_minor: int  # fixed pay only, approximate


@dataclass(frozen=True)
class HourlyPay:
    rate_minor: int  # per hour
    shift_hours: Decimal
    tax_percent: Decimal  # withheld from the rate; 0 if the rate is take-home
    delay_days: int  # payday comes this many days after its period ends
    pattern: WorkPattern


@dataclass(frozen=True)
class PaySchedule:
    rules: list[IncomeRule] = field(default_factory=list)
    weekend: Weekend = Weekend.BEFORE
    # None: every rule is a fixed payday with its own amount
    hourly: HourlyPay | None = None


@dataclass(frozen=True)
class PaydayRecord:
    rule_id: int
    scheduled: date
    status: str  # expected (asked, no answer yet) / received / delayed
    delayed_until: date | None = None


@dataclass(frozen=True)
class Payout:
    """One scheduled payday and what it is expected to bring."""

    rule: IncomeRule
    scheduled: date
    amount: int
    # Hourly pay: the worked period it pays for and the shifts in it
    period: tuple[date, date] | None = None
    shifts: int | None = None

    @property
    def label(self) -> str:
        if self.period is None:
            return self.rule.title
        start, end = self.period
        return f"{self.rule.title} за {start:%d.%m}–{end:%d.%m}"


@dataclass(frozen=True)
class ExpectedPayday:
    payout: Payout
    # When the money is expected now: later than scheduled if delayed or not confirmed yet
    effective: date
    delayed: bool

    @property
    def rule(self) -> IncomeRule:
        return self.payout.rule

    @property
    def scheduled(self) -> date:
        return self.payout.scheduled


def shift_weekend(d: date, weekend: Weekend) -> date:
    """Public holidays are not known to the bot, only weekends."""
    step = {Weekend.BEFORE: -1, Weekend.AFTER: 1}.get(weekend)
    while step is not None and d.weekday() >= 5:
        d += timedelta(days=step)
    return d


def pay_date(year: int, month: int, day: int, weekend: Weekend = Weekend.BEFORE) -> date:
    return shift_weekend(add_months(date(year, month, 1), 0, day), weekend)


def _period_ends(cuts: list[int], start: date, end: date) -> list[date]:
    """Ends of pay periods (cut days clamped to month length) in [start, end]."""
    result = set()
    month = date(start.year, start.month, 1)
    while month <= end:
        for day in cuts:
            d = add_months(month, 0, day)
            if start <= d <= end:
                result.add(d)
        month = add_months(month, 1)
    return sorted(result)


def period_of(cuts: list[int], period_end: date) -> tuple[date, date]:
    """The pay period that ends on ``period_end``: it starts the day after the previous cut."""
    previous = _period_ends(cuts, period_end - timedelta(days=62), period_end - timedelta(days=1))
    if previous:
        return previous[-1] + timedelta(days=1), period_end
    return date(period_end.year, period_end.month, 1), period_end


def hourly_amount(hourly: HourlyPay, shifts: int) -> int:
    gross = Decimal(hourly.rate_minor) * hourly.shift_hours * shifts
    return round_minor(gross * (100 - hourly.tax_percent) / 100)


def _fixed_payouts(schedule: PaySchedule, start: date, end: date) -> list[Payout]:
    result = []
    for rule in schedule.rules:
        # A weekend shift can move a payday across a month boundary: look one month around
        month = add_months(date(start.year, start.month, 1), -1)
        while month <= end + timedelta(days=3):
            d = pay_date(month.year, month.month, rule.day, schedule.weekend)
            if start <= d <= end:
                result.append(Payout(rule, d, rule.amount_minor))
            month = add_months(month, 1)
    return result


def _hourly_payouts(
    schedule: PaySchedule, hourly: HourlyPay, start: date, end: date
) -> list[Payout]:
    cuts = sorted({r.day for r in schedule.rules})
    if not cuts:
        return []
    by_day = {}
    for rule in sorted(schedule.rules, key=lambda r: r.id):
        by_day.setdefault(rule.day, rule)
    result = []
    lead = timedelta(days=hourly.delay_days + 7)
    for period_end in _period_ends(cuts, start - lead, end):
        payday = shift_weekend(period_end + timedelta(days=hourly.delay_days), schedule.weekend)
        if not start <= payday <= end:
            continue
        # A clamped cut (31 -> 30 in November) still belongs to the rule with cut 31
        rule = by_day.get(period_end.day) or by_day[min(c for c in cuts if c > period_end.day)]
        period = period_of(cuts, period_end)
        shifts = hourly.pattern.work_days(period[0], period[1] + timedelta(days=1))
        result.append(Payout(rule, payday, hourly_amount(hourly, shifts), period, shifts))
    return result


def payouts(schedule: PaySchedule, start: date, end: date) -> list[Payout]:
    """Paydays in [start, end] with expected amounts."""
    if schedule.hourly is None:
        return _fixed_payouts(schedule, start, end)
    return _hourly_payouts(schedule, schedule.hourly, start, end)


def find_payout(schedule: PaySchedule, rule_id: int, scheduled: date) -> Payout | None:
    for payout in payouts(schedule, scheduled, scheduled):
        if payout.rule.id == rule_id:
            return payout
    return None


def expected_paydays(
    schedule: PaySchedule, records: list[PaydayRecord], today: date
) -> list[ExpectedPayday]:
    """Paydays still to come, soonest first. One not confirmed by its date is expected
    tomorrow at the earliest, so the limit never assumes money that hasn't arrived.
    Past paydays count only if the bot asked about them (a record exists)."""
    by_key = {(r.rule_id, r.scheduled): r for r in records}
    tomorrow = today + timedelta(days=1)
    start = today - timedelta(days=PAYDAY_LOOKBACK_DAYS)
    result = []
    for payout in payouts(schedule, start, today + timedelta(days=PAYDAY_HORIZON_DAYS)):
        d = payout.scheduled
        record = by_key.get((payout.rule.id, d))
        if record is not None and record.status == "received":
            continue
        # A past payday the bot never asked about (e.g. before the rule was added)
        if d < today and record is None:
            continue
        delayed = record is not None and record.status == "delayed"
        until = record.delayed_until if delayed else None
        if until is not None:
            effective = max(until, tomorrow)
        elif d > today:
            effective = d
        else:
            effective = tomorrow
        result.append(ExpectedPayday(payout, effective, delayed=delayed))
    result.sort(key=lambda p: (p.effective, p.scheduled, p.rule.id))
    return result


def next_payday(
    schedule: PaySchedule, records: list[PaydayRecord], today: date
) -> ExpectedPayday | None:
    upcoming = expected_paydays(schedule, records, today)
    return upcoming[0] if upcoming else None


def due_today(
    schedule: PaySchedule, records: list[PaydayRecord], today: date
) -> list[ExpectedPayday]:
    """Paydays scheduled for today or earlier that are still not confirmed."""
    return [p for p in expected_paydays(schedule, records, today) if p.scheduled <= today]


_CUTS_RE = re.compile(r"^[\d\s,;]+$")
MAX_PERIODS = 6


def parse_cuts(text: str) -> list[int] | None:
    """'10 20 31' -> [10, 20, 31]: the last day of each pay period."""
    if not _CUTS_RE.match(text.strip()):
        return None
    values = sorted({int(p) for p in re.split(r"[\s,;]+", text.strip()) if p})
    if not values or len(values) > MAX_PERIODS or not all(1 <= v <= 31 for v in values):
        return None
    return values


def describe_periods(cuts: list[int]) -> str:
    """[10, 20, 31] -> '1–10, 11–20, 21–31'; 31 means the month's last day."""
    parts = []
    for i, cut in enumerate(cuts):
        if i:
            parts.append(f"{cuts[i - 1] + 1}–{cut}")
        elif cuts[-1] >= 31:
            parts.append(f"1–{cut}")
        else:
            # The first period starts in the previous month after the last cut
            parts.append(f"{cuts[-1] + 1}–{cut}")
    return ", ".join(parts)
