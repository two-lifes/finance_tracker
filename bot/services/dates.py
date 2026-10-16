import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bot.services.plural import DAYS, count

MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def local_today(now_utc: datetime, tz: str) -> date:
    return now_utc.astimezone(ZoneInfo(tz)).date()


def fmt_date(d: date) -> str:
    return f"{d:%d.%m.%Y}"


def fmt_short(d: date) -> str:
    """'17.10, пт' — compact form for lists."""
    return f"{d:%d.%m}, {WEEKDAYS_SHORT[d.weekday()]}"


def fmt_words(d: date) -> str:
    return f"{d.day} {MONTHS_GENITIVE[d.month - 1]}"


def relative_days(target: date, today: date) -> str:
    delta = (target - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчера"
    if delta > 0:
        return f"через {count(delta, DAYS)}"
    return f"{count(-delta, DAYS)} назад"


def add_months(d: date, months: int, day: int | None = None) -> date:
    """Same day ``months`` later, clamped to the month's length (31.01 + 1 -> 28/29.02)."""
    index = d.year * 12 + d.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day or d.day, days_in_month(year, month)))


def days_in_month(year: int, month: int) -> int:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


_DATE_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?$")


def parse_date(text: str, today: date, future: bool = True) -> date | None:
    """DD.MM[.YY|.YYYY]. Without a year picks the nearest such date not in the past
    (``future``) or not in the future (otherwise)."""
    match = _DATE_RE.match(text.strip())
    if not match:
        return None
    day, month, year_text = int(match.group(1)), int(match.group(2)), match.group(3)
    try:
        if year_text is not None:
            year = int(year_text)
            return date(year + 2000 if year < 100 else year, month, day)
        parsed = date(today.year, month, day)
        if future and parsed < today:
            parsed = date(today.year + 1, month, day)
        elif not future and parsed > today:
            parsed = date(today.year - 1, month, day)
        return parsed
    except ValueError:
        return None


_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def parse_time(text: str) -> time | None:
    match = _TIME_RE.match(text.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)
