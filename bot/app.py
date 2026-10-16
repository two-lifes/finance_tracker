import logging

from aiogram import Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

from bot.handlers import build_router
from bot.middlewares.auth import WhitelistMiddleware
from bot.middlewares.db import DbSessionMiddleware

log = logging.getLogger(__name__)

ERROR_TEXT = "Что-то пошло не так, попробуйте ещё раз."


async def on_error(event: ErrorEvent) -> None:
    # The DB transaction is already rolled back by DbSessionMiddleware at this point
    log.exception("Unhandled error while processing update", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(ERROR_TEXT, show_alert=True)
        elif update.message is not None:
            await update.message.answer(ERROR_TEXT)
    except TelegramAPIError:
        log.exception("Could not tell the user about the error")


def build_dispatcher(allowed_user_id: int) -> Dispatcher:
    """Call once per process: handler routers are module-level and attach to one parent only.

    Before polling put into workflow data: ``sessionmaker``, ``db_lock`` (asyncio.Lock shared
    with the scheduler) and ``subs_client`` (None when subscriptions are off).
    """
    dp = Dispatcher()
    # Whitelist runs first so strangers never touch the database
    dp.update.outer_middleware(WhitelistMiddleware(allowed_user_id))
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.errors.register(on_error)
    dp.include_router(build_router())
    return dp
