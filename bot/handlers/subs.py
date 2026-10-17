"""Subscriptions in the budget: monthly total, share of income, the most expensive ones."""

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.clock import user_today
from bot.db.models import User
from bot.handlers.home import refresh_external
from bot.handlers.menu import menu_router
from bot.integrations.subs_client import SubsClient
from bot.keyboards import BTN_SUBS, main_menu
from bot.services.budget import build_view, monthly_income, monthly_loan_payments
from bot.services.external import rate_lookup
from bot.services.loan_ops import active_loans
from bot.services.priority import cancel_candidates
from bot.services.subscriptions import monthly_costs
from bot.views import subs_screen, subs_status


@menu_router.message(Command("subs"))
@menu_router.message(F.text == BTN_SUBS)
async def show_subs(
    message: Message, state: FSMContext, session: AsyncSession, user: User,
    subs_client: SubsClient | None,
) -> None:
    await state.clear()
    if user.timezone is None:
        await message.answer("Сначала короткая настройка: /start")
        return
    if subs_client is None:
        await message.answer("Сервис подписок не подключён.", reply_markup=main_menu())
        return
    await refresh_external(session, user, subs_client)
    today = user_today(user.timezone)
    view = await build_view(session, user, today)
    costs = monthly_costs(view.subs.subscriptions, today, await rate_lookup(session))
    cancel = cancel_candidates(view.shortfall.items) if view.shortfall is not None else []
    text = subs_screen(
        costs,
        await monthly_income(session, user, today),
        monthly_loan_payments(await active_loans(session, user)),
        cancel,
        subs_status(view, user, True),
        today,
    )
    await message.answer(text, reply_markup=main_menu())
