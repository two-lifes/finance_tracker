"""Main screen and free-text input: balance, cash, '+5000 премия', '500 еда'."""

import re
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.clock import user_today, utcnow
from bot.db.models import Loan, LoanDue, User
from bot.handlers.common import drop_buttons
from bot.handlers.menu import menu_router
from bot.integrations.subs_client import SubsClient
from bot.keyboards import BTN_HOME, DueCb, balance_keyboard, main_menu, unchanged_keyboard
from bot.services.budget import (
    CARD,
    CASH,
    add_expense_note,
    add_income,
    build_view,
    latest_snapshot,
    record_card_balance,
    save_snapshot,
)
from bot.services.external import fx_refresh_due, load_subs, refresh_rates, sync_subs
from bot.services.loan_ops import loan_progress, pay_due, undo_auto_debit
from bot.services.money import format_rub, parse_rub
from bot.services.priority import cancel_candidates, rank
from bot.views import (
    ASK_CARD,
    ASK_CASH,
    balance_reply,
    closed_text,
    main_screen,
    shortfall_screen,
)

router = Router(name="home")

MAX_COMMENT = 200

_CASH_RE = re.compile(r"^(?:нал|наличные|наличка|cash)\s*[:=]?\s*(.+)$", re.IGNORECASE)
_INCOME_RE = re.compile(r"^\+\s*(\d[\d\s .,]*(?:к|k|тыс\.?)?)\s*(.*)$", re.IGNORECASE)
_NOTE_RE = re.compile(r"^(\d[\d\s .,]*(?:к|k)?)\s+([^\d\s].*)$", re.IGNORECASE)


class BalanceInput(StatesGroup):
    card = State()
    cash = State()


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


async def shortfall_text(session: AsyncSession, user: User) -> str:
    view = await build_view(session, user, user_today(user.timezone))
    items = view.shortfall.items if view.shortfall is not None else []
    return shortfall_screen(view, rank(items, view.terms, view.today), cancel_candidates(items))


@menu_router.message(Command("shortfall"))
async def show_shortfall(message: Message, state: FSMContext, session: AsyncSession,
                         user: User) -> None:
    await state.clear()
    if user.timezone is None:
        await message.answer("Сначала короткая настройка: /start")
        return
    await message.answer(await shortfall_text(session, user))


@router.callback_query(F.data == "short:plan")
async def shortfall_button(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await callback.answer()
    await callback.message.answer(await shortfall_text(session, user))


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
    note = f" ({escape(comment)})" if comment else ""
    await message.answer(f"Записал поступление {format_rub(amount)}{note}.")


async def _record_card(
    message: Message, session: AsyncSession, user: User, subs_client: SubsClient | None,
    amount: int,
) -> None:
    await refresh_external(session, user, subs_client)
    today = user_today(user.timezone)
    result = await record_card_balance(session, user, amount, today, utcnow())
    view = await build_view(session, user, today)
    text = balance_reply(result, amount) + "\n\n" + main_screen(view, user, subs_client is not None)
    await message.answer(text, reply_markup=balance_keyboard(result.settled, result.unconfirmed))


@router.message(StateFilter(None), F.text.func(lambda t: parse_rub(t, allow_negative=True)
                                                is not None))
async def card_balance(
    message: Message, session: AsyncSession, user: User, subs_client: SubsClient | None
) -> None:
    if user.timezone is None:
        await message.answer("Сначала короткая настройка: /start")
        return
    await _record_card(message, session, user, subs_client,
                       parse_rub(message.text, allow_negative=True))


@router.message(StateFilter(None), F.text.regexp(_NOTE_RE.pattern, flags=re.IGNORECASE))
async def expense_note(message: Message, session: AsyncSession, user: User) -> None:
    match = _NOTE_RE.match(message.text.strip())
    amount = parse_rub(match.group(1), allow_zero=False)
    if amount is None:
        await message.answer("Не понял сумму. Пример: <code>500 еда</code>.")
        return
    category = match.group(2).strip()[:50]
    await add_expense_note(session, user, amount, category, user_today(user.timezone), utcnow())
    await message.answer(f"Записал: {format_rub(amount)} — {escape(category)}. "
                         "На расчёты это не влияет, траты я и так вижу по остатку.")


# --- evening reminder: card, then cash ----------------------------------------------------


async def _last_amount(session: AsyncSession, user: User, account: str) -> int | None:
    row = await latest_snapshot(session, user, account)
    return row.amount_minor if row is not None else None


@router.callback_query(F.data == "bal:start")
async def balance_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                        user: User) -> None:
    await callback.answer()
    if user.timezone is None:
        await callback.message.answer("Сначала короткая настройка: /start")
        return
    await state.clear()
    await state.set_state(BalanceInput.card)
    await callback.message.answer(
        ASK_CARD, reply_markup=unchanged_keyboard(await _last_amount(session, user, CARD))
    )


async def _ask_cash(message: Message, state: FSMContext, session: AsyncSession, user: User,
                    card: int) -> None:
    await state.set_state(BalanceInput.cash)
    await state.update_data(card=card)
    # No cash recorded yet means none: "unchanged" keeps it at zero
    cash = await _last_amount(session, user, CASH) or 0
    await message.answer(ASK_CASH, reply_markup=unchanged_keyboard(cash))


@router.message(BalanceInput.card, F.text)
async def balance_card(message: Message, state: FSMContext, session: AsyncSession,
                       user: User) -> None:
    amount = parse_rub(message.text, allow_negative=True)
    if amount is None:
        await message.answer("Не понял сумму. Пример: <code>45000</code>.")
        return
    await _ask_cash(message, state, session, user, amount)


@router.callback_query(BalanceInput.card, F.data == "bal:same")
async def balance_card_same(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                            user: User) -> None:
    amount = await _last_amount(session, user, CARD)
    if amount is None:
        await callback.answer("Остатка ещё нет — пришлите число.")
        return
    await callback.answer()
    await drop_buttons(callback)
    await _ask_cash(callback.message, state, session, user, amount)


async def _finish(message: Message, state: FSMContext, session: AsyncSession, user: User,
                  subs_client: SubsClient | None, cash: int | None) -> None:
    card = (await state.get_data())["card"]
    await state.clear()
    # Cash goes first: spending for the day is counted from card + cash
    if cash is not None:
        await save_snapshot(session, user, CASH, user_today(user.timezone), cash, utcnow())
    await _record_card(message, session, user, subs_client, card)


@router.message(BalanceInput.cash, F.text)
async def balance_cash(message: Message, state: FSMContext, session: AsyncSession, user: User,
                       subs_client: SubsClient | None) -> None:
    amount = parse_rub(message.text)
    if amount is None:
        await message.answer("Не понял сумму. Пример: <code>5000</code> или <code>0</code>.")
        return
    await _finish(message, state, session, user, subs_client, amount)


@router.callback_query(BalanceInput.cash, F.data == "bal:same")
async def balance_cash_same(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                            user: User, subs_client: SubsClient | None) -> None:
    await callback.answer()
    await drop_buttons(callback)
    await _finish(callback.message, state, session, user, subs_client, None)


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
    if due is None or not await undo_auto_debit(session, due, user_today(user.timezone)):
        await callback.answer("Уже неактуально.")
        return
    await callback.answer()
    await callback.message.answer(
        f"Понял: {escape(loan.lender)} не списался. "
        f"Платёж {format_rub(due.amount_minor - due.paid_minor)} "
        "теперь «ждёт оплаты» и учтён в лимите. Если банк спишет его позже — учту "
        "по следующему остатку."
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
    text = f"✅ {escape(loan.lender)}: оплачено {format_rub(left)}."
    if result.closed:
        text += "\n" + closed_text(*await loan_progress(session, user))
    await callback.message.answer(text)
