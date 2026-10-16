from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def show(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Replaces the tapped message with ``text``; sends a new one if it can't be edited.

    Telegram may hand over an InaccessibleMessage (old or deleted), which has no edit methods.
    """
    if not isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=markup)
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        # Re-rendering identical content (e.g. a double tap) is a harmless no-op
        if "message is not modified" not in str(exc):
            raise


async def drop_buttons(callback: CallbackQuery) -> None:
    """Removes the inline keyboard from the tapped message, if it is still editable."""
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
