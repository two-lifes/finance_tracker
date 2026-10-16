"""Russian plural forms: 1 день / 2 дня / 5 дней."""

DAYS = ("день", "дня", "дней")
PAYMENTS = ("платёж", "платежа", "платежей")
LOANS = ("кредит", "кредита", "кредитов")
SHIFTS = ("смена", "смены", "смен")
MONTHS = ("месяц", "месяца", "месяцев")


def plural(n: int, forms: tuple[str, str, str]) -> str:
    one, few, many = forms
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def count(n: int, forms: tuple[str, str, str]) -> str:
    return f"{n} {plural(n, forms)}"
