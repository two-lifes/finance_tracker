"""How the user is paid: fixed paydays or hourly pay. Used in onboarding and settings."""

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.clock import user_today
from bot.db.models import User
from bot.handlers.common import show
from bot.handlers.flow import after_income, ask_shifts, is_onboarding
from bot.keyboards import (
    income_settings_keyboard,
    income_type_keyboard,
    tax_keyboard,
    weekend_keyboard,
)
from bot.services.budget import income_rules, pay_schedule, save_rules, work_pattern
from bot.services.money import parse_rub
from bot.services.work_calendar import describe_periods, parse_cuts, payouts
from bot.views import income_text, payout_lines, quote

router = Router(name="pay_setup")

MAX_RULES = 4
MAX_TITLE = 50
MAX_DELAY_DAYS = 60
HOURLY_TITLE = "Зарплата"
PREVIEW_DAYS = 45


class PaySetup(StatesGroup):
    kind = State()
    fixed = State()
    rate = State()
    hours = State()
    cuts = State()
    delay = State()
    tax = State()
    tax_other = State()


TYPE_PROMPT = "Как вам платят?"
FIXED_PROMPT = (
    "Когда приходят деньги? Пришлите число месяца и примерную сумму, каждую выплату — "
    "отдельной строкой:\n<code>10 25000 аванс\n25 30000 зарплата</code>\n"
    "Точную сумму я спрошу в день выплаты."
)
RATE_PROMPT = "Ставка за час, ₽? Например <code>300</code>."
HOURS_PROMPT = ("Сколько часов в смене, примерно? Например <code>12</code> или "
                "<code>11,5</code>. Точно не нужно — сумму я уточню в день выплаты.")
CUTS_PROMPT = (
    "За какие периоды платят? Пришлите числа, которыми периоды заканчиваются:\n"
    "<code>10 20 31</code> — три периода: 1–10, 11–20, 21–конец месяца;\n"
    "<code>15 31</code> — две половины месяца;\n"
    "<code>31</code> — за весь месяц.\n"
    "31 означает последний день месяца."
)
DELAY_PROMPT = ("Через сколько дней после конца периода приходят деньги? Например "
                "<code>10</code> — за 1–10 число придут 20-го. <code>0</code> — в последний "
                "день периода.")
TAX_PROMPT = "Ставка указана на руки или до вычета налога?"

_RULE_RE = re.compile(r"^(\d{1,2})\s*[-–—:.]?\s+(.+)$")
_TITLE_RE = re.compile(r"^(.*?\d(?:\s*(?:к|k|тыс\.?))?)\s+([^\d\s].*)$", re.IGNORECASE)


def parse_rules(text: str) -> list[tuple[int, int, str | None]] | None:
    """'10 25000 аванс' lines -> [(day, amount, title or None)]."""
    result = []
    for line in filter(None, (raw.strip() for raw in text.splitlines())):
        match = _RULE_RE.match(line)
        if not match:
            return None
        day, rest = int(match.group(1)), match.group(2).strip()
        title = None
        titled = _TITLE_RE.match(rest)
        if titled and parse_rub(titled.group(1)) is not None:
            rest, title = titled.group(1), titled.group(2).strip()[:MAX_TITLE]
        amount = parse_rub(rest, allow_zero=False)
        if not 1 <= day <= 31 or amount is None:
            return None
        result.append((day, amount, title))
    if not result or len(result) > MAX_RULES:
        return None
    return result


def default_titles(count: int) -> list[str]:
    if count == 1:
        return ["Зарплата"]
    if count == 2:
        return ["Аванс", "Зарплата"]
    return [f"Выплата {i + 1}" for i in range(count)]


def parse_decimal(text: str, low: Decimal, high: Decimal) -> Decimal | None:
    cleaned = text.strip().replace(",", ".").rstrip("%").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if low <= value <= high and value.is_finite() else None


async def ask_income_type(message: Message, state: FSMContext) -> None:
    await state.set_state(PaySetup.kind)
    await message.answer(TYPE_PROMPT, reply_markup=income_type_keyboard())


async def _single(state: FSMContext) -> bool:
    """True when one field is edited from the settings: save it and return there."""
    return bool((await state.get_data()).get("single"))


async def _income_view(message: Message, state: FSMContext, session: AsyncSession,
                       user: User, edit: CallbackQuery | None = None) -> None:
    await state.clear()
    text = "💵 <b>Выплаты</b>\n" + quote([income_text(user, await income_rules(session, user))])
    preview = await _preview(session, user)
    if preview:
        text += "\n\n" + preview
    markup = income_settings_keyboard(user.income_mode == "hourly")
    if edit is not None:
        await show(edit, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _preview(session: AsyncSession, user: User) -> str:
    today = user_today(user.timezone)
    schedule = await pay_schedule(session, user)
    upcoming = payouts(schedule, today, today + timedelta(days=PREVIEW_DAYS))[:3]
    if not upcoming:
        return ""
    return "📅 <b>Ближайшие выплаты</b>\n" + payout_lines(upcoming, user)


# --- entry points -------------------------------------------------------------------------


@router.callback_query(F.data == "settings:income")
async def settings_income(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                          user: User) -> None:
    await callback.answer()
    await _income_view(callback.message, state, session, user, edit=callback)


@router.callback_query(F.data == "iset:all")
async def setup_again(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await ask_income_type(callback.message, state)


_SINGLE_PROMPTS = {
    "rate": (PaySetup.rate, RATE_PROMPT),
    "hours": (PaySetup.hours, HOURS_PROMPT),
    "cuts": (PaySetup.cuts, CUTS_PROMPT),
    "delay": (PaySetup.delay, DELAY_PROMPT),
}


@router.callback_query(F.data.in_({"iset:rate", "iset:hours", "iset:cuts", "iset:delay"}))
async def edit_one(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    state_, prompt = _SINGLE_PROMPTS[callback.data.removeprefix("iset:")]
    await state.clear()
    await state.update_data(single=True)
    await state.set_state(state_)
    await callback.message.answer(prompt + "\n/cancel — отмена")


@router.callback_query(F.data == "iset:tax")
async def edit_tax(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.update_data(single=True)
    await state.set_state(PaySetup.tax)
    await callback.message.answer(TAX_PROMPT, reply_markup=tax_keyboard())


@router.callback_query(F.data == "iset:weekend")
async def edit_weekend(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Если день выплаты выпадает на выходной, деньги приходят:",
                                  reply_markup=weekend_keyboard())


@router.callback_query(F.data.startswith("iweekend:"))
async def pick_weekend(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                       user: User) -> None:
    await callback.answer()
    user.weekend_shift = callback.data.removeprefix("iweekend:")
    await _income_view(callback.message, state, session, user, edit=callback)


# --- type ---------------------------------------------------------------------------------


@router.callback_query(PaySetup.kind, F.data.startswith("itype:"))
async def pick_type(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                    user: User) -> None:
    await callback.answer()
    kind = callback.data.removeprefix("itype:")
    if kind == "skip":
        await after_income(callback.message, state, session, user)
        return
    if kind == "fixed":
        await state.set_state(PaySetup.fixed)
        await callback.message.answer(FIXED_PROMPT)
        return
    if work_pattern(user) is None:
        reason = "Для почасовой оплаты нужен график смен — по нему я считаю часы.\n\n"
        if await is_onboarding(state):
            await ask_shifts(callback.message, state, reason)
        else:
            await state.clear()
            await callback.message.answer(reason + "Задайте его: ⚙️ Настройки → График.")
        return
    await state.set_state(PaySetup.rate)
    await callback.message.answer(RATE_PROMPT)


@router.message(PaySetup.fixed, F.text)
async def type_fixed(message: Message, state: FSMContext, session: AsyncSession,
                     user: User) -> None:
    rules = parse_rules(message.text)
    if rules is None:
        await message.answer("Не понял. Пример:\n<code>10 25000 аванс\n25 30000</code>\n"
                             f"Не больше {MAX_RULES} выплат.")
        return
    rules.sort(key=lambda r: r[0])
    titles = default_titles(len(rules))
    await save_rules(session, user, [(day, amount, title or default) for (day, amount, title),
                                     default in zip(rules, titles, strict=True)])
    user.income_mode = "fixed"
    await after_income(message, state, session, user)


# --- hourly -------------------------------------------------------------------------------


async def _next(message: Message, state: FSMContext, session: AsyncSession, user: User,
                next_state: State, prompt: str, markup=None) -> None:
    if await _single(state):
        await _income_view(message, state, session, user)
        return
    await state.set_state(next_state)
    await message.answer(prompt, reply_markup=markup)


@router.message(PaySetup.rate, F.text)
async def type_rate(message: Message, state: FSMContext, session: AsyncSession,
                    user: User) -> None:
    rate = parse_rub(message.text, allow_zero=False)
    if rate is None:
        await message.answer("Нужна сумма, например <code>300</code> или <code>287,5</code>.")
        return
    if await _single(state):
        user.hourly_rate_minor = rate
    else:
        await state.update_data(rate=rate)
    await _next(message, state, session, user, PaySetup.hours, HOURS_PROMPT)


@router.message(PaySetup.hours, F.text)
async def type_hours(message: Message, state: FSMContext, session: AsyncSession,
                     user: User) -> None:
    hours = parse_decimal(message.text, Decimal("0.5"), Decimal("24"))
    if hours is None:
        await message.answer("Нужно число часов от 0,5 до 24, например <code>12</code>.")
        return
    if await _single(state):
        user.shift_hours = str(hours)
    else:
        await state.update_data(hours=str(hours))
    await _next(message, state, session, user, PaySetup.cuts, CUTS_PROMPT)


@router.message(PaySetup.cuts, F.text)
async def type_cuts(message: Message, state: FSMContext, session: AsyncSession,
                    user: User) -> None:
    cuts = parse_cuts(message.text)
    if cuts is None:
        await message.answer("Нужны числа от 1 до 31 через пробел, например "
                             "<code>10 20 31</code>.")
        return
    if await _single(state):
        await save_rules(session, user, [(c, 0, HOURLY_TITLE) for c in cuts])
    else:
        await state.update_data(cuts=cuts)
    await _next(message, state, session, user, PaySetup.delay,
                f"Периоды: {describe_periods(cuts)}.\n\n{DELAY_PROMPT}")


@router.message(PaySetup.delay, F.text)
async def type_delay(message: Message, state: FSMContext, session: AsyncSession,
                     user: User) -> None:
    text = message.text.strip()
    if not text.isdigit() or int(text) > MAX_DELAY_DAYS:
        await message.answer(f"Нужно число дней от 0 до {MAX_DELAY_DAYS}.")
        return
    if await _single(state):
        user.pay_delay_days = int(text)
    else:
        await state.update_data(delay=int(text))
    await _next(message, state, session, user, PaySetup.tax, TAX_PROMPT, tax_keyboard())


async def _set_tax(message: Message, state: FSMContext, session: AsyncSession, user: User,
                   tax: Decimal) -> None:
    if await _single(state):
        user.tax_percent = str(tax)
        await _income_view(message, state, session, user)
        return
    data = await state.get_data()
    # Saved only now, so an abandoned dialog leaves the previous settings intact
    user.income_mode = "hourly"
    user.hourly_rate_minor = data["rate"]
    user.shift_hours = data["hours"]
    user.pay_delay_days = data["delay"]
    user.tax_percent = str(tax)
    await save_rules(session, user, [(c, 0, HOURLY_TITLE) for c in data["cuts"]])
    preview = await _preview(session, user)
    if preview:
        await message.answer(preview)
    await after_income(message, state, session, user)


@router.callback_query(PaySetup.tax, F.data.startswith("itax:"))
async def pick_tax(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                   user: User) -> None:
    await callback.answer()
    value = callback.data.removeprefix("itax:")
    if value == "other":
        await state.set_state(PaySetup.tax_other)
        await callback.message.answer("Какой процент вычитается? Например <code>13</code>.")
        return
    await _set_tax(callback.message, state, session, user, Decimal(value))


@router.message(PaySetup.tax_other, F.text)
async def type_tax(message: Message, state: FSMContext, session: AsyncSession,
                   user: User) -> None:
    tax = parse_decimal(message.text, Decimal("0"), Decimal("99"))
    if tax is None:
        await message.answer("Нужен процент от 0 до 99, например <code>13</code>.")
        return
    await _set_tax(message, state, session, user, tax)
