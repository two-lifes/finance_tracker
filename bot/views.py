"""User-facing text (Russian, HTML parse mode)."""

from datetime import date, datetime
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from bot.db.models import Deployment, IncomeRule, Loan, LoanDue, User
from bot.services.budget import BalanceResult, View
from bot.services.dates import fmt_date, fmt_short, relative_days
from bot.services.deployments import STATUS_OK
from bot.services.limit import PaydayPlan
from bot.services.loan_ops import EarlyResult, debt_of, interest_now, next_due
from bot.services.loans import KIND_TITLES, LoanKind, RateUnit, Schedule
from bot.services.money import format_money, format_rub
from bot.services.obligations import Obligation, Source, State
from bot.services.plural import DAYS, LOANS, MONTHS, SHIFTS, count
from bot.services.work_calendar import Payout, Weekend, describe_periods

NEAREST_COUNT = 3
SUBS_STALE_HOURS = 3


def _local(moment: datetime, tz: str | None) -> datetime:
    return moment.astimezone(ZoneInfo(tz or "Europe/Moscow"))


def obligation_line(o: Obligation, today: date) -> str:
    title = escape(o.title)
    amount = format_rub(o.amount) if o.amount is not None else "сумма неизвестна (нет курса)"
    if o.original is not None:
        amount += f" ({format_money(*o.original)})"
    if o.state == State.OVERDUE:
        late = (today - o.date).days
        fine = f", пени {format_rub(o.penalty)}" if o.penalty else ""
        return f"🔴 {title} — {amount} — просрочен на {count(late, DAYS)}{fine}"
    if o.state == State.AWAITING:
        return f"⚠️ {title} — {amount} — ждёт оплаты (срок был {o.date:%d.%m})"
    mark = "🔁" if o.source == Source.SUB else "💳"
    auto = " · автосписание" if o.auto and o.source == Source.LOAN else ""
    return f"{mark} {fmt_short(o.date)} — {title} — {amount}{auto}"


def _subs_status(view: View, user: User, configured: bool) -> str | None:
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


def main_screen(view: View, user: User, subs_configured: bool) -> str:
    today = view.today
    lines = []
    if view.card is None or view.limit is None:
        lines.append("Пришлите остаток на карте одним числом — и я посчитаю, "
                     "сколько можно тратить в день.")
    else:
        limit = view.limit
        lines.append(f"💰 <b>Можно тратить сегодня: {format_rub(limit.per_day)}</b>")
        if limit.shortfall:
            lines.append(f"⚠️ До зарплаты не хватает {format_rub(limit.shortfall)} "
                         "на обязательные платежи.")
        if view.payday is not None:
            delayed = " (задерживается)" if view.payday.delayed else ""
            lines.append(f"Следующая выплата: {escape(view.payday.payout.label)}, "
                         f"{fmt_date(limit.horizon)}{delayed} — через {count(limit.days, DAYS)}")
        else:
            lines.append(f"Дата зарплаты не задана — считаю на {count(limit.days, DAYS)}.")
        balance = f"На карте {format_rub(view.card.amount_minor)}"
        if view.cash is not None and view.cash.amount_minor:
            balance += f" + наличные {format_rub(view.cash.amount_minor)}"
        if view.card.on_date != today:
            balance += f" (данные на {view.card.on_date:%d.%m})"
        lines.append(balance)
        lines.append(f"Обязательные до зарплаты: {format_rub(limit.mandatory)}")
        if limit.unknown_amounts:
            lines.append("⚠️ Для части подписок нет курса валюты — они не учтены.")

    late = [o for o in view.obligations if o.is_late]
    upcoming = [o for o in view.obligations if not o.is_late][:NEAREST_COUNT]
    if late or upcoming:
        lines.append("")
        lines.append("<b>Ближайшие платежи:</b>")
        lines.extend(obligation_line(o, today) for o in [*late, *upcoming])
    if view.loans_count:
        lines.append("")
        lines.append(f"Общий долг: <b>{format_rub(view.debt)}</b> "
                     f"({count(view.loans_count, LOANS)})")
    status = _subs_status(view, user, subs_configured)
    if status:
        lines.append(status)
    return "\n".join(lines)


def balance_reply(result: BalanceResult, amount: int) -> str:
    lines = [f"Записал: на карте {format_rub(amount)}."]
    report = result.report
    if report is not None:
        period = "со вчера" if report.days == 1 else f"за {count(report.days, DAYS)}"
        if report.spent >= 0:
            lines.append(f"Потрачено {period}: <b>{format_rub(report.spent)}</b>")
        else:
            lines.append(
                f"Денег стало больше, чем я ожидал, на {format_rub(-report.spent)}. "
                "Если было поступление — запишите его: <code>+5000 премия</code>."
            )
        if report.sub_payments:
            lines.append(f"Учтены списания подписок: {format_rub(report.sub_payments)}")
    for loan, due in result.settled:
        lines.append(f"Учёл автосписание: {escape(loan.lender)} — "
                     f"{format_rub(due.amount_minor)} ({due.due_date:%d.%m})")
    for loan, due in result.unconfirmed:
        left = due.amount_minor - due.paid_minor
        lines.append(f"❓ Платёж {escape(loan.lender)} {format_rub(left)} "
                     f"({due.due_date:%d.%m}) — оплачен?")
    return "\n".join(lines)


# --- loans --------------------------------------------------------------------------------


def format_percent(value: str) -> str:
    return format(Decimal(value).normalize(), "f").replace(".", ",")


def format_rate(loan: Loan) -> str:
    unit = "% в день" if loan.rate_unit == RateUnit.DAY else "% годовых"
    return f"{format_percent(loan.rate)}{unit}"


def loan_button(loan: Loan, today: date) -> str:
    return f"{loan.lender} · {format_rub(debt_of(loan, today))}"


def loan_card(loan: Loan, today: date) -> str:
    kind = LoanKind(loan.kind)
    lines = [f"<b>{escape(loan.lender)}</b> — {KIND_TITLES[kind]}"]
    if loan.status != "active":
        lines.append("✅ Закрыт")
        return "\n".join(lines)
    lines.append(f"Долг: <b>{format_rub(debt_of(loan, today))}</b>")
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
    lines.append("Оплата: " + ("автосписание" if loan.pay_method == "auto" else "вручную"))
    if loan.penalty_rate:
        lines.append(f"Пени при просрочке: {format_percent(loan.penalty_rate)}% годовых")
    due = next_due(loan)
    if due is not None:
        left = due.amount_minor - due.paid_minor
        paid = f" (внесено {format_rub(due.paid_minor)})" if due.paid_minor else ""
        lines.append(f"Ближайший платёж: {format_rub(left)} — {fmt_date(due.due_date)}, "
                     f"{relative_days(due.due_date, today)}{paid}")
    return "\n".join(lines)


def loan_draft_text(data: dict) -> str:
    kind = LoanKind(data["kind"])
    lines = ["<b>Проверьте:</b>", f"{escape(data['lender'])} — {KIND_TITLES[kind]}",
             f"Долг: {format_rub(data['principal'])}"]
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
    lines.append("Оплата: " + ("автосписание" if data["pay_method"] == "auto" else "вручную"))
    return "\n".join(lines)


def early_result_text(loan: Loan, result: EarlyResult) -> str:
    alloc = result.allocation
    lines = [f"Досрочный платёж по «{escape(loan.lender)}» записан."]
    if alloc.interest:
        lines.append(f"На проценты: {format_rub(alloc.interest)}, "
                     f"на основной долг: {format_rub(alloc.principal)}")
    if result.closed:
        lines.append("🎉 Кредит полностью погашен!")
        return "\n".join(lines)
    s = result.schedule
    if s is not None:
        if s.months_before and s.months_after is not None and s.months_after < s.months_before:
            lines.append(f"Срок сократился: осталось {count(s.months_after, MONTHS)} "
                         f"вместо {s.months_before}.")
        lines.append(f"Платёж теперь: {format_rub(s.payment_after)}")
    lines.append(f"Остаток долга: {format_rub(loan.principal_minor)}")
    return "\n".join(lines)


def loan_reminder(loan: Loan, due: LoanDue, today: date) -> str:
    left = due.amount_minor - due.paid_minor
    when = relative_days(due.due_date, today)
    head = f"⏰ {when.capitalize()} ({fmt_date(due.due_date)}) платёж: " \
           f"<b>{escape(loan.lender)}</b> — {format_rub(left)}"
    if loan.pay_method == "auto":
        return head + "\nСпишется автоматически — проверьте, что на карте хватает денег."
    return head + "\nОплата вручную. Когда оплатите — нажмите кнопку."


BALANCE_REMINDER = "Вечер — пришлите остаток на карте одним числом, и я посчитаю траты за день."


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
        extra = f" ({breakdown})" if breakdown else ""
        lines.append(f"• {fmt_short(p.scheduled)} — {escape(p.label)} ≈ "
                     f"{format_rub(p.amount)}{extra}")
    return "\n".join(lines)


def payday_question(payout: Payout, user: User, today: date) -> str:
    when = "Сегодня" if payout.scheduled == today else f"{fmt_date(payout.scheduled)} был"
    text = (f"💵 {when} день выплаты: {escape(payout.label)}, "
            f"ожидалось ≈ {format_rub(payout.amount)}")
    breakdown = payout_breakdown(payout, user)
    if breakdown:
        text += f" ({breakdown})"
    return text + ". Деньги пришли?"


def payday_summary(title: str, plan: PaydayPlan, today: date) -> str:
    lines = [f"💵 {escape(title)}: пришло {format_rub(plan.received)}."]
    if plan.items:
        lines.append(f"Сразу отложите <b>{format_rub(plan.set_aside)}</b> на платежи до "
                     f"{fmt_date(plan.next_horizon)}:")
        lines.extend(obligation_line(o, today) for o in plan.items)
        if plan.unknown_amounts:
            lines.append("⚠️ Часть подписок без курса валюты — не учтены.")
    else:
        lines.append(f"До {fmt_date(plan.next_horizon)} обязательных платежей нет.")
    if plan.free < 0:
        lines.append(f"⚠️ Выплаты не хватает на платежи: минус {format_rub(-plan.free)}.")
    return "\n".join(lines)


# --- settings -----------------------------------------------------------------------------


WEEKEND_TITLES = {
    Weekend.BEFORE: "в пятницу перед выходными",
    Weekend.AFTER: "в понедельник после выходных",
    Weekend.SAME: "в тот же день",
}


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
    return (
        "<b>Настройки</b>\n"
        f"Часовой пояс: {user.timezone or 'не задан'}\n"
        f"График: {shifts}\n"
        f"Выплаты: {income_text(user, rules)}\n"
        f"Напоминания о платежах: в {user.remind_time:%H:%M}\n"
        f"Напоминание про остаток: в {user.evening_time:%H:%M}"
    )


def deployment_text(d: Deployment) -> str:
    if d.status == STATUS_OK:
        title = f"\n{escape(d.title)}" if d.title else ""
        return f"🔄 Бот обновлён до версии <code>{d.version[:7]}</code>{title}"
    return (f"⚠️ Обновление <code>{d.version[:7]}</code> не запустилось, "
            "бот вернулся на предыдущую версию.")
