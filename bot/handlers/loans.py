import re
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.clock import user_today, utcnow
from bot.db.models import Loan, LoanDue, User
from bot.handlers.common import drop_buttons, show
from bot.handlers.menu import menu_router
from bot.keyboards import (
    BTN_LOANS,
    EDIT_FIELDS,
    DueCb,
    EarlyCb,
    EditCb,
    LoanCb,
    confirm_loan_keyboard,
    due_pay_keyboard,
    early_mode_keyboard,
    loan_card_keyboard,
    loan_edit_keyboard,
    loan_kinds_keyboard,
    loans_keyboard,
    main_menu,
    pay_method_keyboard,
    skip_keyboard,
    yes_no_keyboard,
)
from bot.services.dates import parse_date
from bot.services.loan_ops import (
    LoanDraft,
    accrues,
    active_loans,
    close_loan,
    create_loan,
    early_repay,
    interest_now,
    next_due,
    pay_due,
    regenerate_dues,
)
from bot.services.loans import EarlyMode, LoanKind, RateUnit, Schedule
from bot.services.money import format_rub, parse_rub
from bot.views import early_result_text, loan_button, loan_card, loan_draft_text

router = Router(name="loans")

MAX_LENDER = 100
_PERCENT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*%?\s*(в день|/д(?:ень)?|д|годовых|/г)?$",
                         re.IGNORECASE)


def parse_percent(text: str) -> Decimal | None:
    match = _PERCENT_RE.match(text.strip())
    if not match:
        return None
    try:
        value = Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None
    return value if 0 <= value <= 1000 else None


class AddLoan(StatesGroup):
    lender = State()
    kind = State()
    principal = State()
    rate = State()
    payment = State()
    first_due = State()
    insurance = State()
    pay_method = State()
    confirm = State()


class LoanInput(StatesGroup):
    pay_amount = State()  # another amount for a due
    early_amount = State()
    early_mode = State()
    edit_value = State()


PROMPTS = {
    LoanKind.BANK: {
        "principal": "Сколько осталось выплатить (остаток долга)?",
        "rate": "Ставка, % годовых? Например <code>24,9</code>.",
        "payment": "Ежемесячный платёж?",
        "first_due": "Дата ближайшего платежа? <code>ДД.ММ</code>",
    },
    LoanKind.MFO: {
        "principal": "Сумма займа (основной долг)?",
        "rate": "Ставка, % в день? Например <code>0,8</code>.",
        "payment": "Сколько нужно вернуть всего (с процентами)?",
        "first_due": "До какой даты вернуть? <code>ДД.ММ</code>",
    },
    LoanKind.CARD: {
        "principal": "Текущий долг по карте?",
        "rate": "Ставка после льготного периода, % годовых?",
        "payment": "Минимальный платёж в этом месяце?",
        "first_due": "Дата ближайшего платежа? <code>ДД.ММ</code>",
    },
    LoanKind.INSTALLMENT: {
        "principal": "Сколько осталось выплатить?",
        "payment": "Ежемесячный платёж?",
        "first_due": "Дата ближайшего платежа? <code>ДД.ММ</code>",
    },
}


def _kind(data: dict) -> LoanKind:
    return LoanKind(data["kind"])


async def _own_loan(session: AsyncSession, user: User, loan_id: int) -> Loan | None:
    loan = await session.scalar(
        select(Loan).where(Loan.id == loan_id).options(selectinload(Loan.dues))
    )
    return loan if loan is not None and loan.user_id == user.id else None


async def _refreshed(session: AsyncSession, loan: Loan) -> Loan:
    # Dues may have been added or deleted behind the loaded collection
    await session.refresh(loan, ["dues"])
    return loan


# --- list ---------------------------------------------------------------------------------


async def _list_view(session: AsyncSession, user: User):
    today = user_today(user.timezone)
    loans = await active_loans(session, user)
    if loans:
        text = "<b>Кредиты</b>\nНажмите на кредит, чтобы открыть его."
    else:
        text = "Кредитов пока нет. Добавьте их — и я буду напоминать о платежах."
    return text, loans_keyboard([(loan.id, loan_button(loan, today)) for loan in loans])


@menu_router.message(Command("loans"))
@menu_router.message(F.text == BTN_LOANS)
async def show_loans(message: Message, state: FSMContext, session: AsyncSession,
                     user: User) -> None:
    await state.clear()
    text, markup = await _list_view(session, user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "loans:list")
async def back_to_list(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                       user: User) -> None:
    await state.clear()
    text, markup = await _list_view(session, user)
    await show(callback, text, markup)
    await callback.answer()


@router.callback_query(LoanCb.filter(F.action == "open"))
async def open_loan(callback: CallbackQuery, callback_data: LoanCb, state: FSMContext,
                    session: AsyncSession, user: User) -> None:
    await state.clear()
    loan = await _own_loan(session, user, callback_data.loan_id)
    if loan is None:
        await callback.answer("Кредит не найден.")
        return
    await show(callback, loan_card(loan, user_today(user.timezone)), loan_card_keyboard(loan))
    await callback.answer()


# --- add ----------------------------------------------------------------------------------


@router.callback_query(F.data == "ladd:start")
async def add_start(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    await callback.answer()
    if user.timezone is None:
        await callback.message.answer("Сначала короткая настройка: /start")
        return
    await state.clear()
    await state.set_state(AddLoan.lender)
    await callback.message.answer("Кто кредитор? Например «Сбер» или «Займер». (/cancel — отмена)")


@router.message(AddLoan.lender, F.text)
async def add_lender(message: Message, state: FSMContext) -> None:
    lender = message.text.strip()
    if not lender or len(lender) > MAX_LENDER or lender.startswith("/"):
        await message.answer(f"Название — от 1 до {MAX_LENDER} символов.")
        return
    await state.update_data(lender=lender)
    await state.set_state(AddLoan.kind)
    await message.answer("Что это за долг?", reply_markup=loan_kinds_keyboard())


@router.callback_query(AddLoan.kind, F.data.startswith("lkind:"))
async def add_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = LoanKind(callback.data.removeprefix("lkind:"))
    await state.update_data(
        kind=kind.value,
        rate_unit=RateUnit.DAY.value if kind == LoanKind.MFO else RateUnit.YEAR.value,
    )
    await state.set_state(AddLoan.principal)
    await callback.answer()
    await callback.message.answer(PROMPTS[kind]["principal"])


@router.message(AddLoan.principal, F.text)
async def add_principal(message: Message, state: FSMContext) -> None:
    amount = parse_rub(message.text, allow_zero=False)
    if amount is None:
        await message.answer("Нужна сумма, например <code>150 000</code>.")
        return
    await state.update_data(principal=amount)
    data = await state.get_data()
    if _kind(data) == LoanKind.INSTALLMENT:
        await state.update_data(rate="0")
        await state.set_state(AddLoan.payment)
        await message.answer(PROMPTS[LoanKind.INSTALLMENT]["payment"])
        return
    await state.set_state(AddLoan.rate)
    await message.answer(PROMPTS[_kind(data)]["rate"])


@router.message(AddLoan.rate, F.text)
async def add_rate(message: Message, state: FSMContext) -> None:
    rate = parse_percent(message.text)
    if rate is None:
        await message.answer("Нужно число, например <code>24,9</code> или <code>0,8</code>.")
        return
    await state.update_data(rate=str(rate))
    await state.set_state(AddLoan.payment)
    await message.answer(PROMPTS[_kind(await state.get_data())]["payment"])


@router.message(AddLoan.payment, F.text)
async def add_payment(message: Message, state: FSMContext) -> None:
    amount = parse_rub(message.text, allow_zero=False)
    if amount is None:
        await message.answer("Нужна сумма, например <code>5 400</code>.")
        return
    await state.update_data(payment=amount)
    await state.set_state(AddLoan.first_due)
    await message.answer(PROMPTS[_kind(await state.get_data())]["first_due"])


@router.message(AddLoan.first_due, F.text)
async def add_first_due(message: Message, state: FSMContext, user: User) -> None:
    today = user_today(user.timezone)
    due = parse_date(message.text, today)
    if due is None:
        await message.answer("Нужна дата <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.")
        return
    if due < today:
        await message.answer("Эта дата уже прошла. Если платёж просрочен — укажите сегодняшнюю "
                             "дату, а пени добавьте потом в карточке кредита.")
        return
    await state.update_data(first_due=due.isoformat())
    data = await state.get_data()
    if _kind(data) in (LoanKind.BANK, LoanKind.INSTALLMENT):
        await state.set_state(AddLoan.insurance)
        await message.answer("Есть ли в платеже страховка или комиссия? Пришлите сумму в месяц "
                             "или нажмите «Нет».", reply_markup=skip_keyboard("lins:0", "Нет"))
        return
    await _ask_pay_method(message, state)


async def _ask_pay_method(message: Message, state: FSMContext) -> None:
    await state.set_state(AddLoan.pay_method)
    await message.answer("Как платите?", reply_markup=pay_method_keyboard())


@router.message(AddLoan.insurance, F.text)
async def add_insurance(message: Message, state: FSMContext) -> None:
    amount = parse_rub(message.text)
    data = await state.get_data()
    if amount is None or amount >= data["payment"]:
        await message.answer("Нужна сумма меньше платежа, например <code>350</code>.")
        return
    await state.update_data(insurance=amount)
    await _ask_pay_method(message, state)


@router.callback_query(AddLoan.insurance, F.data == "lins:0")
async def add_no_insurance(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(insurance=0)
    await _ask_pay_method(callback.message, state)


@router.callback_query(AddLoan.pay_method, F.data.startswith("lpay:"))
async def add_pay_method(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pay_method=callback.data.removeprefix("lpay:"))
    await state.set_state(AddLoan.confirm)
    await callback.answer()
    await callback.message.answer(loan_draft_text(await state.get_data()),
                                  reply_markup=confirm_loan_keyboard())


@router.callback_query(AddLoan.confirm, F.data == "ladd:save")
async def add_save(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                   user: User) -> None:
    data = await state.get_data()
    draft = LoanDraft(
        lender=data["lender"],
        kind=_kind(data),
        principal=data["principal"],
        rate=Decimal(data["rate"]),
        rate_unit=RateUnit(data["rate_unit"]),
        payment=data["payment"],
        first_due=date.fromisoformat(data["first_due"]),
        pay_method=data["pay_method"],
        insurance=data.get("insurance", 0),
    )
    today = user_today(user.timezone)
    loan = await create_loan(session, user, draft, today, utcnow())
    await state.clear()
    await callback.answer("Сохранено")
    await drop_buttons(callback)
    loan = await _refreshed(session, loan)
    await callback.message.answer("✅ Кредит добавлен.\n\n" + loan_card(loan, today),
                                  reply_markup=loan_card_keyboard(loan))
    await callback.message.answer("Добавить ещё один?", reply_markup=loans_keyboard([]))


@router.callback_query(AddLoan.confirm, F.data == "ladd:cancel")
async def add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await drop_buttons(callback)
    await callback.message.answer("Отменено.", reply_markup=main_menu())


# --- payments -----------------------------------------------------------------------------


@router.callback_query(LoanCb.filter(F.action == "pay"))
async def pay_start(callback: CallbackQuery, callback_data: LoanCb, session: AsyncSession,
                    user: User) -> None:
    loan = await _own_loan(session, user, callback_data.loan_id)
    if loan is None or loan.status != "active":
        await callback.answer("Кредит не найден.")
        return
    await callback.answer()
    due = next_due(loan)
    if due is None:
        await callback.message.answer(
            "Запланированных платежей нет. Внести деньги сверх графика — «⏩ Досрочно»."
        )
        return
    await callback.message.answer(
        f"Платёж по «{escape(loan.lender)}» на {due.due_date:%d.%m}: "
        f"{format_rub(due.amount_minor - due.paid_minor)}",
        reply_markup=due_pay_keyboard(due),
    )


@router.callback_query(DueCb.filter(F.action == "part"))
async def pay_other_amount(callback: CallbackQuery, callback_data: DueCb, state: FSMContext,
                           session: AsyncSession, user: User) -> None:
    due = await session.get(LoanDue, callback_data.due_id)
    loan = await _own_loan(session, user, due.loan_id) if due is not None else None
    if loan is None or due.status == "paid":
        await callback.answer("Уже отмечено.")
        return
    await state.set_state(LoanInput.pay_amount)
    await state.update_data(due_id=due.id)
    await callback.answer()
    await callback.message.answer(
        f"Сколько внесли по «{escape(loan.lender)}»? По графику — "
        f"{format_rub(due.amount_minor - due.paid_minor)}. Если больше — остаток пойдёт "
        "в досрочное погашение.\n/cancel — отмена"
    )


def _monthly_annuity(loan: Loan) -> bool:
    return loan.schedule == Schedule.MONTHLY and LoanKind(loan.kind) != LoanKind.CARD


@router.message(LoanInput.pay_amount, F.text)
async def pay_amount(message: Message, state: FSMContext, session: AsyncSession,
                     user: User) -> None:
    amount = parse_rub(message.text, allow_zero=False)
    if amount is None:
        await message.answer("Нужна сумма, например <code>3 000</code>.")
        return
    data = await state.get_data()
    due = await session.get(LoanDue, data["due_id"])
    loan = await _own_loan(session, user, due.loan_id) if due is not None else None
    if loan is None or due.status == "paid" or loan.status != "active":
        await state.clear()
        await message.answer("Этот платёж уже отмечен.")
        return
    today, now = user_today(user.timezone), utcnow()
    left = due.amount_minor - due.paid_minor
    result = await pay_due(session, loan, due, min(amount, left), today, "manual", now)
    if amount < left:
        await state.clear()
        await message.answer(
            f"Записал частичный платёж {format_rub(amount)}. По графику осталось внести "
            f"{format_rub(left - amount)} до {due.due_date:%d.%m}."
        )
        return
    extra = amount - left
    if result.closed or extra == 0:
        await state.clear()
        text = f"✅ {escape(loan.lender)}: оплачено {format_rub(min(amount, left))}."
        if result.closed:
            text += "\n🎉 Кредит полностью погашен!"
        await message.answer(text)
        return
    await message.answer(f"✅ Платёж {format_rub(left)} записан. "
                         f"Сверх графика: {format_rub(extra)}.")
    await _early_next(message, state, session, user, loan, extra)


async def _early_next(message: Message, state: FSMContext, session: AsyncSession, user: User,
                      loan: Loan, amount: int) -> None:
    if _monthly_annuity(loan):
        await state.set_state(LoanInput.early_mode)
        await state.update_data(loan_id=loan.id, amount=amount)
        await message.answer("Как пересчитать график после досрочного погашения?",
                             reply_markup=early_mode_keyboard())
        return
    await state.clear()
    result = await early_repay(session, loan, amount, EarlyMode.TERM,
                               user_today(user.timezone), utcnow())
    await message.answer(early_result_text(loan, result))


@router.callback_query(LoanCb.filter(F.action == "early"))
async def early_start(callback: CallbackQuery, callback_data: LoanCb, state: FSMContext,
                      session: AsyncSession, user: User) -> None:
    loan = await _own_loan(session, user, callback_data.loan_id)
    if loan is None or loan.status != "active":
        await callback.answer("Кредит не найден.")
        return
    await state.set_state(LoanInput.early_amount)
    await state.update_data(loan_id=loan.id)
    await callback.answer()
    owed = loan.principal_minor + interest_now(loan, user_today(user.timezone))
    await callback.message.answer(
        f"Сколько вносите досрочно в «{escape(loan.lender)}»? Для полного погашения — "
        f"{format_rub(owed)}.\n/cancel — отмена"
    )


@router.message(LoanInput.early_amount, F.text)
async def early_amount(message: Message, state: FSMContext, session: AsyncSession,
                       user: User) -> None:
    amount = parse_rub(message.text, allow_zero=False)
    if amount is None:
        await message.answer("Нужна сумма, например <code>10 000</code>.")
        return
    loan = await _own_loan(session, user, (await state.get_data())["loan_id"])
    if loan is None or loan.status != "active":
        await state.clear()
        await message.answer("Кредит не найден.")
        return
    await _early_next(message, state, session, user, loan, amount)


@router.callback_query(LoanInput.early_mode, EarlyCb.filter())
async def early_mode(callback: CallbackQuery, callback_data: EarlyCb, state: FSMContext,
                     session: AsyncSession, user: User) -> None:
    data = await state.get_data()
    await state.clear()
    loan = await _own_loan(session, user, data["loan_id"])
    await callback.answer()
    await drop_buttons(callback)
    if loan is None or loan.status != "active":
        await callback.message.answer("Кредит не найден.")
        return
    result = await early_repay(session, loan, data["amount"], EarlyMode(callback_data.mode),
                               user_today(user.timezone), utcnow())
    await callback.message.answer(early_result_text(loan, result))


# --- close / delete -----------------------------------------------------------------------


@router.callback_query(LoanCb.filter(F.action.in_({"close", "delete"})))
async def ask_close_delete(callback: CallbackQuery, callback_data: LoanCb,
                           session: AsyncSession, user: User) -> None:
    loan = await _own_loan(session, user, callback_data.loan_id)
    if loan is None:
        await callback.answer("Кредит не найден.")
        return
    await callback.answer()
    if callback_data.action == "close":
        question = (f"Кредит «{escape(loan.lender)}» полностью погашен? "
                    "Он уйдёт из списка и напоминаний.")
    else:
        question = (f"Удалить «{escape(loan.lender)}» со всей историей платежей? "
                    "Если кредит погашен — лучше «🏁 Закрыт».")
    yes = LoanCb(action=f"{callback_data.action}_yes", loan_id=loan.id)
    await show(callback, question, yes_no_keyboard(yes, LoanCb(action="open", loan_id=loan.id)))


@router.callback_query(LoanCb.filter(F.action.in_({"close_yes", "delete_yes"})))
async def do_close_delete(callback: CallbackQuery, callback_data: LoanCb,
                          session: AsyncSession, user: User) -> None:
    loan = await _own_loan(session, user, callback_data.loan_id)
    if loan is None:
        await callback.answer("Кредит не найден.")
        return
    if callback_data.action == "close_yes":
        await close_loan(session, loan, utcnow())
        await callback.answer("🎉 Закрыт")
    else:
        await session.delete(loan)
        await session.flush()
        await callback.answer("Удалён")
    text, markup = await _list_view(session, user)
    await show(callback, text, markup)


# --- edit ---------------------------------------------------------------------------------


EDIT_PROMPTS = {
    "principal": "Текущий долг (основной долг без процентов)?",
    "payment": "Новый платёж?",
    "due": "Дата ближайшего платежа? <code>ДД.ММ</code>",
    "rate": "Новая ставка, %?",
    "insurance": "Страховка/комиссии в платеже, ₽ в месяц? <code>0</code> — нет.",
    "penalty": "Пени за просрочку, % годовых? Например <code>20</code>. <code>-</code> — не знаю.",
}


@router.callback_query(LoanCb.filter(F.action == "edit"))
async def edit_menu(callback: CallbackQuery, callback_data: LoanCb, session: AsyncSession,
                    user: User) -> None:
    loan = await _own_loan(session, user, callback_data.loan_id)
    if loan is None or loan.status != "active":
        await callback.answer("Кредит не найден.")
        return
    await show(callback, f"Что изменить в «{escape(loan.lender)}»?", loan_edit_keyboard(loan.id))
    await callback.answer()


@router.callback_query(EditCb.filter())
async def edit_field(callback: CallbackQuery, callback_data: EditCb, state: FSMContext,
                     session: AsyncSession, user: User) -> None:
    loan = await _own_loan(session, user, callback_data.loan_id)
    if loan is None or callback_data.field not in EDIT_FIELDS:
        await callback.answer("Кредит не найден.")
        return
    await callback.answer()
    await state.set_state(LoanInput.edit_value)
    await state.update_data(loan_id=loan.id, field=callback_data.field)
    if callback_data.field == "method":
        await callback.message.answer("Как платите?", reply_markup=pay_method_keyboard())
        return
    await callback.message.answer(EDIT_PROMPTS[callback_data.field] + "\n/cancel — отмена")


def _apply_edit(loan: Loan, field: str, text: str, today: date) -> str | None:
    """Applies the new value; returns an error message if it is invalid."""
    if field in ("principal", "payment", "insurance"):
        amount = parse_rub(text, allow_zero=field == "insurance")
        if amount is None:
            return "Нужна сумма, например <code>5 000</code>."
        if field == "principal":
            # The user's figure is the truth now: interest starts from today
            loan.principal_minor = amount
            loan.unpaid_interest_minor = 0
            loan.interest_from = today
        elif field == "payment":
            loan.payment_minor = amount
        else:
            if amount >= loan.payment_minor:
                return "Страховка должна быть меньше платежа."
            loan.insurance_minor = amount
        return None
    if field == "due":
        due = parse_date(text, today)
        if due is None or due < today:
            return "Нужна будущая дата <code>ДД.ММ</code>."
        loan.first_due = due
        if loan.schedule == Schedule.MONTHLY:
            loan.payment_day = due.day
        return None
    if field == "rate":
        rate = parse_percent(text)
        if rate is None:
            return "Нужно число, например <code>24,9</code>."
        if accrues(loan):
            # Interest so far was at the old rate
            loan.unpaid_interest_minor = interest_now(loan, today)
            loan.interest_from = today
        loan.rate = str(rate)
        return None
    if field == "penalty":
        if text.strip() == "-":
            loan.penalty_rate = None
            return None
        rate = parse_percent(text)
        if rate is None:
            return "Нужно число, например <code>20</code>, или <code>-</code>."
        loan.penalty_rate = str(rate)
        return None
    return "Не понял."


async def _after_edit(message: Message, state: FSMContext, session: AsyncSession, user: User,
                      loan: Loan, field: str) -> None:
    await state.clear()
    today = user_today(user.timezone)
    if field in ("principal", "payment", "due", "insurance", "rate"):
        if field == "due" and loan.schedule == Schedule.ONCE:
            # A single-payment loan moved to another date: replace its due
            for d in list(loan.dues):
                if d.status != "paid":
                    await session.delete(d)
            await session.flush()
        await regenerate_dues(session, loan, today)
    loan = await _refreshed(session, loan)
    await message.answer("Сохранено.\n\n" + loan_card(loan, today),
                         reply_markup=loan_card_keyboard(loan))


@router.message(LoanInput.edit_value, F.text)
async def edit_value(message: Message, state: FSMContext, session: AsyncSession,
                     user: User) -> None:
    data = await state.get_data()
    loan = await _own_loan(session, user, data["loan_id"])
    if loan is None or data["field"] == "method":
        await state.clear()
        await message.answer("Отменено.")
        return
    error = _apply_edit(loan, data["field"], message.text, user_today(user.timezone))
    if error:
        await message.answer(error)
        return
    await _after_edit(message, state, session, user, loan, data["field"])


@router.callback_query(LoanInput.edit_value, F.data.startswith("lpay:"))
async def edit_method(callback: CallbackQuery, state: FSMContext, session: AsyncSession,
                      user: User) -> None:
    data = await state.get_data()
    loan = await _own_loan(session, user, data["loan_id"])
    await callback.answer()
    if loan is None:
        await state.clear()
        return
    loan.pay_method = callback.data.removeprefix("lpay:")
    await _after_edit(callback.message, state, session, user, loan, "method")
