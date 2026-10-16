import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_OFFSET_RE = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})$", re.IGNORECASE)


def parse_timezone(text: str) -> str | None:
    """Accepts an IANA name (Europe/Berlin) or a whole-hour offset (UTC+3, +3).

    Returns a valid IANA key or None.
    """
    text = text.strip()
    match = _OFFSET_RE.match(text)
    if match:
        sign, hours = match.group(1), int(match.group(2))
        if hours > 14:
            return None
        if hours == 0:
            return "UTC"
        # Etc/GMT zones use the inverted POSIX sign: UTC+3 is Etc/GMT-3
        return f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"
    if not text or ".." in text:
        return None
    try:
        return ZoneInfo(text).key
    except (ZoneInfoNotFoundError, ValueError):
        return None
