"""Incomes: the '➕ Доход' button, payday questions, salary delays."""

import re
from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.clock import user_today, utcnow
from bot.db.models import IncomeRule, Payday, User
from bot.handlers.common import drop_buttons
from bot.handlers.menu import menu_router
from bot.keyboards import BTN_INCOME, PaydayCb, delay_keyboard, main_menu
from bot.services.budget import (
    add_income,
    build_view,
    get_payday,
    payout_of,
    receive_payday,
    upcoming_paydays,
)
from bot.services.dates import fmt_date, parse_date
from bot.services.limit import default_horizon, payday_plan
from bot.services.money import format_rub, parse_rub
from bot.views import payday_summary

router = Router(name="income")

MAX_COMMENT = 200
_AMOUNT_COMMENT_RE = re.compile(r"^\+?\s*(\d[\d\s .,]*(?:к|k|тыс\.?)?)\s*(.*)$",
                                re.IGNORECASE)


class IncomeInput(StatesGroup):
    other = State()
    payday_amount = State()
    delay_date = State()


@menu_router.message(F.text == BTN_INCOME)
async def income_start(message: Message, state: FSMContext, session: AsyncSession,
                       user: User) -> None:
    await state.clear()
    await state.set_state(IncomeInput.other)
    builder = InlineKeyboardBuilder()
    paydays = await upcoming_paydays(session, user, user_today(user.timezone))
    if paydays:
        builder.button(text=f"💵 {paydays[0].payout.label}: пришла",
                       callback_data=f"inc:recv:{paydays[0].rule.id}:{paydays[0].scheduled}")
        builder.button(text=f"⏳ {paydays[0].payout.label}: задерживается",
                       callback_data=f"inc:delay:{paydays[0].rule.id}:{paydays[0].scheduled}")
        builder.adjust(1)
    await message.answer(
        "Сколько пришло и откуда? Например: <code>5000 подработка</code>.\n"
        "Можно и без кнопки, в любой момент: <code>+5000 подработка</code>.\n/cancel — отмена",
        reply_markup=builder.as_markup() if paydays else None,
    )


@router.message(IncomeInput.other, F.text)
async def income_other(message: Message, state: FSMContext, session: AsyncSession,
                       user: User) -> None:
    match = _AMOUNT_COMMENT_RE.match(message.text.strip())
    amount = parse_rub(match.group(1), allow_zero=False) if match else None
    if amount is None:
        await message.answer("Пример: <code>5000 подработка</code>.")
        return
    comment = match.group(2).strip()[:MAX_COMMENT] or None
    await add_income(session, user, amount, "other", comment, user_today(user.timezone), utcnow())
    await state.clear()
    await message.answer(f"Записал поступление {format_rub(amount)}.", reply_markup=main_menu())


async def _payday_from_button(session: AsyncSession, user: User, data: str) -> Payday | None:
    _, _, rule_id, scheduled = data.split(":", 3)
    rule = await session.get(IncomeRule, int(rule_id))
    if rule is None or rule.user_id != user.id:
        return None
    payday = await get_payday(session, rule.id, date.fromisoformat(scheduled))
    await session.refresh(payday, ["rule"])
    return payday


@router.callback_query(F.data.startswith("inc:recv:"))
async def income_recv_button(callback: CallbackQuery, state: FSMContext,
                             session: AsyncSession, user: User) -> None:
    await state.clear()
    payday = await _payday_from_button(session, user, callback.data)
    await callback.answer()
    if payday is None or payday.status == "received":
        await callback.message.answer("Уже отмечено.")
        return
    await state.set_state(IncomeInput.payday_amount)
    await state.update_data(payday_id=payday.id)
    expected = (await payout_of(session, user, payday)).amount
    await callback.message.answer(
        f"Сколько пришло? Ожидалось ≈ {format_rub(expected)}.\n/cancel — отмена"
    )


@router.callback_query(F.data.startswith("inc:delay:"))
async def income_delay_button(callback: CallbackQuery, state: FSMContext,
                              session: AsyncSession, user: User) -> None:
    await state.clear()
    payday = await _payday_from_button(session, user, callback.data)
    await callback.answer()
    if payday is None or payday.status == "received":
        await callback.message.answer("Уже отмечено.")
        return
    await callback.message.answer("На сколько задерживается?",
                                  reply_markup=delay_keyboard(payday.id))


# --- payday question ----------------------------------------------------------------------


async def _own_payday(session: AsyncSession, user: User, payday_id: int) -> Payday | None:
    payday = await session.scalar(
        select(Payday).where(Payday.id == payday_id).options(selectinload(Payday.rule))
    )
    return payday if payday is not None and payday.rule.user_id == user.id else None


async def _receive(message: Message, session: AsyncSession, user: User, payday: Payday,
                   amount: int) -> None:
    today = user_today(user.timezone)
    label = (await payout_of(session, user, payday)).label
    await receive_payday(session, user, payday, amount, today, utcnow())
    paydays = await upcoming_paydays(session, user, today)
    horizon = paydays[0].effective if paydays else default_horizon(today)
    view = await build_view(session, user, today)
    plan = payday_plan(amount, view.obligations, horizon)
    await message.answer(payday_summary(label, plan, today) +
                         "\n\nПришлите остаток на карте — пересчитаю лимит.",
                         reply_markup=main_menu())


@router.callback_query(PaydayCb.filter(F.action == "yes"))
async def payday_yes(callback: CallbackQuery, callback_data: PaydayCb,
                     session: AsyncSession, user: User) -> None:
    payday = await _own_payday(session, user, callback_data.payday_id)
    if payday is None or payday.status == "received":
        await callback.answer("Уже отмечено.")
        return
    await callback.answer()
    await drop_buttons(callback)
    payout = await payout_of(session, user, payday)
    await _receive(callback.message, session, user, payday, payout.amount)


@router.callback_query(PaydayCb.filter(F.action == "other"))
async def payday_other(callback: CallbackQuery, callback_data: PaydayCb, state: FSMContext,
                       session: AsyncSession, user: User) -> None:
    payday = await _own_payday(session, user, callback_data.payday_id)
    if payday is None or payday.status == "received":
        await callback.answer("Уже отмечено.")
        return
    await callback.answer()
    await state.set_state(IncomeInput.payday_amount)
    await state.update_data(payday_id=payday.id)
    await callback.message.answer("Сколько пришло?\n/cancel — отмена")


@router.message(IncomeInput.payday_amount, F.text)
async def payday_amount(message: Message, state: FSMContext, session: AsyncSession,
                        user: User) -> None:
    amount = parse_rub(message.text, allow_zero=False)
    if amount is None:
        await message.answer("Нужна сумма, например <code>31 500</code>.")
        return
    payday = await _own_payday(session, user, (await state.get_data())["payday_id"])
    await state.clear()
    if payday is None or payday.status == "received":
        await message.answer("Уже отмечено.")
        return
    await _receive(message, session, user, payday, amount)


@router.callback_query(PaydayCb.filter(F.action == "delay"))
async def payday_delay(callback: CallbackQuery, callback_data: PaydayCb,
                       session: AsyncSession, user: User) -> None:
    payday = await _own_payday(session, user, callback_data.payday_id)
    if payday is None or payday.status == "received":
        await callback.answer("Уже отмечено.")
        return
    await callback.answer()
    await callback.message.answer("На сколько задерживается?",
                                  reply_markup=delay_keyboard(payday.id))


async def _set_delay(message: Message, payday: Payday, until: date) -> None:
    payday.status = "delayed"
    payday.delayed_until = until
    await message.answer(
        f"Понял, жду {escape(payday.rule.title.lower())} {fmt_date(until)}. "
        "Лимит пересчитан до этой даты, в этот день спрошу снова."
    )


@router.callback_query(PaydayCb.filter(F.action.in_({"d1", "d3"})))
async def payday_delay_days(callback: CallbackQuery, callback_data: PaydayCb,
                            session: AsyncSession, user: User) -> None:
    payday = await _own_payday(session, user, callback_data.payday_id)
    if payday is None or payday.status == "received":
        await callback.answer("Уже отмечено.")
        return
    await callback.answer()
    await drop_buttons(callback)
    days = 1 if callback_data.action == "d1" else 3
    base = max(user_today(user.timezone), payday.scheduled)
    await _set_delay(callback.message, payday, base + timedelta(days=days))


@router.callback_query(PaydayCb.filter(F.action == "date"))
async def payday_delay_date(callback: CallbackQuery, callback_data: PaydayCb,
                            state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(IncomeInput.delay_date)
    await state.update_data(payday_id=callback_data.payday_id)
    await callback.message.answer("До какой даты задерживается? <code>ДД.ММ</code>\n"
                                  "/cancel — отмена")


@router.message(IncomeInput.delay_date, F.text)
async def payday_delay_typed(message: Message, state: FSMContext, session: AsyncSession,
                             user: User) -> None:
    today = user_today(user.timezone)
    until = parse_date(message.text, today)
    if until is None or until <= today:
        await message.answer("Нужна будущая дата <code>ДД.ММ</code>.")
        return
    payday = await _own_payday(session, user, (await state.get_data())["payday_id"])
    await state.clear()
    if payday is None or payday.status == "received":
        await message.answer("Уже отмечено.")
        return
    await _set_delay(message, payday, until)
