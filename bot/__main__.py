import asyncio
import contextlib
import logging
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand
from alembic import command
from alembic.config import Config as AlembicConfig

from bot.app import build_dispatcher
from bot.clock import utcnow
from bot.config import Config, ConfigError, load_config
from bot.db.session import create_engine, create_sessionmaker
from bot.integrations.subs_client import HttpSubsClient
from bot.scheduler import run_scheduler
from bot.services.deployments import record_startup

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="today", description="Сколько можно тратить сегодня"),
    BotCommand(command="loans", description="Кредиты"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


def run_migrations(config: Config) -> None:
    alembic_cfg = AlembicConfig("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", config.db_url)
    # Otherwise alembic.ini's logging config would silence the bot's INFO logs
    alembic_cfg.attributes["configure_logger"] = False
    command.upgrade(alembic_cfg, "head")


async def main(config: Config) -> None:
    engine = create_engine(config.db_url)
    sessionmaker = create_sessionmaker(engine)
    async with sessionmaker() as db, db.begin():
        await record_startup(
            db, config.app_version, config.app_title, config.failed_version, utcnow()
        )
    log.info("Version: %s", config.app_version or "local")

    subs_client = None
    if config.subs_api_url and config.subs_api_token:
        subs_client = HttpSubsClient(config.subs_api_url, config.subs_api_token)
        log.info("Subscription API: %s", config.subs_api_url)
    else:
        log.info("Subscriptions disabled: SUBS_API_URL or SUBS_API_TOKEN is not set")

    session = AiohttpSession(proxy=config.proxy_url) if config.proxy_url else None
    bot = Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher(config.allowed_user_id)
    db_lock = asyncio.Lock()
    dp["sessionmaker"] = sessionmaker
    dp["db_lock"] = db_lock
    dp["subs_client"] = subs_client

    scheduler = None
    try:
        try:
            await bot.set_my_commands(COMMANDS)
        except TelegramNetworkError as exc:
            # The command menu is a convenience; polling will retry the network itself
            log.warning("Could not set bot commands: %s", exc)
        scheduler = asyncio.create_task(
            run_scheduler(sessionmaker, bot, db_lock, subs_client), name="scheduler"
        )
        await dp.start_polling(bot)
    finally:
        if scheduler is not None:
            scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
        if subs_client is not None:
            await subs_client.aclose()
        await bot.session.close()
        await engine.dispose()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)
    # Alembic's async env calls asyncio.run itself, so it must run outside our event loop
    run_migrations(config)
    asyncio.run(main(config))


if __name__ == "__main__":
    run()
