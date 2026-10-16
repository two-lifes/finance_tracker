from datetime import date

import pytest

from bot.services.dates import add_months, fmt_date, fmt_words, parse_date, relative_days
from bot.services.money import (
    NBSP,
    format_money,
    format_rub,
    parse_rub,
    to_rub_minor,
)
from bot.services.plural import DAYS, PAYMENTS, count, plural


@pytest.mark.parametrize(
    ("minor", "text"),
    [
        (123456, f"1{NBSP}234,56{NBSP}₽"),
        (100000, f"1{NBSP}000{NBSP}₽"),
        (5, f"0,05{NBSP}₽"),
        (123456789012, f"1{NBSP}234{NBSP}567{NBSP}890,12{NBSP}₽"),
        (-50000, f"−500{NBSP}₽"),
        (0, f"0{NBSP}₽"),
    ],
)
def test_format_rub(minor, text):
    assert format_rub(minor) == text


def test_format_foreign():
    assert format_money(2224, "USD") == f"22,24{NBSP}$"
    assert format_money(990, "JPY") == f"990{NBSP}JPY"


@pytest.mark.parametrize(
    ("text", "minor"),
    [
        ("12345", 1234500),
        ("1 234,56", 123456),
        ("1234.5", 123450),
        ("5к", 500000),
        ("2,5k", 250000),
        ("15 000 ₽", 1500000),
        ("0", 0),
    ],
)
def test_parse_rub(text, minor):
    assert parse_rub(text) == minor


@pytest.mark.parametrize("text", ["", "abc", "1,234", "12.345", "-5", "5 еда"])
def test_parse_rub_rejects(text):
    assert parse_rub(text) is None


def test_parse_rub_negative_and_zero():
    assert parse_rub("-300", allow_negative=True) == -30000
    assert parse_rub("0", allow_zero=False) is None


def test_currency_conversion_uses_iso_exponent():
    from decimal import Decimal

    assert to_rub_minor(2224, "USD", Decimal("80.5")) == 179032
    assert to_rub_minor(1000, "JPY", Decimal("0.55")) == 55000
    assert to_rub_minor(1000, "KWD", Decimal("260")) == 26000  # 1.000 KWD
    assert to_rub_minor(500, "RUB", Decimal("999")) == 500


@pytest.mark.parametrize(
    ("n", "word"),
    [(1, "день"), (2, "дня"), (4, "дня"), (5, "дней"), (11, "дней"), (12, "дней"),
     (21, "день"), (22, "дня"), (111, "дней"), (0, "дней")],
)
def test_plural_days(n, word):
    assert plural(n, DAYS) == word


def test_count_payments():
    assert count(1, PAYMENTS) == "1 платёж"
    assert count(3, PAYMENTS) == "3 платежа"
    assert count(25, PAYMENTS) == "25 платежей"


def test_dates():
    assert fmt_date(date(2026, 3, 5)) == "05.03.2026"
    assert fmt_words(date(2026, 3, 5)) == "5 марта"
    assert relative_days(date(2026, 10, 20), date(2026, 10, 17)) == "через 3 дня"
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 15), 1, day=31) == date(2026, 2, 28)


def test_parse_date():
    today = date(2026, 10, 17)
    assert parse_date("20.10", today) == date(2026, 10, 20)
    assert parse_date("01.02", today) == date(2027, 2, 1)
    assert parse_date("01.10", today, future=False) == date(2026, 10, 1)
    assert parse_date("31.02", today) is None
    assert parse_date("05.11.27", today) == date(2027, 11, 5)
