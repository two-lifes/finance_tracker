from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import main_menu

router = Router(name="fallback")


@router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    # Buttons from finished or abandoned dialogs; answer so Telegram stops the spinner
    await callback.answer("Эта кнопка уже неактуальна.")


@router.message()
async def unknown_message(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        await message.answer("Не понял ответ. Нажмите кнопку выше или /cancel — отменить.")
        return
    await message.answer(
        "Не понял. Пришлите остаток на карте числом, <code>+5000 премия</code> — доход, "
        "<code>нал 2000</code> — наличные. Или выберите действие в меню.",
        reply_markup=main_menu(),
    )
