from datetime import date

from bot.services.work_calendar import (
    IncomeRule,
    PaydayRecord,
    WorkPattern,
    default_anchor,
    due_today,
    next_payday,
    parse_pattern,
    pay_date,
)

ADVANCE = IncomeRule(1, "Аванс", 10, 2000000)
SALARY = IncomeRule(2, "Зарплата", 25, 3000000)


def test_two_by_two():
    p = WorkPattern(2, 2, date(2026, 10, 17))
    assert [p.is_work_day(date(2026, 10, d)) for d in range(15, 23)] == [
        False, False, True, True, False, False, True, True
    ]
    assert p.work_days(date(2026, 10, 17), date(2026, 10, 25)) == 4


def test_five_by_two_starts_on_monday():
    today = date(2026, 10, 17)  # Saturday
    p = WorkPattern(5, 2, default_anchor(5, 2, today))
    assert p.anchor == date(2026, 10, 12)
    assert not p.is_work_day(today)
    assert p.is_work_day(date(2026, 10, 19))


def test_parse_pattern():
    assert parse_pattern("2/2") == (2, 2)
    assert parse_pattern(" 5 / 2 ") == (5, 2)
    assert parse_pattern("1х3") == (1, 3)
    assert parse_pattern("2") is None
    assert parse_pattern("0/2") is None


def test_weekend_payday_moves_to_friday():
    assert pay_date(2026, 10, 25) == date(2026, 10, 23)  # Sunday -> Friday
    assert pay_date(2026, 2, 31) == date(2026, 2, 27)  # clamped to 28th (Sat) -> Friday
    assert pay_date(2026, 11, 10) == date(2026, 11, 10)


def test_next_payday():
    today = date(2026, 10, 17)
    p = next_payday([ADVANCE, SALARY], [], today)
    assert (p.rule.id, p.effective) == (2, date(2026, 10, 23))


def test_unconfirmed_payday_is_expected_tomorrow():
    today = date(2026, 10, 23)
    p = next_payday([SALARY], [], today)
    assert (p.scheduled, p.effective, p.delayed) == (date(2026, 10, 23), date(2026, 10, 24), False)
    assert [d.scheduled for d in due_today([SALARY], [], today)] == [date(2026, 10, 23)]
    # Asked on payday and no answer yet: still expected on the following days
    asked = [PaydayRecord(2, date(2026, 10, 23), "expected")]
    assert next_payday([SALARY], asked, date(2026, 10, 26)).effective == date(2026, 10, 27)
    # Never asked (bot set up later): a past payday is not waited for
    assert next_payday([SALARY], [], date(2026, 10, 26)).scheduled == date(2026, 11, 25)


def test_received_payday_moves_to_next():
    today = date(2026, 10, 23)
    records = [PaydayRecord(2, date(2026, 10, 23), "received")]
    p = next_payday([ADVANCE, SALARY], records, today)
    assert (p.rule.id, p.effective) == (1, date(2026, 11, 10))


def test_salary_delay():
    today = date(2026, 10, 24)
    records = [PaydayRecord(2, date(2026, 10, 23), "delayed", date(2026, 10, 29))]
    p = next_payday([SALARY], records, today)
    assert (p.effective, p.delayed) == (date(2026, 10, 29), True)
    # The delay date passed without money: still expected, from tomorrow on
    p = next_payday([SALARY], records, date(2026, 10, 30))
    assert p.effective == date(2026, 10, 31)


def test_delay_announced_before_payday():
    records = [PaydayRecord(2, date(2026, 10, 23), "delayed", date(2026, 10, 28))]
    p = next_payday([SALARY], records, date(2026, 10, 20))
    assert p.effective == date(2026, 10, 28)


def test_old_unconfirmed_payday_is_forgotten():
    asked = [PaydayRecord(2, date(2026, 10, 23), "expected")]
    p = next_payday([SALARY], asked, date(2026, 11, 20))
    assert p.scheduled == date(2026, 11, 25)
