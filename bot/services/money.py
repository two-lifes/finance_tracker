"""Money in minor units (kopecks for RUB). Floats are never used for amounts."""

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

NBSP = " "
RUB = "RUB"

# ISO 4217 minor units; currencies not listed here have 2
_EXPONENTS = {
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0,
    "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
}
_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€"}

_MAX_RUB = Decimal(10) ** 10
_AMOUNT_RE = re.compile(r"^([+-])?(\d+(?:[.,]\d{1,2})?)\s*(к|k|тыс\.?)?$", re.IGNORECASE)


def exponent(currency: str) -> int:
    return _EXPONENTS.get(currency.upper(), 2)


def to_rub_minor(amount_minor: int, currency: str, rate: Decimal) -> int:
    """Converts minor units of ``currency`` to kopecks; ``rate`` is RUB per one major unit."""
    if currency.upper() == RUB:
        return amount_minor
    major = Decimal(amount_minor).scaleb(-exponent(currency))
    return round_minor(major * rate * 100)


def round_minor(value: Decimal) -> int:
    """The single place where fractional kopecks are rounded (banker's rounding)."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def parse_rub(text: str, allow_negative: bool = False, allow_zero: bool = True) -> int | None:
    """'1 234,56' / '1234.5' / '5к' / '-300' -> kopecks; None if not an amount."""
    cleaned = re.sub(r"[\s '₽]|руб\.?|р\.?$", "", text.strip(), flags=re.IGNORECASE)
    match = _AMOUNT_RE.match(cleaned)
    if not match:
        return None
    sign, number, thousands = match.groups()
    try:
        value = Decimal(number.replace(",", "."))
    except InvalidOperation:
        return None
    if thousands:
        value *= 1000
    if value >= _MAX_RUB:
        return None
    minor = value.scaleb(2)
    if minor != minor.to_integral_value():
        return None
    result = int(minor)
    if sign == "-":
        if not allow_negative:
            return None
        result = -result
    if result == 0 and not allow_zero:
        return None
    return result


def _group(digits: str) -> str:
    parts = []
    while len(digits) > 3:
        parts.append(digits[-3:])
        digits = digits[:-3]
    parts.append(digits)
    return NBSP.join(reversed(parts))


def format_number(minor: int, exp: int = 2, keep_zero_fraction: bool = False) -> str:
    sign = "−" if minor < 0 else ""
    value = abs(minor)
    if exp == 0:
        return sign + _group(str(value))
    whole, frac = divmod(value, 10**exp)
    text = _group(str(whole))
    if frac or keep_zero_fraction:
        text += "," + str(frac).zfill(exp)
    return sign + text


def format_rub(minor: int) -> str:
    """12345678 -> '123 456,78 ₽' (NBSP groups); whole amounts drop ',00'."""
    return f"{format_number(minor)}{NBSP}₽"


def format_money(minor: int, currency: str) -> str:
    currency = currency.upper()
    symbol = _SYMBOLS.get(currency, currency)
    return f"{format_number(minor, exponent(currency))}{NBSP}{symbol}"
