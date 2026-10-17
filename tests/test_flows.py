"""End-to-end dialogs: real dispatcher and handlers, fake Telegram, fake subscription API."""

from datetime import UTC, date, datetime

from bot.db.models import (
    BalanceSnapshot,
    ExpenseNote,
    Income,
    IncomeRule,
    Loan,
    LoanDue,
    LoanPayment,
    User,
)
from bot.keyboards import BTN_HOME, DueCb, EarlyCb, LoanCb
from tests.harness import onboard, plain
from tests.tg_fakes import STRANGER_ID


async def add_loan(h, lender="Сбер", kind="bank", principal="100000", rate="20",
                   payment="10000", due="20.10", insurance=None, method="auto"):
    await h.tap("ladd:start")
    await h.text(lender)
    await h.tap(f"lkind:{kind}")
    await h.text(principal)
    if kind != "installment":
        await h.text(rate)
    await h.text(payment)
    await h.text(due)
    if kind in ("bank", "installment"):
        if insurance:
            await h.text(insurance)
        else:
            await h.tap("lins:0")
    await h.tap(f"lpay:{method}")
    return await h.tap("ladd:save")


async def test_onboarding(h):
    await onboard(h)
    [user] = await h.all(User)
    assert (user.timezone, user.shift_work, user.shift_off) == ("Europe/Moscow", 2, 2)
    assert user.shift_anchor == date(2026, 10, 17)
    [rule] = await h.all(IncomeRule)
    assert (rule.title, rule.day, rule.amount_minor) == ("Зарплата", 23, 5_000_000)
    [snap] = await h.all(BalanceSnapshot)
    assert snap.amount_minor == 2_000_000
    assert "Готово" in h.net.last_text()


async def test_income_rules_with_titles(h):
    await onboard(h, income="10 25 000 аванс\n25 5к")
    rules = await h.all(IncomeRule)
    assert [(r.title, r.day, r.amount_minor) for r in rules] == [
        ("аванс", 10, 2_500_000), ("Зарплата", 25, 500_000)
    ]


async def test_stranger_is_ignored(h):
    assert await h.text("/start", user_id=STRANGER_ID) is None


async def test_main_screen_limit(h):
    await onboard(h)  # 20 000 ₽, salary on the 23rd (Friday)
    text = await h.text(BTN_HOME)
    # 20 000 / 6 days (17..22) = 3 333 ₽
    assert "Можно тратить сегодня: 3 333 ₽" in text
    assert "Следующая выплата: Зарплата, 23.10.2026 — через 6 дней" in text


async def test_add_loan_and_dues(h):
    await onboard(h)
    reply = await add_loan(h)
    assert "Кредит добавлен" in h.net.texts()[-2]
    del reply
    [loan] = await h.all(Loan)
    assert (loan.principal_minor, loan.payment_day, loan.pay_method) == (10_000_000, 20, "auto")
    dues = await h.all(LoanDue)
    assert [d.due_date for d in dues][:2] == [date(2026, 10, 20), date(2026, 11, 20)]
    text = await h.text(BTN_HOME)
    # (20 000 − 10 000) / 6 = 1 666 ₽
    assert "Можно тратить сегодня: 1 666 ₽" in text
    assert "вт 20.10 · Сбер — 10 000 ₽ · ⟳ авто" in text


async def test_mfo_single_payment(h):
    await onboard(h)
    await add_loan(h, lender="Займер", kind="mfo", principal="15000", rate="0,8",
                   payment="18000", due="25.10", method="manual")
    [loan] = await h.all(Loan)
    [due] = await h.all(LoanDue)
    assert (loan.schedule, due.due_date, due.amount_minor) == ("once", date(2026, 10, 25),
                                                               1_800_000)
    # Debt grows daily; on the due date it equals the amount to return
    from bot.services.loan_ops import debt_of
    assert debt_of(loan, date(2026, 10, 25)) == 1_800_000


async def test_daily_balance_spending_and_auto_debit(h, now):
    await onboard(h, balance="20000")
    await add_loan(h, payment="5000", due="18.10")
    now.set(datetime(2026, 10, 18, 18, 0, tzinfo=UTC))
    # 20 000 − auto-debit 5 000 − spent 1 000 = 14 000
    text = await h.text("14000")
    assert "Потрачено со вчера: 1 000 ₽" in text
    assert "Учёл автосписание: Сбер — 5 000 ₽" in text
    [payment] = await h.all(LoanPayment)
    assert (payment.source, payment.amount_minor) == ("auto", 500_000)
    [first, *_] = await h.all(LoanDue)
    assert first.status == "paid"

    # Entering the balance again the same day must not count the debit twice
    text = await h.text("13500")
    assert "Потрачено со вчера: 1 500 ₽" in text
    assert len(await h.all(LoanPayment)) == 1


async def test_auto_debit_did_not_happen(h, now):
    await onboard(h, balance="20000")
    await add_loan(h, payment="5000", due="18.10")
    now.set(datetime(2026, 10, 18, 18, 0, tzinfo=UTC))
    await h.text("19000")
    [first, *_] = await h.all(LoanDue)
    await h.tap(DueCb(action="undo", due_id=first.id).pack())
    assert "не списался" in h.net.last_text()
    [first, *_] = await h.all(LoanDue)
    assert (first.status, first.paid_minor) == ("not_debited", 0)
    assert await h.all(LoanPayment) == []
    [loan] = await h.all(Loan)
    assert loan.principal_minor == 10_000_000
    text = await h.text(BTN_HOME)
    assert "ждёт оплаты" in text
    # Re-entering the balance recomputes spending without the reverted debit
    text = await h.text("19000")
    assert "Потрачено со вчера: 1 000 ₽" in text


async def test_manual_payment_confirmation(h, now):
    await onboard(h, balance="20000")
    await add_loan(h, payment="5000", due="18.10", method="manual")
    now.set(datetime(2026, 10, 18, 18, 0, tzinfo=UTC))
    text = await h.text("14500")
    assert "оплачен?" in text
    [first, *_] = await h.all(LoanDue)
    await h.tap(DueCb(action="paid", due_id=first.id).pack())
    [payment] = await h.all(LoanPayment)
    assert (payment.source, payment.amount_minor) == ("manual", 500_000)
    # Now the payment explains the drop: only 500 ₽ were spent
    text = await h.text("14500")
    assert "Потрачено со вчера: 500 ₽" in text


async def test_overdue_manual_payment(h, now):
    await onboard(h, balance="20000")
    await add_loan(h, payment="5000", due="18.10", method="manual")
    now.set(datetime(2026, 10, 20, 7, 0, tzinfo=UTC))
    text = await h.text(BTN_HOME)
    assert "просрочен на 2 дня" in text


async def test_partial_and_extra_payment(h):
    await onboard(h, balance="50000")
    await add_loan(h, payment="10000", due="20.10", method="manual")
    [first, *_] = await h.all(LoanDue)
    await h.tap(DueCb(action="part", due_id=first.id).pack())
    text = await h.text("4000")
    assert "частичный платёж 4 000 ₽" in text
    assert "осталось внести 6 000 ₽" in text
    await h.tap(DueCb(action="part", due_id=first.id).pack())
    await h.text("16000")  # 6 000 to the due + 10 000 early
    assert "Как пересчитать график" in h.net.last_text()
    text = await h.tap(EarlyCb(mode="payment").pack())
    assert "Платёж теперь" in text
    [loan] = await h.all(Loan)
    assert loan.payment_minor < 1_000_000
    [first, *rest] = await h.all(LoanDue)
    assert first.status == "paid"
    assert all(d.amount_minor == loan.payment_minor for d in rest)


async def test_early_repayment_reduces_term(h):
    await onboard(h, balance="50000")
    await add_loan(h, principal="30000", payment="10000", due="20.10")
    [loan] = await h.all(Loan)
    await h.tap(LoanCb(action="early", loan_id=loan.id).pack())
    await h.text("10000")
    text = await h.tap(EarlyCb(mode="term").pack())
    assert "Срок сократился" in text
    [loan] = await h.all(Loan)
    assert loan.payment_minor == 1_000_000


async def test_full_early_repayment_closes_loan(h):
    await onboard(h, balance="50000")
    await add_loan(h, kind="installment", principal="12000", payment="1000", due="20.10")
    [loan] = await h.all(Loan)
    await h.tap(LoanCb(action="early", loan_id=loan.id).pack())
    await h.text("12000")
    text = await h.tap(EarlyCb(mode="term").pack())
    assert "полностью погашен" in text
    [loan] = await h.all(Loan)
    assert loan.status == "closed"
    assert await h.all(LoanDue) == []


async def test_installment_last_payment_is_the_rest(h):
    await onboard(h)
    await add_loan(h, kind="installment", principal="2500", payment="1000", due="20.10")
    dues = await h.all(LoanDue)
    assert [d.amount_minor for d in dues] == [100_000, 100_000, 50_000][: len(dues)]


async def test_free_text_inputs(h):
    await onboard(h)
    assert "поступление 5 000 ₽ (премия)" in await h.text("+5000 премия")
    assert "500" in await h.text("500 еда")
    assert "Наличные: 2 000 ₽" in await h.text("нал 2000")
    [income] = await h.all(Income)
    assert (income.amount_minor, income.comment) == (500_000, "премия")
    [note] = await h.all(ExpenseNote)
    assert note.category == "еда"
    text = await h.text(BTN_HOME)
    assert "наличные 2 000 ₽" in text


async def test_cash_counts_in_spending(h, now):
    await onboard(h, balance="20000")
    await h.text("нал 3000")
    now.set(datetime(2026, 10, 18, 18, 0, tzinfo=UTC))
    await h.text("нал 1000")  # 2 000 cash spent
    text = await h.text("19000")  # 1 000 card spent
    assert "Потрачено со вчера: 3 000 ₽" in text


async def test_income_explains_balance_growth(h, now):
    await onboard(h, balance="20000")
    now.set(datetime(2026, 10, 18, 18, 0, tzinfo=UTC))
    text = await h.text("25000")
    assert "Денег стало больше" in text
    await h.text("+6000 подработка")
    text = await h.text("25000")
    assert "Потрачено со вчера: 1 000 ₽" in text


def sub_json(id_, name, amount, currency, next_date, auto=True, status="active"):
    return {
        "id": id_, "user_id": 1, "name": name, "amount_minor": amount, "currency": currency,
        "period_unit": "month", "period_count": 1, "anchor_date": "2026-09-16",
        "next_charge_date": next_date, "status": status, "auto_renew": auto,
        "trial_end": None, "remind_offsets": None, "category": None, "note": None,
        "created_at": "2026-09-16T09:00:00Z", "archived_at": None,
    }


async def test_subscriptions_in_limit(h):
    await onboard(h, balance="20000")
    h.subs.subs = [sub_json(1, "Claude Code", 2224, "USD", "2026-10-20"),
                   sub_json(2, "Кинопоиск", 29900, "RUB", "2026-10-15", auto=False)]
    h.subs.upcoming_items = [{"subscription_id": 1, "name": "Claude Code",
                              "charge_date": "2026-10-20", "amount_minor": 2224,
                              "currency": "USD", "auto_renew": True}]
    text = await h.text(BTN_HOME)
    assert "Claude Code" in text
    assert "1 779,20 ₽" in text  # 22.24 USD × 80
    assert "Кинопоиск" in text and "ждёт оплаты" in text
    assert "Обязательные до зарплаты: 2 078,20 ₽" in text

    # Paid in the subscription bot: no longer owed
    h.subs.payment_items = [{"id": 1, "subscription_id": 2, "charge_date": "2026-10-15",
                             "amount_minor": 29900, "currency": "RUB"}]
    text = await h.text(BTN_HOME)
    assert "Кинопоиск" not in text


async def test_subscription_api_down_uses_cache(h):
    await onboard(h)
    h.subs.subs = [sub_json(1, "Spotify", 29900, "RUB", "2026-10-20")]
    h.subs.upcoming_items = [{"subscription_id": 1, "name": "Spotify",
                              "charge_date": "2026-10-20", "amount_minor": 29900,
                              "currency": "RUB", "auto_renew": True}]
    await h.text(BTN_HOME)
    h.subs.down = True
    text = await h.text(BTN_HOME)
    assert "Spotify" in text
    assert "Сервис подписок недоступен, данные на 17.10 10:00" in text


async def test_subscription_charge_is_not_spending(h, now):
    await onboard(h, balance="20000")
    now.set(datetime(2026, 10, 18, 18, 0, tzinfo=UTC))
    h.subs.payment_items = [{"id": 1, "subscription_id": 1, "charge_date": "2026-10-18",
                             "amount_minor": 29900, "currency": "RUB"}]
    text = await h.text("19201")
    assert "Потрачено со вчера: 500 ₽" in text
    assert "подписок" in text


async def test_loan_reminders(h, now):
    await onboard(h)
    await add_loan(h, payment="5000", due="21.10", method="manual")
    before = len(h.net.sent())
    now.set(datetime(2026, 10, 18, 7, 1, tzinfo=UTC))  # 3 days before, 10:01 MSK
    await h.tick()
    await h.tick()  # no duplicates
    sent = h.net.sent()[before:]
    assert len(sent) == 1
    assert "Через 3 дня" in sent[0].text and "Сбер" in sent[0].text
    assert sent[0].reply_markup is not None  # "Оплатил" for manual loans
    now.set(datetime(2026, 10, 20, 7, 1, tzinfo=UTC))
    await h.tick()
    assert "Завтра" in h.net.sent()[-1].text


async def test_no_reminder_for_paid_due(h, now):
    await onboard(h)
    await add_loan(h, payment="5000", due="21.10", method="manual")
    [first, *_] = await h.all(LoanDue)
    await h.tap(DueCb(action="paid", due_id=first.id).pack())
    before = len(h.net.sent())
    now.set(datetime(2026, 10, 18, 7, 1, tzinfo=UTC))
    await h.tick()
    assert h.net.sent()[before:] == []


async def test_evening_balance_reminder(h, now):
    await onboard(h)
    now.set(datetime(2026, 10, 18, 18, 0, tzinfo=UTC))  # 21:00 MSK, no balance today
    await h.tick()
    assert "пришлите остаток" in h.net.sent()[-1].text
    count = len(h.net.sent())
    await h.tick()
    assert len(h.net.sent()) == count


async def test_payday_question_and_summary(h, now):
    await onboard(h, balance="20000")
    await add_loan(h, payment="5000", due="28.10")
    now.set(datetime(2026, 10, 23, 7, 0, tzinfo=UTC))
    await h.tick()
    question = h.net.sent()[-1]
    assert "день выплаты" in question.text
    data = question.reply_markup.inline_keyboard[0][0].callback_data
    text = await h.tap(data)
    assert "пришло 50 000 ₽" in text
    assert "Сразу отложите 5 000 ₽" in text
    [income] = await h.all(Income)
    assert (income.kind, income.amount_minor) == ("salary", 5_000_000)
    # Next payday is in November now
    text = await h.text(BTN_HOME)
    assert "23.11.2026" in text


async def test_salary_delay(h, now):
    await onboard(h, balance="20000")
    now.set(datetime(2026, 10, 23, 7, 0, tzinfo=UTC))
    await h.tick()
    question = h.net.sent()[-1]
    delay = question.reply_markup.inline_keyboard[2][0].callback_data
    await h.tap(delay)
    three_days = h.net.sent()[-1].reply_markup.inline_keyboard[0][1].callback_data
    text = await h.tap(three_days)
    assert "26.10.2026" in text
    text = await h.text(BTN_HOME)
    assert "Зарплата, 26.10.2026 (задерживается) — через 3 дня" in text


async def test_loan_edit_and_close(h):
    await onboard(h)
    await add_loan(h, payment="5000", due="20.10")
    [loan] = await h.all(Loan)
    await h.tap(f"ledit:payment:{loan.id}")
    text = await h.text("6000")
    assert "Платёж: 6 000 ₽" in text
    dues = await h.all(LoanDue)
    assert all(d.amount_minor == 600_000 for d in dues)
    await h.tap(f"ledit:penalty:{loan.id}")
    assert "Пени при просрочке: 20% годовых" in await h.text("20")
    await h.tap(LoanCb(action="close", loan_id=loan.id).pack())
    await h.tap(LoanCb(action="close_yes", loan_id=loan.id).pack())
    [loan] = await h.all(Loan)
    assert loan.status == "closed"
    assert "Общий долг" not in await h.text(BTN_HOME)


async def test_scheduler_syncs_hourly_and_survives_outages(h, now, cbr):
    await onboard(h)
    h.subs.subs = [sub_json(1, "Claude Code", 2224, "USD", "2026-10-20")]
    await h.tick()
    calls = h.subs.calls
    assert calls == 3  # subscriptions, upcoming, payments
    assert cbr.calls == 1  # USD appeared: rates fetched right away
    await h.tick()
    assert h.subs.calls == calls  # not again within the hour
    now.set(datetime(2026, 10, 17, 8, 1, tzinfo=UTC))
    h.subs.down = True
    cbr.down = True
    await h.tick()  # failures are recorded, nothing raises
    assert h.subs.calls > calls
    text = await h.text(BTN_HOME)
    assert "Сервис подписок недоступен" in text


async def test_mfo_partial_early_repayment_lowers_due(h):
    await onboard(h, balance="50000")
    await add_loan(h, lender="Займер", kind="mfo", principal="15000", rate="0,8",
                   payment="18000", due="25.10", method="manual")
    [loan] = await h.all(Loan)
    await h.tap(LoanCb(action="early", loan_id=loan.id).pack())
    await h.text("10000")
    [loan] = await h.all(Loan)
    [due] = await h.all(LoanDue)
    # The due is what will be owed on its date, not the original amount to return
    from bot.services.loan_ops import debt_of
    assert due.amount_minor == debt_of(loan, date(2026, 10, 25))
    assert due.amount_minor < 1_800_000 - 1_000_000 + 1


async def test_user_text_is_html_escaped(h):
    await onboard(h, balance="50000")
    await add_loan(h, lender="Банк <А> & Ко", payment="5000", due="20.10", method="manual")
    [loan] = await h.all(Loan)
    await h.tap(LoanCb(action="pay", loan_id=loan.id).pack())
    raw = h.net.texts()[-1]
    assert "Банк &lt;А&gt; &amp; Ко" in raw


async def test_not_debited_is_retried_next_day(h, now):
    await onboard(h, balance="20000")
    await add_loan(h, payment="5000", due="18.10")
    now.set(datetime(2026, 10, 18, 18, 0, tzinfo=UTC))
    await h.text("19000")
    [first, *_] = await h.all(LoanDue)
    await h.tap(DueCb(action="undo", due_id=first.id).pack())
    # The bank debited it a day later: 19 000 − 5 000 − 1 000 spent
    now.set(datetime(2026, 10, 19, 18, 0, tzinfo=UTC))
    text = await h.text("13000")
    assert "Учёл автосписание: Сбер — 5 000 ₽" in text
    assert "Потрачено со вчера: 1 000 ₽" in text
    [payment] = await h.all(LoanPayment)
    assert payment.paid_on == date(2026, 10, 19)
    [first, *_] = await h.all(LoanDue)
    assert first.status == "paid"


async def onboard_hourly(h, tax="itax:13"):
    await h.text("/start")
    await h.tap("tz:Europe/Moscow")
    await h.tap("shift:2/2")
    await h.tap("spos:0")  # 17.10 is the 1st shift
    await h.tap("itype:hourly")
    await h.text("300")
    await h.text("12")
    await h.text("10 20 31")
    await h.text("10")
    preview = await h.tap(tax)
    return preview


async def test_hourly_onboarding_and_main_screen(h):
    await onboard_hourly(h)
    [user] = await h.all(User)
    assert (user.income_mode, user.hourly_rate_minor, user.shift_hours, user.tax_percent,
            user.pay_delay_days) == ("hourly", 30000, "12", "13", 10)
    rules = await h.all(IncomeRule)
    assert sorted(r.day for r in rules) == [10, 20, 31]
    preview = h.net.texts()[-2].replace(" ", " ")
    # 20.10 pays for 01.10–10.10; the pattern anchors 17.10 as the 1st shift of 2/2
    assert "Зарплата за 01.10–10.10" in preview
    assert "Сколько сейчас на карте" in h.net.last_text()
    await h.text("20000")
    text = await h.text(BTN_HOME)
    assert "Следующая выплата: Зарплата за 01.10–10.10, 20.10.2026" in text


async def test_hourly_payday_question_shows_estimate(h, now):
    await onboard_hourly(h)
    await h.text("20000")
    now.set(datetime(2026, 10, 20, 7, 0, tzinfo=UTC))
    await h.tick()
    question = h.net.sent()[-1]
    text = plain(question.text)
    # 2/2 with 17.10 as 1st shift: 1–10 October has 6 shifts (1, 2, 5, 6, 9, 10)
    assert "Зарплата за 01.10–10.10, ожидалось ≈ 18 792 ₽" in text
    assert "6 смен × 12 ч × 300 ₽ − 13%" in text
    data = question.reply_markup.inline_keyboard[0][0].callback_data
    text = await h.tap(data)
    assert "Зарплата за 01.10–10.10: пришло 18 792 ₽" in text
    [income] = await h.all(Income)
    assert income.amount_minor == 1_879_200


async def test_hourly_requires_shift_pattern(h):
    await h.text("/start")
    await h.tap("tz:Europe/Moscow")
    await h.tap("shift:none")
    text = await h.tap("itype:hourly")
    assert "нужен график смен" in text
    # Onboarding asks for the pattern again and then returns to the pay type
    await h.tap("shift:5/2")
    assert "Как вам платят?" in h.net.last_text()


async def test_edit_single_hourly_field(h):
    await onboard_hourly(h, tax="itax:0")
    await h.text("20000")
    await h.tap("settings:income")
    await h.tap("iset:tax")
    await h.tap("itax:other")
    text = await h.text("15")
    assert "налог 15%" in text
    [user] = await h.all(User)
    assert user.tax_percent == "15"
    await h.tap("iset:rate")
    text = await h.text("350")
    assert "350 ₽/ч" in text


async def test_weekend_rule_setting(h):
    await onboard(h, income="25 50000")  # 25.10 is a Sunday
    await h.tap("settings:income")
    await h.tap("iset:weekend")
    text = await h.tap("iweekend:after")
    assert "в понедельник после выходных" in text
    text = await h.text(BTN_HOME)
    assert "26.10.2026" in text


async def test_abandoned_hourly_setup_keeps_old_settings(h):
    await onboard(h, income="23 50000")
    await h.tap("settings:income")
    await h.tap("iset:all")
    await h.tap("itype:hourly")
    await h.text("300")
    await h.text("/cancel")
    [user] = await h.all(User)
    assert user.income_mode == "fixed"
    [rule] = await h.all(IncomeRule)
    assert (rule.day, rule.amount_minor) == (23, 5_000_000)
