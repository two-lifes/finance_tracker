"""Main screen and free-text input: balance, cash, '+5000 премия', '500 еда'."""

import re

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.clock import user_today, utcnow
from bot.db.models import Loan, LoanDue, User
from bot.handlers.common import drop_buttons
from bot.handlers.menu import menu_router
from bot.integrations.subs_client import SubsClient
from bot.keyboards import BTN_HOME, DueCb, balance_keyboard, main_menu
from bot.services.budget import (
    CASH,
    add_expense_note,
    add_income,
    build_view,
    record_card_balance,
    save_snapshot,
)
from bot.services.external import fx_refresh_due, load_subs, refresh_rates, sync_subs
from bot.services.loan_ops import pay_due, undo_auto_debit
from bot.services.money import format_rub, parse_rub
from bot.views import balance_reply, main_screen

router = Router(name="home")

MAX_COMMENT = 200

_CASH_RE = re.compile(r"^(?:нал|наличные|наличка|cash)\s*[:=]?\s*(.+)$", re.IGNORECASE)
_INCOME_RE = re.compile(r"^\+\s*(\d[\d\s .,]*(?:к|k|тыс\.?)?)\s*(.*)$", re.IGNORECASE)
_NOTE_RE = re.compile(r"^(\d[\d\s .,]*(?:к|k)?)\s+([^\d\s].*)$", re.IGNORECASE)


async def refresh_external(
    session: AsyncSession, user: User, subs_client: SubsClient | None
) -> None:
    """Fresh subscriptions on every main screen; rates when a currency needs them."""
    today, now = user_today(user.timezone), utcnow()
    await sync_subs(session, subs_client, today, now)
    subs = await load_subs(session)
    if await fx_refresh_due(session, subs.currencies, today, user.timezone, now):
        await refresh_rates(session, subs.currencies, today, now)


async def home_text(
    session: AsyncSession, user: User, subs_client: SubsClient | None
) -> str:
    await refresh_external(session, user, subs_client)
    view = await build_view(session, user, user_today(user.timezone))
    return main_screen(view, user, subs_client is not None)


@menu_router.message(Command("today"))
@menu_router.message(F.text == BTN_HOME)
async def show_home(
    message: Message, state: FSMContext, session: AsyncSession, user: User,
    subs_client: SubsClient | None,
) -> None:
    await state.clear()
    if user.timezone is None:
        await message.answer("Сначала короткая настройка: /start")
        return
    await message.answer(await home_text(session, user, subs_client), reply_markup=main_menu())


# Free text is read only outside dialogs: inside one it is the answer to the dialog
@router.message(StateFilter(None), F.text.regexp(_CASH_RE.pattern, flags=re.IGNORECASE))
async def cash_balance(message: Message, session: AsyncSession, user: User) -> None:
    amount = parse_rub(_CASH_RE.match(message.text.strip()).group(1))
    if amount is None:
        await message.answer("Пример: <code>нал 2000</code>.")
        return
    await save_snapshot(session, user, CASH, user_today(user.timezone), amount, utcnow())
    await message.answer(f"Наличные: {format_rub(amount)}. Учту их в лимите.")


@router.message(StateFilter(None), F.text.regexp(_INCOME_RE.pattern, flags=re.IGNORECASE))
async def other_income(message: Message, session: AsyncSession, user: User) -> None:
    match = _INCOME_RE.match(message.text.strip())
    amount = parse_rub(match.group(1), allow_zero=False)
    if amount is None:
        await message.answer("Пример: <code>+5000 премия</code>.")
        return
    comment = match.group(2).strip()[:MAX_COMMENT] or None
    await add_income(session, user, amount, "other", comment, user_today(user.timezone), utcnow())
    note = f" ({comment})" if comment else ""
    await message.answer(f"Записал поступление {format_rub(amount)}{note}.")


@router.message(StateFilter(None), F.text.func(lambda t: parse_rub(t, allow_negative=True)
                                                is not None))
async def card_balance(
    message: Message, session: AsyncSession, user: User, subs_client: SubsClient | None
) -> None:
    if user.timezone is None:
        await message.answer("Сначала короткая настройка: /start")
        return
    amount = parse_rub(message.text, allow_negative=True)
    await refresh_external(session, user, subs_client)
    today = user_today(user.timezone)
    result = await record_card_balance(session, user, amount, today, utcnow())
    view = await build_view(session, user, today)
    text = balance_reply(result, amount) + "\n\n" + main_screen(view, user, subs_client is not None)
    await message.answer(text, reply_markup=balance_keyboard(result.settled, result.unconfirmed))


@router.message(StateFilter(None), F.text.regexp(_NOTE_RE.pattern, flags=re.IGNORECASE))
async def expense_note(message: Message, session: AsyncSession, user: User) -> None:
    match = _NOTE_RE.match(message.text.strip())
    amount = parse_rub(match.group(1), allow_zero=False)
    if amount is None:
        await message.answer("Не понял сумму. Пример: <code>500 еда</code>.")
        return
    category = match.group(2).strip()[:50]
    await add_expense_note(session, user, amount, category, user_today(user.timezone), utcnow())
    await message.answer(f"Записал: {format_rub(amount)} — {category}. "
                         "На расчёты это не влияет, траты я и так вижу по остатку.")


# --- loan dues from balance replies and reminders -----------------------------------------


async def _due_and_loan(session: AsyncSession, user: User, due_id: int):
    due = await session.get(LoanDue, due_id)
    if due is None:
        return None, None
    loan = await session.get(Loan, due.loan_id)
    if loan is None or loan.user_id != user.id:
        return None, None
    return due, loan


@router.callback_query(DueCb.filter(F.action == "undo"))
async def due_not_debited(callback: CallbackQuery, callback_data: DueCb,
                          session: AsyncSession, user: User) -> None:
    due, loan = await _due_and_loan(session, user, callback_data.due_id)
    if due is None or not await undo_auto_debit(session, due):
        await callback.answer("Уже неактуально.")
        return
    await callback.answer()
    await callback.message.answer(
        f"Понял: {loan.lender} не списался. Платёж {format_rub(due.amount_minor - due.paid_minor)} "
        "теперь «ждёт оплаты» и учтён в лимите. Траты пересчитаются при следующем остатке."
    )


@router.callback_query(DueCb.filter(F.action == "paid"))
async def due_paid(callback: CallbackQuery, callback_data: DueCb, session: AsyncSession,
                   user: User) -> None:
    due, loan = await _due_and_loan(session, user, callback_data.due_id)
    if due is None or due.status == "paid" or loan.status != "active":
        await callback.answer("Уже отмечено.")
        return
    left = due.amount_minor - due.paid_minor
    result = await pay_due(session, loan, due, left, user_today(user.timezone), "manual", utcnow())
    await callback.answer("Записал")
    await drop_buttons(callback)
    text = f"✅ {loan.lender}: оплачено {format_rub(left)}."
    if result.closed:
        text += "\n🎉 Кредит полностью погашен!"
    await callback.message.answer(text)
