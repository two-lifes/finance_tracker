from datetime import date
from decimal import Decimal

from bot.services.work_calendar import (
    HourlyPay,
    IncomeRule,
    PaydayRecord,
    PaySchedule,
    Weekend,
    WorkPattern,
    default_anchor,
    describe_periods,
    due_today,
    find_payout,
    next_payday,
    parse_cuts,
    parse_pattern,
    pay_date,
    payouts,
)

ADVANCE = IncomeRule(1, "Аванс", 10, 2000000)
SALARY = IncomeRule(2, "Зарплата", 25, 3000000)
BOTH = PaySchedule([ADVANCE, SALARY])
ONLY_SALARY = PaySchedule([SALARY])


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
    p = next_payday(BOTH, [], today)
    assert (p.rule.id, p.effective) == (2, date(2026, 10, 23))


def test_unconfirmed_payday_is_expected_tomorrow():
    today = date(2026, 10, 23)
    p = next_payday(ONLY_SALARY, [], today)
    assert (p.scheduled, p.effective, p.delayed) == (date(2026, 10, 23), date(2026, 10, 24), False)
    assert [d.scheduled for d in due_today(ONLY_SALARY, [], today)] == [date(2026, 10, 23)]
    # Asked on payday and no answer yet: still expected on the following days
    asked = [PaydayRecord(2, date(2026, 10, 23), "expected")]
    assert next_payday(ONLY_SALARY, asked, date(2026, 10, 26)).effective == date(2026, 10, 27)
    # Never asked (bot set up later): a past payday is not waited for
    assert next_payday(ONLY_SALARY, [], date(2026, 10, 26)).scheduled == date(2026, 11, 25)


def test_received_payday_moves_to_next():
    today = date(2026, 10, 23)
    records = [PaydayRecord(2, date(2026, 10, 23), "received")]
    p = next_payday(BOTH, records, today)
    assert (p.rule.id, p.effective) == (1, date(2026, 11, 10))


def test_salary_delay():
    today = date(2026, 10, 24)
    records = [PaydayRecord(2, date(2026, 10, 23), "delayed", date(2026, 10, 29))]
    p = next_payday(ONLY_SALARY, records, today)
    assert (p.effective, p.delayed) == (date(2026, 10, 29), True)
    # The delay date passed without money: still expected, from tomorrow on
    p = next_payday(ONLY_SALARY, records, date(2026, 10, 30))
    assert p.effective == date(2026, 10, 31)


def test_delay_announced_before_payday():
    records = [PaydayRecord(2, date(2026, 10, 23), "delayed", date(2026, 10, 28))]
    p = next_payday(ONLY_SALARY, records, date(2026, 10, 20))
    assert p.effective == date(2026, 10, 28)


def test_old_unconfirmed_payday_is_forgotten():
    asked = [PaydayRecord(2, date(2026, 10, 23), "expected")]
    p = next_payday(ONLY_SALARY, asked, date(2026, 11, 20))
    assert p.scheduled == date(2026, 11, 25)


def test_weekend_rules():
    # 25.10.2026 is a Sunday
    assert pay_date(2026, 10, 25, Weekend.AFTER) == date(2026, 10, 26)
    assert pay_date(2026, 10, 25, Weekend.SAME) == date(2026, 10, 25)


def hourly_schedule(cuts=(10, 20, 31), delay=10, tax="13", weekend=Weekend.BEFORE):
    rules = [IncomeRule(i + 1, "Зарплата", c, 0) for i, c in enumerate(cuts)]
    pattern = WorkPattern(2, 2, date(2026, 10, 1))  # 1-2 work, 3-4 off, ...
    hourly = HourlyPay(30000, Decimal("12"), Decimal(tax), delay, pattern)
    return PaySchedule(rules, weekend, hourly)


def test_hourly_three_periods_paid_ten_days_later():
    schedule = hourly_schedule()
    result = payouts(schedule, date(2026, 10, 15), date(2026, 11, 30))
    # 20.10 (Tue) pays for 1–10; 30.10 (Fri) for 11–20; 10.11 (Tue) for 21–31; ...
    assert [(p.scheduled, p.period) for p in result[:3]] == [
        (date(2026, 10, 20), (date(2026, 10, 1), date(2026, 10, 10))),
        (date(2026, 10, 30), (date(2026, 10, 11), date(2026, 10, 20))),
        (date(2026, 11, 10), (date(2026, 10, 21), date(2026, 10, 31))),
    ]
    first = result[0]
    # 2/2 from 01.10: 1,2,5,6,9,10 -> 6 shifts × 12 h × 300 ₽ × 0.87
    assert first.shifts == 6
    assert first.amount == 1_879_200
    assert first.label == "Зарплата за 01.10–10.10"


def test_hourly_take_home_rate_and_clamped_month_end():
    schedule = hourly_schedule(tax="0")
    p = find_payout(schedule, 3, date(2026, 12, 10))  # for 21–30 November (cut 31 clamped)
    assert p is not None
    assert p.period == (date(2026, 11, 21), date(2026, 11, 30))
    assert p.amount == p.shifts * 12 * 30000


def test_hourly_payday_on_weekend_moves():
    # 10.11 + 11 days = 21.11, a Saturday -> Friday 20.11
    schedule = hourly_schedule(delay=11)
    p = payouts(schedule, date(2026, 11, 20), date(2026, 11, 20))
    assert p and p[0].period == (date(2026, 11, 1), date(2026, 11, 10))


def test_single_cut_in_middle_of_month():
    schedule = hourly_schedule(cuts=(15,), delay=10)
    # 15.10 + 10 = 25.10, a Sunday -> Friday 23.10
    [p] = payouts(schedule, date(2026, 10, 23), date(2026, 10, 23))
    assert p.period == (date(2026, 9, 16), date(2026, 10, 15))


def test_parse_and_describe_cuts():
    assert parse_cuts("10 20 31") == [10, 20, 31]
    assert parse_cuts("31, 15") == [15, 31]
    assert parse_cuts("0 10") is None
    assert parse_cuts("abc") is None
    assert describe_periods([10, 20, 31]) == "1–10, 11–20, 21–31"
    assert describe_periods([15]) == "16–15"
    assert describe_periods([15, 31]) == "1–15, 16–31"


def test_hourly_expected_payday_flow():
    schedule = hourly_schedule()
    p = next_payday(schedule, [], date(2026, 10, 17))
    assert (p.scheduled, p.payout.period[0]) == (date(2026, 10, 20), date(2026, 10, 1))
