"""User-facing text (Russian, HTML parse mode).

Layout: a bold headline with the key number, details grouped in <blockquote> blocks,
long explanations in an expandable blockquote, blocks separated by an empty line.
"""

from datetime import date, datetime
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from bot.db.models import Deployment, Income, IncomeRule, Loan, LoanDue, User
from bot.services.budget import BalanceResult, View
from bot.services.dates import WEEKDAYS_SHORT, fmt_date, fmt_words, relative_days
from bot.services.deployments import STATUS_OK
from bot.services.forecast import Shortfall
from bot.services.limit import PaydayPlan
from bot.services.loan_ops import EarlyResult, debt_of, interest_now, next_due
from bot.services.loans import KIND_TITLES, LoanKind, RateUnit, Schedule
from bot.services.money import format_money, format_rub
from bot.services.obligations import Obligation, Source, State
from bot.services.plural import DAYS, LOANS, MONTHS, SHIFTS, count
from bot.services.priority import Group, group_of
from bot.services.strategy import Advice
from bot.services.subscriptions import MonthlyCost
from bot.services.work_calendar import Payout, Weekend, describe_periods

NEAREST_COUNT = 3
SUBS_STALE_HOURS = 3

KIND_ICONS = {
    LoanKind.BANK: "🏦",
    LoanKind.MFO: "⚡",
    LoanKind.CARD: "💳",
    LoanKind.INSTALLMENT: "🛍",
}


def _local(moment: datetime, tz: str | None) -> datetime:
    return moment.astimezone(ZoneInfo(tz or "Europe/Moscow"))


def quote(lines: list[str], expandable: bool = False) -> str:
    """A grey block; expandable ones show a few lines until tapped."""
    items = [line for line in lines if line]
    if not items:
        return ""
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return tag + "\n".join(items) + "</blockquote>"


def blocks(*parts: str | None) -> str:
    return "\n\n".join(p for p in parts if p)


def day_label(d: date) -> str:
    """'пн 19.10'"""
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d:%d.%m}"


def obligation_line(o: Obligation, today: date) -> str:
    title = escape(o.title)
    if o.amount is not None:
        amount = f"<b>{format_rub(o.amount)}</b>"
    else:
        amount = "сумма неизвестна (нет курса)"
    if o.original is not None:
        amount += f" ({format_money(*o.original)})"
    if o.state == State.OVERDUE:
        late = (today - o.date).days
        fine = f", пени {format_rub(o.penalty)}" if o.penalty else ""
        return f"🔴 {title} — {amount} · просрочен на {count(late, DAYS)}{fine}"
    if o.state == State.AWAITING:
        return f"⚠️ {title} — {amount} · ждёт оплаты с {o.date:%d.%m}"
    mark = "🔁" if o.source == Source.SUB else "💳"
    auto = " · ⟳ авто" if o.auto and o.source == Source.LOAN else ""
    return f"{mark} {day_label(o.date)} · {title} — {amount}{auto}"


def subs_status(view: View, user: User, configured: bool) -> str | None:
    if not configured:
        return None
    subs = view.subs
    if subs.fetched_at is None:
        return "⚠️ Подписки пока не загружены: сервис подписок недоступен."
    when = _local(subs.fetched_at, user.timezone)
    stamp = f"{when:%d.%m %H:%M}"
    if subs.last_error:
        return f"⚠️ Сервис подписок недоступен, данные на {stamp}."
    return f"<i>Подписки обновлены {stamp}</i>"


def _money_block(view: View) -> str:
    limit, today = view.limit, view.today
    card = f"💳 На карте <b>{format_rub(view.card.amount_minor)}</b>"
    if view.cash is not None and view.cash.amount_minor:
        card += f" + наличные <b>{format_rub(view.cash.amount_minor)}</b>"
    if view.card.on_date != today:
        card += f" <i>(данные на {view.card.on_date:%d.%m})</i>"
    lines = [card, f"📌 Обязательные до зарплаты: <b>{format_rub(limit.mandatory)}</b>"]
    if view.payday is not None:
        delayed = " (задерживается)" if view.payday.delayed else ""
        lines.append(f"💵 Следующая выплата: {escape(view.payday.payout.label)}, "
                     f"{fmt_date(limit.horizon)}{delayed} — через {count(limit.days, DAYS)}")
    else:
        lines.append(f"💵 Дата зарплаты не задана — считаю на {count(limit.days, DAYS)}")
    if limit.unknown_amounts:
        lines.append("⚠️ Для части подписок нет курса валюты — они не учтены.")
    return quote(lines)


def main_screen(view: View, user: User, subs_configured: bool) -> str:
    today = view.today
    parts = []
    if view.card is None or view.limit is None:
        parts.append("Пришлите остаток на карте одним числом — и я посчитаю, "
                     "сколько можно тратить в день.")
    else:
        limit = view.limit
        parts.append(f"💰 Можно тратить сегодня: <b>{format_rub(limit.per_day)}</b>")
        parts.append(_money_block(view))
        if view.shortfall is not None:
            parts.append(shortfall_banner(view.shortfall, today) + "\nЧто делать → /shortfall")
        elif limit.shortfall:
            parts.append(f"⚠️ <b>До зарплаты не хватает {format_rub(limit.shortfall)}</b> "
                         "на обязательные платежи.")
        if view.advice is not None:
            parts.append(advice_line(view.advice)
                         + "\n<i>«💳 Кредиты» → кредит → «⏩ Досрочно»</i>")

    late = [o for o in view.obligations if o.is_late]
    upcoming = [o for o in view.obligations if not o.is_late][:NEAREST_COUNT]
    if late or upcoming:
        parts.append("📅 <b>Ближайшие платежи</b>\n"
                     + quote([obligation_line(o, today) for o in [*late, *upcoming]]))
    debt = []
    if view.loans_count:
        debt.append(f"📉 Общий долг: <b>{format_rub(view.debt)}</b> · "
                    f"{count(view.loans_count, LOANS)}")
    if view.closed_loans:
        debt.append(progress_line(view.closed_loans, view.total_loans))
    parts.append("\n".join(debt))
    parts.append(subs_status(view, user, subs_configured))
    return blocks(*parts)


# --- shortfall and strategy ---------------------------------------------------------------


def shortfall_banner(shortfall: Shortfall, today: date) -> str:
    when = "Сегодня" if shortfall.day == today else fmt_words(shortfall.day)
    return f"⚠️ <b>{when} не хватит {format_rub(shortfall.amount)}</b> на платежи."


def shortfall_notice(shortfall: Shortfall, today: date) -> str:
    titles = ", ".join(escape(o.title) for o in shortfall.items[:3])
    return blocks(
        shortfall_banner(shortfall, today),
        quote([f"Под угрозой: {titles}."]) if titles else None,
        "Разберёмся, что платить первым и что можно перенести.",
    )


GROUP_ADVICE = {
    Group.URGENT: "платите первым: дневная ставка и пени растут быстрее всего. Если не "
                  "успеваете — позвоните кредитору до даты платежа и спросите про "
                  "пролонгацию (обычно платите только проценты, срок сдвигается).",
    Group.BANK: "позвоните в банк до даты платежа: попросите перенос или реструктуризацию, "
                "спросите про кредитные каникулы. Условия уточняйте у банка.",
    Group.CARD: "внесите хотя бы минимальный платёж — иначе просрочка и штраф.",
    Group.INSTALLMENT: "уточните у банка или магазина перенос: при просрочке рассрочка "
                       "может стать кредитом с процентами.",
    Group.SUBSCRIPTION: "можно отменить или поставить на паузу в боте подписок.",
}


def shortfall_screen(view: View, ranked: list[Obligation], cancel: list[Obligation]) -> str:
    today, shortfall = view.today, view.shortfall
    if shortfall is None:
        return "✅ На ближайшие дни денег на обязательные платежи хватает."
    order = quote([f"<b>{i}.</b> {obligation_line(o, today)}"
                   for i, o in enumerate(ranked, 1)])
    advice = quote([f"<b>{escape(o.title)}</b>: {GROUP_ADVICE[group_of(o, view.terms)]}"
                    for o in ranked], expandable=True)
    return blocks(
        shortfall_banner(shortfall, today),
        "📋 <b>Что платить в первую очередь</b>\n" + order,
        "💬 <b>Что можно сделать</b>\n" + advice,
        ("✂️ <b>Подписки, которые можно отменить или отложить</b>\n"
         + quote([obligation_line(o, today) for o in cancel])) if cancel else None,
        "<i>Это подсказки, а не юридическая консультация: условия переноса и "
        "каникул зависят от кредитора.</i>",
    )


def advice_line(advice: Advice) -> str:
    t = advice.target
    return (f"💡 Свободно ≈ <b>{format_rub(advice.free)}</b> — выгоднее всего гасить досрочно "
            f"«{escape(t.title)}» ({format_percent(str(t.annual_rate))}% годовых)")


def progress_line(closed: int, total: int) -> str:
    return f"🏁 Закрыто кредитов: <b>{closed} из {total}</b>"


def closed_text(closed: int, total: int) -> str:
    return f"🎉 <b>Кредит полностью погашен!</b> Закрыто кредитов: {closed} из {total}."


def balance_reply(result: BalanceResult, amount: int) -> str:
    lines = []
    report = result.report
    if report is not None:
        period = "со вчера" if report.days == 1 else f"за {count(report.days, DAYS)}"
        if report.spent >= 0:
            lines.append(f"🛒 Потрачено {period}: <b>{format_rub(report.spent)}</b>")
        else:
            lines.append(
                f"📈 Денег стало больше, чем я ожидал, на {format_rub(-report.spent)}. "
                "Если было поступление — запишите его: <code>+5000 премия</code>."
            )
        if report.sub_payments:
            lines.append(f"🔁 Учтены списания подписок: {format_rub(report.sub_payments)}")
    for loan, due in result.settled:
        lines.append(f"⟳ Учёл автосписание: {escape(loan.lender)} — "
                     f"{format_rub(due.amount_minor)} ({due.due_date:%d.%m})")
    for loan, due in result.unconfirmed:
        left = due.amount_minor - due.paid_minor
        lines.append(f"❓ Платёж {escape(loan.lender)} {format_rub(left)} "
                     f"({due.due_date:%d.%m}) — оплачен?")
    return blocks(f"✅ Записал: на карте <b>{format_rub(amount)}</b>", quote(lines))


# --- loans --------------------------------------------------------------------------------


def format_percent(value: str) -> str:
    return format(Decimal(value).normalize(), "f").replace(".", ",")


def format_rate(loan: Loan) -> str:
    unit = "% в день" if loan.rate_unit == RateUnit.DAY else "% годовых"
    return f"{format_percent(loan.rate)}{unit}"


def loan_button(loan: Loan, today: date) -> str:
    return f"{KIND_ICONS[LoanKind(loan.kind)]} {loan.lender} · {format_rub(debt_of(loan, today))}"


def loan_card(loan: Loan, today: date) -> str:
    kind = LoanKind(loan.kind)
    head = f"{KIND_ICONS[kind]} <b>{escape(loan.lender)}</b> · {KIND_TITLES[kind]}"
    if loan.status != "active":
        return blocks(head, "✅ Закрыт")
    lines = [f"Долг: <b>{format_rub(debt_of(loan, today))}</b>"]
    interest = interest_now(loan, today)
    if interest:
        lines.append(f"  из них проценты: {format_rub(interest)}")
    if kind != LoanKind.INSTALLMENT:
        lines.append(f"Ставка: {format_rate(loan)}")
    if loan.schedule == Schedule.ONCE:
        lines.append(f"Вернуть {format_rub(loan.payment_minor)} до {fmt_date(loan.first_due)}")
    else:
        label = "Минимальный платёж" if kind == LoanKind.CARD else "Платёж"
        lines.append(f"{label}: {format_rub(loan.payment_minor)}, {loan.payment_day}-го числа")
    if loan.insurance_minor:
        lines.append(f"  в т. ч. страховка/комиссии: {format_rub(loan.insurance_minor)}")
    lines.append("Оплата: " + ("⟳ автосписание" if loan.pay_method == "auto" else "вручную"))
    if loan.penalty_rate:
        lines.append(f"Пени при просрочке: {format_percent(loan.penalty_rate)}% годовых")
    nearest = None
    due = next_due(loan)
    if due is not None:
        left = due.amount_minor - due.paid_minor
        paid = f" <i>(внесено {format_rub(due.paid_minor)})</i>" if due.paid_minor else ""
        nearest = (f"📅 Ближайший платёж: <b>{format_rub(left)}</b> — {fmt_date(due.due_date)}, "
                   f"{relative_days(due.due_date, today)}{paid}")
    return blocks(head, quote(lines), nearest)


def loan_draft_text(data: dict) -> str:
    kind = LoanKind(data["kind"])
    lines = [f"{KIND_ICONS[kind]} <b>{escape(data['lender'])}</b> · {KIND_TITLES[kind]}",
             f"Долг: <b>{format_rub(data['principal'])}</b>"]
    if kind != LoanKind.INSTALLMENT:
        unit = "% в день" if data["rate_unit"] == RateUnit.DAY else "% годовых"
        lines.append(f"Ставка: {format_percent(data['rate'])}{unit}")
    first_due = date.fromisoformat(data["first_due"])
    if kind == LoanKind.MFO:
        lines.append(f"Вернуть {format_rub(data['payment'])} до {fmt_date(first_due)}")
    else:
        lines.append(f"Платёж: {format_rub(data['payment'])}, ближайший {fmt_date(first_due)}")
    if data.get("insurance"):
        lines.append(f"  в т. ч. страховка/комиссии: {format_rub(data['insurance'])}")
    lines.append("Оплата: " + ("⟳ автосписание" if data["pay_method"] == "auto" else "вручную"))
    return blocks("📝 <b>Проверьте:</b>", quote(lines))


def early_result_text(loan: Loan, result: EarlyResult, progress: tuple[int, int]) -> str:
    alloc = result.allocation
    head = f"⏩ Досрочный платёж по «{escape(loan.lender)}» записан."
    lines = []
    if alloc.interest:
        lines.append(f"На проценты: {format_rub(alloc.interest)}, "
                     f"на основной долг: {format_rub(alloc.principal)}")
    if result.closed:
        return blocks(head, quote(lines), closed_text(*progress))
    s = result.schedule
    if s is not None:
        if s.months_before and s.months_after is not None and s.months_after < s.months_before:
            lines.append(f"Срок сократился: осталось {count(s.months_after, MONTHS)} "
                         f"вместо {s.months_before}.")
        lines.append(f"Платёж теперь: <b>{format_rub(s.payment_after)}</b>")
    lines.append(f"Остаток долга: <b>{format_rub(loan.principal_minor)}</b>")
    return blocks(head, quote(lines))


def loan_reminder(loan: Loan, due: LoanDue, today: date) -> str:
    left = due.amount_minor - due.paid_minor
    when = relative_days(due.due_date, today)
    head = (f"⏰ <b>{when.capitalize()} платёж</b> ({fmt_date(due.due_date)})\n"
            f"{escape(loan.lender)} — <b>{format_rub(left)}</b>")
    if loan.pay_method == "auto":
        return blocks(head, "<i>Спишется автоматически — проверьте, что на карте хватает "
                            "денег.</i>")
    return blocks(head, "<i>Оплата вручную. Когда оплатите — нажмите кнопку.</i>")


BALANCE_REMINDER = "🌙 Вечер — запишем остатки на карте и наличными, и я посчитаю траты за день."
ASK_CARD = ("💳 Сколько сейчас на карте? Пришлите число, например <code>45000</code> "
            "или <code>45 000,50</code>.\n/cancel — отмена")
ASK_CASH = ("💵 Сколько наличных? Например <code>5000</code>, или <code>0</code>, "
            "если их нет.\n/cancel — отмена")


def payout_breakdown(payout: Payout, user: User) -> str:
    """'5 смен × 12 ч × 300 ₽ − 13%' for hourly pay; empty for fixed."""
    if payout.shifts is None or user.hourly_rate_minor is None:
        return ""
    tax = Decimal(user.tax_percent or "0")
    tax_part = f" − {format_percent(user.tax_percent)}%" if tax else ""
    return (f"{count(payout.shifts, SHIFTS)} × {format_percent(user.shift_hours or '0')} ч × "
            f"{format_rub(user.hourly_rate_minor)}{tax_part}")


def payout_lines(items: list[Payout], user: User) -> str:
    lines = []
    for p in items:
        breakdown = payout_breakdown(p, user)
        extra = f"\n   <i>{breakdown}</i>" if breakdown else ""
        lines.append(f"💵 {day_label(p.scheduled)} · {escape(p.label)} ≈ "
                     f"<b>{format_rub(p.amount)}</b>{extra}")
    return quote(lines)


def payday_question(payout: Payout, user: User, today: date) -> str:
    when = "Сегодня" if payout.scheduled == today else f"{fmt_date(payout.scheduled)} был"
    text = (f"💵 <b>{when} день выплаты</b>\n"
            f"{escape(payout.label)}, ожидалось ≈ <b>{format_rub(payout.amount)}</b>")
    breakdown = payout_breakdown(payout, user)
    if breakdown:
        text += f"\n<i>{breakdown}</i>"
    return blocks(text, "Деньги пришли?")


def payday_summary(title: str, plan: PaydayPlan, today: date) -> str:
    head = f"💵 <b>{escape(title)}: пришло {format_rub(plan.received)}</b>"
    if plan.items:
        items = [obligation_line(o, today) for o in plan.items]
        if plan.unknown_amounts:
            items.append("⚠️ Часть подписок без курса валюты — не учтены.")
        aside = (f"📌 Сразу отложите <b>{format_rub(plan.set_aside)}</b> на платежи до "
                 f"{fmt_date(plan.next_horizon)}:\n" + quote(items))
    else:
        aside = f"До {fmt_date(plan.next_horizon)} обязательных платежей нет."
    short = None
    if plan.free < 0:
        short = f"⚠️ <b>Выплаты не хватает на платежи: минус {format_rub(-plan.free)}</b>"
    return blocks(head, aside, short)


# --- settings -----------------------------------------------------------------------------


WEEKEND_TITLES = {
    Weekend.BEFORE: "в пятницу перед выходными",
    Weekend.AFTER: "в понедельник после выходных",
    Weekend.SAME: "в тот же день",
}


def income_line(income: Income) -> str:
    comment = f" · {escape(income.comment)}" if income.comment else ""
    return f"{day_label(income.on_date)} · <b>{format_rub(income.amount_minor)}</b>{comment}"


def incomes_delete_text(incomes: list[Income], *, all_time: bool) -> str:
    title = "Все поступления" if all_time else "Поступления за последнюю неделю"
    if not incomes:
        empty = "Поступлений пока нет." if all_time else "За последнюю неделю поступлений нет."
        return f"🗑 <b>{title}</b>\n\n{empty}"
    lines = [f"<b>{n}.</b> {income_line(row)}" for n, row in enumerate(incomes, 1)]
    return blocks(
        f"🗑 <b>{title}</b>",
        quote(lines),
        "Напишите номер поступления, которое удалить.\n/cancel — отмена",
    )


def income_text(user: User, rules: list[IncomeRule]) -> str:
    """Pay settings in a few lines."""
    weekend = WEEKEND_TITLES[Weekend(user.weekend_shift or Weekend.BEFORE)]
    if user.income_mode == "hourly":
        cuts = sorted({r.day for r in rules})
        tax = Decimal(user.tax_percent or "0")
        rate = (f"{format_rub(user.hourly_rate_minor)}/ч" if user.hourly_rate_minor is not None
                else "не задана")
        lines = [
            "почасовая",
            f"  ставка: {rate}" + (f", налог {format_percent(user.tax_percent)}%" if tax
                                   else ", на руки"),
            "  смена: " + (f"≈ {format_percent(user.shift_hours)} ч" if user.shift_hours
                           else "не задана"),
            "  периоды: " + (describe_periods(cuts) if cuts else "не заданы"),
            "  выплата: " + (f"через {count(user.pay_delay_days, DAYS)} после конца периода"
                             if user.pay_delay_days is not None else "не задана"),
        ]
        if not user.shift_work:
            lines.append("  ⚠️ для почасовой нужен график смен")
    elif rules:
        lines = ["; ".join(f"{escape(r.title)} {r.day}-го ≈ {format_rub(r.amount_minor)}"
                           for r in rules)]
    else:
        return "не заданы"
    lines.append(f"  в выходной: {weekend}")
    return "\n".join(lines)


def settings_text(user: User, rules: list[IncomeRule]) -> str:
    shifts = f"{user.shift_work}/{user.shift_off}" if user.shift_work else "не задан"
    return blocks(
        "⚙️ <b>Настройки</b>",
        quote([
            f"🌍 Часовой пояс: {user.timezone or 'не задан'}",
            f"📆 График: {shifts}",
            f"💵 Выплаты: {income_text(user, rules)}",
        ]),
        quote([
            f"🔔 Напоминания о платежах: в {user.remind_time:%H:%M}",
            f"🌙 Напоминание про остаток: в {user.evening_time:%H:%M}",
        ]),
        quote([
            f"🛟 Резерв на жизнь: {format_rub(user.reserve_per_day_minor)} в день",
            f"💡 Советовать досрочку: от {format_rub(user.prepay_threshold_minor)}",
        ]),
    )


def deployment_text(d: Deployment, when: str | None = None) -> str:
    """``when``: '2 часа назад', for the history; a fresh notice goes without it."""
    stamp = f" · <i>{when}</i>" if when else ""
    if d.status == STATUS_OK:
        title = f"\n{escape(d.title)}" if d.title else ""
        return f"🔄 Бот обновлён до версии <code>{d.version[:7]}</code>{stamp}{title}"
    return (f"⚠️ Обновление <code>{d.version[:7]}</code> не запустилось{stamp}\n"
            "Бот вернулся на предыдущую версию.")


# --- subscriptions ------------------------------------------------------------------------

TOP_SUBS = 3


def subs_screen(costs: list[MonthlyCost], income: int, loan_payments: int,
                cancel: list[Obligation], status: str | None, today: date) -> str:
    parts = []
    if not costs:
        parts.append("🔁 Активных подписок нет.")
    else:
        known = [c.rub_minor for c in costs if c.rub_minor is not None]
        monthly = sum(known)
        parts.append(f"🔁 <b>Подписки</b>: {len(costs)}, ≈ <b>{format_rub(monthly)}</b> в месяц")
        info = []
        disposable = income - loan_payments
        if income and disposable > 0:
            share = monthly * 100 / disposable
            info.append(f"📊 Это <b>{share:.0f}%</b> располагаемого дохода")
            info.append(f"<i>{format_rub(income)} в месяц − кредиты "
                        f"{format_rub(loan_payments)}</i>")
        elif income:
            info.append("📊 Платежи по кредитам съедают весь доход — подписки сверху.")
        else:
            info.append("📊 Доход не настроен — долю от дохода не посчитать.")
        if len(known) != len(costs):
            info.append("⚠️ Для части подписок нет курса валюты — они не учтены.")
        parts.append(quote(info))
        top = []
        for c in costs[:TOP_SUBS]:
            amount = (f"<b>{format_rub(c.rub_minor)}</b>" if c.rub_minor is not None
                      else "нет курса")
            original = ""
            if c.sub.currency != "RUB":
                original = f" ({format_money(c.sub.amount_minor, c.sub.currency)})"
            top.append(f"• {escape(c.sub.name)} — {amount} в месяц{original}")
        parts.append("🏷 <b>Самые дорогие</b>\n" + quote(top))
    if cancel:
        parts.append("⚠️ <b>Денег на ближайшие платежи не хватает.</b> Можно отменить или "
                     "отложить:\n" + quote([obligation_line(o, today) for o in cancel]))
    parts.append(status)
    return blocks(*parts)
