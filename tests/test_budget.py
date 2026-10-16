"""Limit, obligations and subscription handling."""

from datetime import date
from decimal import Decimal

from bot.services.limit import compute_limit, payday_plan, spending
from bot.services.obligations import (
    DueInfo,
    State,
    loan_obligations,
    merge,
    sub_obligations,
)
from bot.services.subscriptions import (
    SubInfo,
    SubPayment,
    UpcomingCharge,
    paid_between,
    parse_payments,
    parse_subscriptions,
    parse_upcoming,
    pending_charges,
)

TODAY = date(2026, 10, 17)


def rates(currency, on):
    return {"USD": Decimal("80")}.get(currency)


def due(id_, day, amount, auto=False, status="expected", paid=0, penalty_rate=None):
    return DueInfo(id_, 1, f"Loan {id_}", day, amount, paid, status, auto, penalty_rate)


def test_spending_formula():
    # yesterday 10 000 + income 5 000 − loan 3 000 − today 11 000 = 1 000 spent
    assert spending(1_000_000, 1_100_000, 500_000, 300_000) == 100_000


def test_limit_until_payday():
    obligations = loan_obligations(
        [due(1, date(2026, 10, 20), 500_000), due(2, date(2026, 10, 25), 900_000)], TODAY
    )
    limit = compute_limit(2_000_000, obligations, TODAY, horizon=date(2026, 10, 23))
    # (20 000 − 5 000) / 6 days = 2 500
    assert (limit.mandatory, limit.days, limit.per_day) == (500_000, 6, 250_000)
    assert limit.shortfall == 0


def test_limit_shortfall():
    obligations = loan_obligations([due(1, date(2026, 10, 18), 3_000_000)], TODAY)
    limit = compute_limit(1_000_000, obligations, TODAY, horizon=date(2026, 10, 23))
    assert limit.per_day == 0
    assert limit.shortfall == 2_000_000


def test_limit_rounds_down_to_rubles():
    limit = compute_limit(1_000_099, [], TODAY, horizon=date(2026, 10, 20))
    assert limit.per_day == 333_300


def test_paid_and_partial_dues():
    items = loan_obligations(
        [
            due(1, date(2026, 10, 18), 500_000, status="paid", paid=500_000),
            due(2, date(2026, 10, 19), 500_000, paid=200_000),
        ],
        TODAY,
    )
    assert [(o.ref, o.amount) for o in items] == [(2, 300_000)]


def test_missed_manual_payment_is_overdue_with_penalty():
    [o] = loan_obligations([due(1, date(2026, 9, 17), 1_000_000, penalty_rate=Decimal("20"))],
                           TODAY)
    assert o.state == State.OVERDUE
    assert o.penalty == round(1_000_000 * 0.2 * 30 / 365)
    assert o.amount == 1_000_000 + o.penalty


def test_past_auto_debit_waits_for_balance_not_overdue():
    [o] = loan_obligations([due(1, date(2026, 10, 16), 500_000, auto=True)], TODAY)
    assert o.state == State.UPCOMING


def test_failed_auto_debit_awaits_payment():
    [o] = loan_obligations([due(1, date(2026, 10, 16), 500_000, auto=True,
                                status="not_debited")], TODAY)
    assert o.state == State.AWAITING


def test_late_items_count_even_after_horizon_and_sort_first():
    items = merge(
        loan_obligations([due(1, date(2026, 10, 18), 100_000)], TODAY),
        loan_obligations([due(2, date(2026, 10, 10), 200_000)], TODAY),
    )
    assert [o.ref for o in items] == [2, 1]
    limit = compute_limit(1_000_000, items, TODAY, horizon=date(2026, 10, 18))
    assert limit.mandatory == 200_000


SUB_JSON = {
    "id": 1, "user_id": 1, "name": "Claude Code", "amount_minor": 2224, "currency": "USD",
    "period_unit": "month", "period_count": 1, "anchor_date": "2026-10-16",
    "next_charge_date": "2026-11-16", "status": "active", "auto_renew": True,
    "trial_end": None, "remind_offsets": None, "category": None, "note": None,
    "created_at": "2026-10-16T09:00:00Z", "archived_at": None,
}


def test_parse_api_payloads_tolerates_bad_items_and_unknown_status():
    subs = parse_subscriptions([SUB_JSON, {"id": "x"}, {**SUB_JSON, "id": 2, "status": "trial"}])
    assert [(s.id, s.status) for s in subs] == [(1, "active"), (2, "trial")]
    up = parse_upcoming([{"subscription_id": 1, "name": "Claude Code",
                          "charge_date": "2026-11-16", "amount_minor": 2224,
                          "currency": "USD", "auto_renew": True}])
    assert up[0].charge_date == date(2026, 11, 16)
    pays = parse_payments([{"id": 1, "subscription_id": 1, "charge_date": "2026-10-16",
                            "amount_minor": 2224, "currency": "USD"}, "junk"])
    assert len(pays) == 1
    assert parse_subscriptions({"error": "x"}) == []


def sub(id_, next_date, auto=True, status="active", amount=29900, currency="RUB"):
    return SubInfo(id_, f"Sub {id_}", amount, currency, next_date, status, auto)


def up(id_, d, amount=29900, currency="RUB", auto=True):
    return UpcomingCharge(id_, f"Sub {id_}", d, amount, currency, auto)


def test_upcoming_charges_converted_and_paid_ones_skipped():
    subs = [sub(1, date(2026, 10, 20), currency="USD", amount=2224), sub(2, date(2026, 10, 18))]
    upcoming = [up(1, date(2026, 10, 20), 2224, "USD"), up(2, date(2026, 10, 18))]
    # Sub 2 was paid ahead of time
    payments = [SubPayment(2, date(2026, 10, 18), 29900, "RUB")]
    charges = pending_charges(subs, upcoming, payments, TODAY, rates)
    assert [(c.subscription_id, c.rub_minor) for c in charges] == [(1, 177920)]
    [o] = sub_obligations(charges)
    assert o.original == (2224, "USD")


def test_manual_subscription_past_due_awaits_payment():
    subs = [sub(3, date(2026, 10, 15), auto=False)]
    charges = pending_charges(subs, [], [], TODAY, rates)
    assert [(c.subscription_id, c.awaiting) for c in charges] == [(3, True)]
    # Once the payment record appears it is no longer owed
    paid = [SubPayment(3, date(2026, 10, 15), 29900, "RUB")]
    assert pending_charges(subs, [], paid, TODAY, rates) == []


def test_archived_and_trial_subscriptions_are_ignored():
    subs = [sub(4, date(2026, 10, 15), auto=False, status="archived"),
            sub(5, date(2026, 10, 20), status="trial")]
    upcoming = [up(5, date(2026, 10, 20))]
    assert pending_charges(subs, upcoming, [], TODAY, rates) == []


def test_missing_rate_is_reported_not_guessed():
    charges = pending_charges([sub(6, date(2026, 10, 20), currency="EUR")],
                              [up(6, date(2026, 10, 20), 999, "EUR")], [], TODAY, rates)
    [o] = sub_obligations(charges)
    assert o.amount is None
    limit = compute_limit(1_000_000, [o], TODAY, date(2026, 10, 25))
    assert limit.unknown_amounts


def test_paid_between_converts_and_filters_by_date():
    payments = [SubPayment(1, date(2026, 10, 16), 2224, "USD"),
                SubPayment(2, date(2026, 10, 17), 29900, "RUB"),
                SubPayment(3, date(2026, 10, 18), 100, "RUB")]
    total, chosen = paid_between(payments, date(2026, 10, 15), TODAY, rates)
    assert total == 177920 + 29900
    assert len(chosen) == 2


def test_payday_plan():
    items = loan_obligations([due(1, date(2026, 10, 30), 1_000_000),
                              due(2, date(2026, 11, 12), 2_000_000)], date(2026, 10, 23))
    plan = payday_plan(3_000_000, items, next_horizon=date(2026, 11, 10))
    assert (plan.set_aside, plan.free) == (1_000_000, 2_000_000)
