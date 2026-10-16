"""Onboarding (/start) and settings. The same steps serve both; ``onboarding`` in the FSM data
says whether to continue to the next step or return to the settings."""

import re
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from bot.clock import user_today, utcnow
from bot.db.models import IncomeRule, User
from bot.handlers.common import show
from bot.handlers.menu import menu_router
from bot.keyboards import (
    BTN_SETTINGS,
    main_menu,
    settings_keyboard,
    shift_position_keyboard,
    shifts_keyboard,
    skip_keyboard,
    timezone_keyboard,
)
from bot.services.budget import CARD, income_rules, save_snapshot
from bot.services.dates import parse_time
from bot.services.deployments import list_deployments
from bot.services.money import parse_rub
from bot.services.tz import parse_timezone
from bot.services.work_calendar import default_anchor, parse_pattern
from bot.views import deployment_text, settings_text

router = Router(name="start")

MAX_RULES = 4
MAX_TITLE = 50


class Setup(StatesGroup):
    timezone = State()
    shifts = State()
    shift_position = State()
    income = State()
    balance = State()
    remind_time = State()
    evening_time = State()


TZ_PROMPT = (
    "В каком вы часовом поясе? Выберите город или пришлите смещение, "
    "например <code>UTC+5</code>."
)
SHIFTS_PROMPT = "Какой у вас график работы? Выберите или пришлите свой, например <code>4/3</code>."
INCOME_PROMPT = (
    "Когда приходят деньги? Пришлите число месяца и примерную сумму, каждую выплату — "
    "отдельной строкой:\n<code>10 25000 аванс\n25 30000 зарплата</code>\n"
    "Если выплата выпадает на выходной, жду её в пятницу перед ним.\n"
    "Сумму потом можно уточнить в день выплаты."
)
BALANCE_PROMPT = "Сколько сейчас на карте? Пришлите одно число, например <code>12 500</code>."

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


async def _onboarding(state: FSMContext) -> bool:
    return bool((await state.get_data()).get("onboarding"))


async def _back_to_settings(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await state.clear()
    await message.answer(settings_text(user, await income_rules(session, user)),
                         reply_markup=settings_keyboard())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    if user.timezone is None:
        await message.answer(
            "Привет! Я помогу видеть реальную картину по деньгам и не пропускать платежи.\n"
            "Каждый день присылайте мне одно число — остаток на карте. Остальное посчитаю сам: "
            "траты, сколько можно тратить в день, напоминания о кредитах.\n\n"
            "Сначала немного настроек."
        )
        await state.update_data(onboarding=True)
        await state.set_state(Setup.timezone)
        await message.answer(TZ_PROMPT, reply_markup=timezone_keyboard())
        return
    await message.answer("Главное меню.", reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())


# --- timezone -----------------------------------------------------------------------------


async def _save_timezone(
    message: Message, state: FSMContext, session: AsyncSession, user: User, tz: str
) -> None:
    user.timezone = tz
    if await _onboarding(state):
        await state.set_state(Setup.shifts)
        await message.answer(f"Часовой пояс: {tz}.\n\n{SHIFTS_PROMPT}",
                             reply_markup=shifts_keyboard())
        return
    await _back_to_settings(message, state, session, user)


@router.callback_query(Setup.timezone, F.data.startswith("tz:"))
async def pick_timezone(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await callback.answer()
    await _save_timezone(callback.message, state, session, user,
                         callback.data.removeprefix("tz:"))


@router.message(Setup.timezone, F.text)
async def type_timezone(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    tz = parse_timezone(message.text)
    if tz is None:
        await message.answer("Не узнал часовой пояс. " + TZ_PROMPT)
        return
    await _save_timezone(message, state, session, user, tz)


# --- shifts -------------------------------------------------------------------------------


async def _after_shifts(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    if await _onboarding(state):
        await state.set_state(Setup.income)
        await message.answer(INCOME_PROMPT, reply_markup=skip_keyboard("income:skip"))
        return
    await _back_to_settings(message, state, session, user)


async def _set_pattern(
    message: Message, state: FSMContext, session: AsyncSession, user: User, text: str
) -> None:
    if text == "none":
        user.shift_work = user.shift_off = user.shift_anchor = None
        await _after_shifts(message, state, session, user)
        return
    pattern = parse_pattern(text)
    if pattern is None:
        await message.answer("Не понял график. " + SHIFTS_PROMPT)
        return
    work, off = pattern
    if work + off == 7 or off == 0:
        # A weekly pattern starts on Monday; no need to ask where in the cycle we are
        user.shift_work, user.shift_off = work, off
        user.shift_anchor = default_anchor(work, off, user_today(user.timezone))
        await _after_shifts(message, state, session, user)
        return
    await state.update_data(shift_work=work, shift_off=off)
    await state.set_state(Setup.shift_position)
    await message.answer(f"График {work}/{off}. Какой у вас сегодня день?",
                         reply_markup=shift_position_keyboard(work, off))


@router.callback_query(Setup.shifts, F.data.startswith("shift:"))
async def pick_shifts(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await callback.answer()
    await _set_pattern(callback.message, state, session, user,
                       callback.data.removeprefix("shift:"))


@router.message(Setup.shifts, F.text)
async def type_shifts(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await _set_pattern(message, state, session, user, message.text.strip())


@router.callback_query(Setup.shift_position, F.data.startswith("spos:"))
async def pick_position(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await callback.answer()
    data = await state.get_data()
    position = int(callback.data.removeprefix("spos:"))
    user.shift_work, user.shift_off = data["shift_work"], data["shift_off"]
    user.shift_anchor = user_today(user.timezone) - timedelta(days=position)
    await _after_shifts(callback.message, state, session, user)


# --- income rules -------------------------------------------------------------------------


async def _after_income(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    if await _onboarding(state):
        await state.set_state(Setup.balance)
        await message.answer(BALANCE_PROMPT)
        return
    await _back_to_settings(message, state, session, user)


@router.message(Setup.income, F.text)
async def type_income(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    rules = parse_rules(message.text)
    if rules is None:
        await message.answer(f"Не понял. Пример:\n<code>10 25000 аванс\n25 30000</code>\n"
                             f"Не больше {MAX_RULES} выплат.")
        return
    rules.sort(key=lambda r: r[0])
    titles = default_titles(len(rules))
    # Paydays history refers to rules, so editing replaces them all
    await session.execute(delete(IncomeRule).where(IncomeRule.user_id == user.id))
    for (day, amount, title), default in zip(rules, titles, strict=True):
        session.add(IncomeRule(user_id=user.id, title=title or default, day=day,
                               amount_minor=amount))
    await session.flush()
    await _after_income(message, state, session, user)


@router.callback_query(Setup.income, F.data == "income:skip")
async def skip_income(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await callback.answer()
    await _after_income(callback.message, state, session, user)


@router.message(Setup.balance, F.text)
async def type_balance(message: Message, state: FSMContext, user: User,
                       session: AsyncSession) -> None:
    amount = parse_rub(message.text, allow_negative=True)
    if amount is None:
        await message.answer("Нужно одно число, например <code>12 500</code>.")
        return
    await save_snapshot(session, user, CARD, user_today(user.timezone), amount, utcnow())
    await state.clear()
    await message.answer(
        "Готово! Дальше:\n"
        "• каждый день присылайте остаток на карте одним числом;\n"
        "• наличные — <code>нал 2000</code>, когда меняются;\n"
        "• другие поступления — <code>+5000 премия</code>;\n"
        "• траты записывать не нужно, но можно: <code>500 еда</code>.\n\n"
        "Теперь добавьте кредиты: «💳 Кредиты» → «Добавить кредит».",
        reply_markup=main_menu(),
    )


# --- settings -----------------------------------------------------------------------------


@menu_router.message(Command("settings"))
@menu_router.message(F.text == BTN_SETTINGS)
async def cmd_settings(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await state.clear()
    await message.answer(settings_text(user, await income_rules(session, user)),
                         reply_markup=settings_keyboard())


@router.callback_query(F.data == "settings:tz")
async def settings_tz(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Setup.timezone)
    await callback.message.answer(TZ_PROMPT, reply_markup=timezone_keyboard())
    await callback.answer()


@router.callback_query(F.data == "settings:shifts")
async def settings_shifts(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Setup.shifts)
    await callback.message.answer(SHIFTS_PROMPT, reply_markup=shifts_keyboard())
    await callback.answer()


@router.callback_query(F.data == "settings:income")
async def settings_income(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Setup.income)
    await callback.message.answer(
        INCOME_PROMPT + "\n\nНовый список заменит текущий. /cancel — отмена"
    )
    await callback.answer()


@router.callback_query(F.data == "settings:remind")
async def settings_remind(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    await state.clear()
    await state.set_state(Setup.remind_time)
    await callback.message.answer(
        f"Сейчас: <code>{user.remind_time:%H:%M}</code>. Во сколько напоминать о платежах "
        "и спрашивать про зарплату? Например <code>10:00</code>.\n/cancel — отмена"
    )
    await callback.answer()


@router.callback_query(F.data == "settings:evening")
async def settings_evening(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    await state.clear()
    await state.set_state(Setup.evening_time)
    await callback.message.answer(
        f"Сейчас: <code>{user.evening_time:%H:%M}</code>. Во сколько напоминать прислать "
        "остаток, если вы его ещё не прислали? Например <code>21:00</code>.\n/cancel — отмена"
    )
    await callback.answer()


async def _set_time(message: Message, state: FSMContext, session: AsyncSession, user: User,
                    field: str) -> None:
    value = parse_time(message.text)
    if value is None:
        await message.answer("Нужно время в формате ЧЧ:ММ, например 09:30.")
        return
    setattr(user, field, value)
    await _back_to_settings(message, state, session, user)


@router.message(Setup.remind_time, F.text)
async def type_remind(message: Message, state: FSMContext, session: AsyncSession,
                      user: User) -> None:
    await _set_time(message, state, session, user, "remind_time")


@router.message(Setup.evening_time, F.text)
async def type_evening(message: Message, state: FSMContext, session: AsyncSession,
                       user: User) -> None:
    await _set_time(message, state, session, user, "evening_time")


@router.callback_query(F.data == "settings:updates")
async def settings_updates(callback: CallbackQuery, session: AsyncSession) -> None:
    deployments = await list_deployments(session)
    text = "\n\n".join(deployment_text(d) for d in deployments) or "Обновлений пока не было."
    builder = InlineKeyboardBuilder()
    builder.button(text="« Настройки", callback_data="settings:back")
    await show(callback, text, builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "settings:back")
async def settings_back(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                        user: User) -> None:
    await state.clear()
    await show(callback, settings_text(user, await income_rules(session, user)),
               settings_keyboard())
    await callback.answer()
