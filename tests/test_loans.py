from datetime import date
from decimal import Decimal

from bot.services.loans import (
    EarlyMode,
    RateUnit,
    accrued_interest,
    allocate,
    annuity_payment,
    card_min_payment,
    early_repayment,
    effective_annual_rate,
    monthly_due_dates,
    penalty,
    remaining_months,
)

Y = RateUnit.YEAR


def test_bank_interest_actual_days():
    # 100 000 ₽ at 36.5 % for 10 days of a non-leap year = 1 000 ₽
    assert accrued_interest(10_000_000, Decimal("36.5"), Y, date(2026, 3, 1), date(2026, 3, 11)) \
        == 100_000


def test_interest_across_leap_year_boundary():
    value = accrued_interest(36_600_000, Decimal("10"), Y, date(2027, 12, 31), date(2028, 1, 2))
    # one day /365 + one day /366
    assert value == round(36_600_000 * 0.1 * (1 / 365 + 1 / 366))


def test_mfo_daily_rate():
    # 15 000 ₽ at 0.8 %/day for 30 days = 3 600 ₽
    assert accrued_interest(1_500_000, Decimal("0.8"), RateUnit.DAY, date(2026, 10, 1),
                            date(2026, 10, 31)) == 360_000


def test_allocate_order_insurance_interest_principal():
    a = allocate(1_000_000, accrued=300_000, principal=5_000_000, insurance=100_000)
    assert (a.insurance, a.interest, a.principal, a.overpaid) == (100_000, 300_000, 600_000, 0)


def test_partial_payment_covers_interest_first():
    a = allocate(200_000, accrued=300_000, principal=5_000_000)
    assert (a.interest, a.principal) == (200_000, 0)


def test_overpayment():
    a = allocate(1_000_000, accrued=0, principal=400_000)
    assert (a.principal, a.overpaid) == (400_000, 600_000)


def test_annuity_and_term():
    payment = annuity_payment(100_000_000, Decimal("12"), Y, 12)
    assert payment == 8_884_879  # 88 848,79 ₽ — standard annuity table value
    assert remaining_months(100_000_000, Decimal("12"), Y, payment) == 12
    assert remaining_months(100_000_000, Decimal("12"), Y, 1_000_000) is None  # < interest


def test_installment_without_interest():
    assert annuity_payment(1_200_000, Decimal("0"), Y, 12) == 100_000
    assert remaining_months(1_250_000, Decimal("0"), Y, 100_000) == 13


def test_early_repayment_term_vs_payment():
    principal, rate = 100_000_000, Decimal("12")
    payment = annuity_payment(principal, rate, Y, 24)
    term = early_repayment(principal, rate, Y, payment, 0, 30_000_000, EarlyMode.TERM)
    assert term.payment_after == payment
    assert term.months_before == 24
    assert term.months_after < 24

    smaller = early_repayment(principal, rate, Y, payment, 0, 30_000_000, EarlyMode.PAYMENT)
    assert smaller.months_after == 24
    assert smaller.payment_after == annuity_payment(70_000_000, rate, Y, 24)
    assert smaller.payment_after < payment


def test_early_repayment_keeps_insurance():
    payment = annuity_payment(10_000_000, Decimal("20"), Y, 12) + 50_000
    r = early_repayment(10_000_000, Decimal("20"), Y, payment, 50_000, 5_000_000,
                        EarlyMode.PAYMENT)
    assert r.payment_after == annuity_payment(5_000_000, Decimal("20"), Y, 12) + 50_000


def test_full_early_repayment():
    r = early_repayment(1_000_000, Decimal("20"), Y, 100_000, 0, 1_500_000, EarlyMode.TERM)
    assert (r.principal_after, r.payment_after, r.months_after) == (0, 0, 0)


def test_penalty():
    # 10 000 ₽ overdue at 20 % per year for 73 days = 400 ₽
    assert penalty(1_000_000, Decimal("20"), 73) == 40_000
    assert penalty(1_000_000, Decimal("20"), 0) == 0


def test_card_min_payment():
    assert card_min_payment(5_000_000, Decimal("3"), floor=30_000) == 150_000
    assert card_min_payment(500_000, Decimal("3"), floor=30_000) == 30_000
    assert card_min_payment(20_000, Decimal("3"), floor=30_000) == 20_000


def test_monthly_due_dates_clamped():
    assert monthly_due_dates(31, date(2026, 1, 15), date(2026, 3, 31)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)
    ]


def test_effective_rate():
    assert effective_annual_rate(Decimal("0.8"), RateUnit.DAY) == Decimal("292.0")
