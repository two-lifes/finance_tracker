"""Central Bank of Russia exchange rates: parsing and picking a rate for a date."""

import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


class FxParseError(ValueError):
    pass


def parse_cbr_daily(content: bytes) -> tuple[date, dict[str, Decimal]]:
    """Parses XML_daily.asp: returns the rate date and RUB per one unit of each currency."""
    try:
        root = ET.fromstring(content)
        on = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    except (ET.ParseError, KeyError, ValueError) as exc:
        raise FxParseError(f"unexpected CBR response: {exc}") from exc
    rates = {}
    for valute in root.iter("Valute"):
        code = (valute.findtext("CharCode") or "").strip().upper()
        try:
            unit_rate = valute.findtext("VunitRate")
            if unit_rate:
                rate = Decimal(unit_rate.replace(",", "."))
            else:
                value = Decimal((valute.findtext("Value") or "").replace(",", "."))
                rate = value / Decimal(valute.findtext("Nominal") or "1")
        except (InvalidOperation, ZeroDivisionError):
            continue
        if len(code) == 3 and rate > 0:
            rates[code] = rate
    if not rates:
        raise FxParseError("CBR response has no rates")
    return on, rates


def pick_rate(history: list[tuple[date, Decimal]], on: date) -> Decimal | None:
    """The latest rate set on or before ``on``; the earliest known one for older dates."""
    if not history:
        return None
    ordered = sorted(history)
    candidates = [rate for d, rate in ordered if d <= on]
    return candidates[-1] if candidates else ordered[0][1]
