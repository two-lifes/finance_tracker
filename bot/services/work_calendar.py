"""Shift patterns (2/2, 5/2, ...) and paydays. Pure functions, no I/O."""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from bot.services.dates import add_months

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


@dataclass(frozen=True)
class IncomeRule:
    id: int
    title: str
    day: int  # day of month, clamped to the month's length
    amount_minor: int  # approximate


@dataclass(frozen=True)
class PaydayRecord:
    rule_id: int
    scheduled: date
    status: str  # expected (asked, no answer yet) / received / delayed
    delayed_until: date | None = None


@dataclass(frozen=True)
class ExpectedPayday:
    rule: IncomeRule
    scheduled: date
    # When the money is expected now: later than scheduled if delayed or not confirmed yet
    effective: date
    delayed: bool


def pay_date(year: int, month: int, day: int) -> date:
    """Payday in a month. A weekend payday moves to the Friday before (ТК РФ ст. 136).
    Public holidays are not known to the bot."""
    d = add_months(date(year, month, 1), 0, day)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def scheduled_dates(rule: IncomeRule, start: date, end: date) -> list[date]:
    """Paydays of ``rule`` in [start, end]."""
    result = []
    month = date(start.year, start.month, 1)
    # A weekend shift can pull next month's payday back into this one, so look one month ahead
    while month <= end + timedelta(days=3):
        d = pay_date(month.year, month.month, rule.day)
        if start <= d <= end:
            result.append(d)
        month = add_months(month, 1)
    return result


def expected_paydays(
    rules: list[IncomeRule], records: list[PaydayRecord], today: date
) -> list[ExpectedPayday]:
    """Paydays still to come, soonest first. One not confirmed by its date is expected
    tomorrow at the earliest, so the limit never assumes money that hasn't arrived.
    Past paydays count only if the bot asked about them (a record exists)."""
    by_key = {(r.rule_id, r.scheduled): r for r in records}
    tomorrow = today + timedelta(days=1)
    result = []
    for rule in rules:
        start = today - timedelta(days=PAYDAY_LOOKBACK_DAYS)
        for d in scheduled_dates(rule, start, today + timedelta(days=PAYDAY_HORIZON_DAYS)):
            record = by_key.get((rule.id, d))
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
            result.append(ExpectedPayday(rule, d, effective, delayed=delayed))
    result.sort(key=lambda p: (p.effective, p.scheduled, p.rule.id))
    return result


def next_payday(
    rules: list[IncomeRule], records: list[PaydayRecord], today: date
) -> ExpectedPayday | None:
    upcoming = expected_paydays(rules, records, today)
    return upcoming[0] if upcoming else None


def due_today(
    rules: list[IncomeRule], records: list[PaydayRecord], today: date
) -> list[ExpectedPayday]:
    """Paydays scheduled for today or earlier that are still not confirmed."""
    return [p for p in expected_paydays(rules, records, today) if p.scheduled <= today]
