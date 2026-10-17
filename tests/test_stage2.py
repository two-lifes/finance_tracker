"""Shortfall forecast, payment priority, avalanche, subscription costs."""

from datetime import date
from decimal import Decimal

from bot.services.forecast import forecast
from bot.services.loans import LoanKind
from bot.services.obligations import Obligation, Source, State
from bot.services.priority import Group, LoanTerms, cancel_candidates, group_of, rank
from bot.services.strategy import Candidate, advise, avalanche, free_money
from bot.services.subscriptions import SubInfo, monthly_costs

TODAY = date(2026, 10, 17)


def loan(ref, day, amount, loan_id=None, state=State.UPCOMING, title="Кредит"):
    return Obligation(Source.LOAN, ref, title, date(2026, 10, day), amount, state, False,
                      loan_id=loan_id if loan_id is not None else ref)


def sub(ref, day, amount, title="Подписка"):
    return Obligation(Source.SUB, ref, title, date(2026, 10, day), amount, State.UPCOMING, True)


# --- forecast -----------------------------------------------------------------------------


def test_no_shortfall_when_money_covers_payments():
    assert forecast(10_000_00, [loan(1, 18, 5_000_00), loan(2, 20, 5_000_00)], [], TODAY) is None


def test_shortfall_on_third_day():
    items = [loan(1, 17, 3_000_00), loan(2, 19, 5_000_00), sub(3, 20, 1_000_00)]
    s = forecast(6_000_00, items, [], TODAY)
    assert s.day == date(2026, 10, 19)
    assert s.amount == 3_000_00  # 6000 − 3000 − 5000 − 1000
    assert [o.ref for o in s.items] == [2, 3]


def test_payments_after_window_are_ignored():
    assert forecast(1_000_00, [loan(1, 22, 5_000_00)], [], TODAY) is None


def test_salary_in_window_covers_payment_on_same_day():
    items = [loan(1, 19, 5_000_00)]
    assert forecast(1_000_00, items, [(date(2026, 10, 19), 30_000_00)], TODAY) is None
    s = forecast(1_000_00, items, [(date(2026, 10, 20), 30_000_00)], TODAY)
    assert (s.day, s.amount) == (date(2026, 10, 19), 4_000_00)


def test_late_payment_counts_today():
    late = loan(1, 10, 2_000_00, state=State.OVERDUE)
    s = forecast(1_000_00, [late], [], TODAY)
    assert (s.day, s.amount, s.items) == (TODAY, 1_000_00, [late])


def test_negative_balance_is_a_shortfall_today():
    s = forecast(-500_00, [], [], TODAY)
    assert (s.day, s.amount) == (TODAY, 500_00)


# --- priority -----------------------------------------------------------------------------


TERMS = {
    1: LoanTerms(LoanKind.BANK, Decimal(20)),
    2: LoanTerms(LoanKind.MFO, Decimal(292)),
    3: LoanTerms(LoanKind.INSTALLMENT, Decimal(0)),
    4: LoanTerms(LoanKind.CARD, Decimal(30)),
    5: LoanTerms(LoanKind.BANK, Decimal(35)),
}


def test_rank_by_group_then_rate():
    items = [sub(9, 18, 500_00), loan(3, 18, 1_000_00), loan(1, 18, 1_000_00),
             loan(4, 18, 1_000_00), loan(2, 19, 1_000_00), loan(5, 20, 1_000_00)]
    assert [o.ref for o in rank(items, TERMS, TODAY)] == [2, 5, 1, 4, 3, 9]


def test_overdue_bank_loan_is_urgent():
    late = loan(1, 10, 1_000_00, state=State.OVERDUE)
    assert group_of(late, TERMS) == Group.URGENT
    # Ahead of a costlier bank loan that is not late yet
    assert [o.ref for o in rank([loan(5, 18, 1_000_00), late], TERMS, TODAY)] == [1, 5]


def test_cancel_candidates_most_expensive_first():
    items = [sub(1, 18, 300_00), loan(2, 18, 1_000_00), sub(3, 19, 900_00)]
    assert [o.ref for o in cancel_candidates(items)] == [3, 1]


# --- strategy -----------------------------------------------------------------------------


def test_avalanche_picks_highest_rate_then_smaller_debt():
    a = Candidate(1, "Банк", Decimal(20), 100_000_00)
    b = Candidate(2, "МФО", Decimal(292), 10_000_00)
    c = Candidate(3, "МФО-2", Decimal(292), 5_000_00)
    free = Candidate(4, "Рассрочка", Decimal(0), 1_000_00)
    assert avalanche([a, b, c, free]).loan_id == 3
    assert avalanche([free]) is None


def test_free_money_and_threshold():
    assert free_money(20_000_00, 5_000_00, 1_000_00, 6) == 9_000_00
    loans = [Candidate(1, "Банк", Decimal(20), 100_000_00)]
    assert advise(500_00, 1_000_00, loans) is None
    assert advise(-5_00, 0, loans) is None
    advice = advise(9_000_00, 1_000_00, loans)
    assert (advice.free, advice.target.loan_id) == (9_000_00, 1)


def test_advice_never_exceeds_debt():
    loans = [Candidate(1, "МФО", Decimal(292), 3_000_00)]
    assert advise(9_000_00, 1_000_00, loans).free == 3_000_00


# --- subscriptions ------------------------------------------------------------------------


def info(i, amount, unit="month", n=1, currency="RUB", status="active"):
    return SubInfo(i, f"s{i}", amount, currency, TODAY, status, True, unit, n)


def test_monthly_costs_by_period():
    rates = {"USD": Decimal(80)}
    costs = monthly_costs(
        [info(1, 300_00), info(2, 1_200_00, "year"), info(3, 100_00, "week"),
         info(4, 10_00, "day"), info(5, 600_00, "month", 3), info(6, 10_00, currency="USD"),
         info(7, 999_00, status="paused")],
        TODAY, lambda c, d: rates.get(c),
    )
    by_id = {c.sub.id: c.rub_minor for c in costs}
    assert 7 not in by_id
    assert by_id[1] == 300_00
    assert by_id[2] == 100_00
    assert by_id[3] == 434_82  # 100 × 365.25 / 7 / 12
    assert by_id[4] == 304_38  # 10 × 30.4375
    assert by_id[5] == 200_00
    assert by_id[6] == 800_00
    assert [c.sub.id for c in costs][:2] == [6, 3]


def test_monthly_cost_without_rate():
    [cost] = monthly_costs([info(1, 10_00, currency="EUR")], TODAY, lambda c, d: None)
    assert cost.rub_minor is None
