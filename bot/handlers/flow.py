"""Steps shared by onboarding and settings. ``onboarding`` in the FSM data says whether a
step continues to the next one or returns to the settings."""

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.keyboards import settings_keyboard, shifts_keyboard
from bot.services.budget import income_rules
from bot.views import settings_text


class Setup(StatesGroup):
    timezone = State()
    shifts = State()
    shift_position = State()
    balance = State()
    remind_time = State()
    evening_time = State()
    reserve = State()
    threshold = State()


SHIFTS_PROMPT = "Какой у вас график работы? Выберите или пришлите свой, например <code>4/3</code>."
BALANCE_PROMPT = "Сколько сейчас на карте? Пришлите одно число, например <code>12 500</code>."


async def is_onboarding(state: FSMContext) -> bool:
    return bool((await state.get_data()).get("onboarding"))


async def back_to_settings(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await state.clear()
    await message.answer(settings_text(user, await income_rules(session, user)),
                         reply_markup=settings_keyboard())


async def ask_shifts(message: Message, state: FSMContext, reason: str = "") -> None:
    await state.set_state(Setup.shifts)
    await message.answer(reason + SHIFTS_PROMPT, reply_markup=shifts_keyboard())


async def after_income(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    if await is_onboarding(state):
        await state.set_state(Setup.balance)
        await message.answer(BALANCE_PROMPT)
        return
    await back_to_settings(message, state, session, user)
