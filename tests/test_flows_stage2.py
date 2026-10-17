"""Stage 2 dialogs: shortfall, early repayment advice, closed-loans counter, subscriptions."""

from datetime import UTC, datetime

from bot.db.models import Deployment, Loan, Notification, User
from bot.keyboards import BTN_HOME, BTN_LOANS, BTN_SUBS, LoanCb
from tests.harness import onboard, plain
from tests.test_flows import add_loan, sub_json


def shortfall_texts(h) -> list[str]:
    # Notices only, not the main screen banner
    return [plain(t) for t in h.net.texts() if "Под угрозой" in t]


async def test_shortfall_banner_and_screen(h):
    await onboard(h, balance="20000")
    await add_loan(h, lender="Сбер", payment="25000", due="19.10")
    await add_loan(h, lender="Займер", kind="mfo", principal="5000", rate="0,8",
                   payment="6000", due="20.10", method="manual")
    text = await h.text(BTN_HOME)
    assert "19 октября не хватит 11 000 ₽" in text  # 20 000 − 25 000 − 6 000
    assert "/shortfall" in text
    assert "Свободно" not in text

    text = await h.text("/shortfall")
    # МФО first: daily rate, even though its date is later
    assert text.index("Займер") < text.index("Сбер")
    assert "пролонгацию" in text and "реструктуризацию" in text


async def test_no_shortfall_screen(h):
    await onboard(h, balance="20000")
    assert "хватает" in await h.text("/shortfall")


async def test_shortfall_notification_only_on_change(h, now):
    await onboard(h, balance="20000")
    await add_loan(h, lender="Сбер", payment="25000", due="19.10")
    await h.tick()
    assert len(shortfall_texts(h)) == 1
    assert "Под угрозой: Сбер" in shortfall_texts(h)[0]
    await h.tick()
    assert len(shortfall_texts(h)) == 1

    # Small daily spending: still the same notice
    await h.text("19500")
    await h.tick()
    assert len(shortfall_texts(h)) == 1

    # The gap grew noticeably
    await h.text("15000")
    await h.tick()
    assert len(shortfall_texts(h)) == 2
    assert "10 000 ₽" in shortfall_texts(h)[-1]

    await h.tap("short:plan")
    assert "Что платить в первую очередь" in h.last_plain()


async def test_shortfall_notice_skipped_when_resolved(h, now):
    await onboard(h, balance="20000")
    await add_loan(h, lender="Сбер", payment="25000", due="19.10")
    # Before the reminder time nothing is planned
    now.set(datetime(2026, 10, 17, 5, 0, tzinfo=UTC))
    await h.tick()
    assert shortfall_texts(h) == []
    await h.text("40000")
    now.set(datetime(2026, 10, 17, 8, 0, tzinfo=UTC))
    await h.tick()
    assert shortfall_texts(h) == []
    assert not [n for n in await h.all(Notification) if n.kind == "shortfall"]


async def test_prepay_advice_on_main_screen(h):
    await onboard(h, balance="20000")
    await add_loan(h, lender="Сбер", payment="1000", due="20.10")
    await add_loan(h, lender="Займер", kind="mfo", principal="3000", rate="0,8",
                   payment="3500", due="30.10", method="manual")
    text = await h.text(BTN_HOME)
    # 20 000 − 1 000 (Сбер by payday 23.10) − 1 000 × 6 days reserve = 13 000, capped by the
    # debt of the costliest loan: МФО, 3 000 + interest so far
    assert "Свободно ≈ 3 188 ₽" in text and "«Займер»" in text and "292% годовых" in text


async def test_prepay_threshold_and_reserve_settings(h):
    await onboard(h, balance="20000")
    await add_loan(h, lender="Сбер", payment="1000", due="20.10")
    await h.tap("settings:reserve")
    text = await h.text("3000")
    assert "Резерв на жизнь: 3 000 ₽ в день" in text
    [user] = await h.all(User)
    assert user.reserve_per_day_minor == 300_000
    # 20 000 − 1 000 − 3 000 × 6 = 1 000: below a 5 000 threshold
    await h.tap("settings:threshold")
    await h.text("5000")
    assert "Свободно" not in await h.text(BTN_HOME)
    await h.tap("settings:threshold")
    await h.text("1000")
    assert "Свободно ≈ 1 000 ₽" in await h.text(BTN_HOME)


async def test_prepay_advice_in_payday_summary(h):
    await onboard(h, balance="20000")
    await add_loan(h, lender="Сбер", payment="1000", due="20.10")
    await h.text("➕ Доход")
    await h.tap("inc:recv:1:2026-10-23")
    text = await h.text("50000")
    assert "Свободно ≈" in text and "«Сбер»" in text
    markup = h.net.sent()[-1].reply_markup
    [[button]] = markup.inline_keyboard
    assert button.callback_data == LoanCb(action="early", loan_id=1).pack()


async def test_closed_loans_counter(h):
    await onboard(h)
    await add_loan(h, lender="Сбер")
    await add_loan(h, lender="Тинькофф")
    await h.tap(LoanCb(action="close_yes", loan_id=1).pack())
    assert "Закрыто кредитов: 1 из 2" in h.last_plain()
    assert "Закрыто кредитов: 1 из 2" in await h.text(BTN_LOANS)
    assert "Закрыто кредитов: 1 из 2" in await h.text(BTN_HOME)
    [closed, _] = await h.all(Loan)
    assert closed.status == "closed"


async def test_subscriptions_screen(h):
    await onboard(h, balance="20000")
    h.subs.subs = [
        sub_json(1, "Claude Code", 2000, "USD", "2026-10-20"),
        sub_json(2, "Кинопоиск", 29900, "RUB", "2026-10-25"),
        {**sub_json(3, "Облако", 120000, "RUB", "2027-01-01"), "period_unit": "year"},
        sub_json(4, "Музыка", 16900, "RUB", "2026-10-28"),
        sub_json(5, "Старое", 99900, "RUB", "2026-10-28", status="paused"),
    ]
    text = await h.text(BTN_SUBS)
    # 1 600 + 299 + 100 + 169
    assert "4, ≈ 2 168 ₽ в месяц" in text
    assert "4% располагаемого дохода" in text  # of 50 000
    top = text[text.index("Самые дорогие"):]
    assert top.index("Claude Code") < top.index("Кинопоиск") < top.index("Музыка")
    assert "Облако" not in top and "Старое" not in text
    assert "20 $" in top


async def test_updates_history_shows_when(h):
    await onboard(h)
    async with h.sessionmaker() as session, session.begin():
        session.add_all([
            Deployment(version="a" * 40, title="Stage 1", status="ok",
                       created_at=datetime(2026, 10, 16, 12, 50, tzinfo=UTC),
                       notified_at=datetime(2026, 10, 16, 12, 51, tzinfo=UTC)),
            Deployment(version="b" * 40, title="Stage 2", status="ok",
                       created_at=datetime(2026, 10, 17, 6, 0, tzinfo=UTC),
                       notified_at=datetime(2026, 10, 17, 6, 1, tzinfo=UTC)),
        ])
    text = await h.tap("settings:updates")
    assert text.index("bbbbbbb · 1 час назад") < text.index("aaaaaaa · вчера в 15:50")
    assert "Stage 2" in text
