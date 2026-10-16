"""Runs the real dispatcher against a temporary DB, a fake Telegram network and a fake
subscription API."""

import asyncio
import itertools
import re
from datetime import date
from functools import cache

from aiogram import Bot
from sqlalchemy import select

from bot.app import build_dispatcher
from bot.db.models import Base
from bot.db.session import create_engine, create_sessionmaker
from bot.integrations.subs_client import SubsApiError
from bot.scheduler import tick
from bot.services.money import NBSP
from tests.tg_fakes import OWNER_ID, FakeSession, callback_update, text_update

TZ = "Europe/Moscow"


class FakeSubsClient:
    def __init__(self) -> None:
        self.subs: list[dict] = []
        self.upcoming_items: list[dict] = []
        self.payment_items: list[dict] = []
        self.down = False
        self.calls = 0

    def _check(self) -> None:
        self.calls += 1
        if self.down:
            raise SubsApiError("connection refused")

    async def subscriptions(self) -> list[dict]:
        self._check()
        return self.subs

    async def upcoming(self, until: date) -> list[dict]:
        self._check()
        return [u for u in self.upcoming_items if u["charge_date"] <= until.isoformat()]

    async def payments(self, start: date, end: date) -> list[dict]:
        self._check()
        return [p for p in self.payment_items
                if start.isoformat() <= p["charge_date"] <= end.isoformat()]


class Harness:
    def __init__(self, dp, bot, net, sessionmaker, clock):
        self.dp, self.bot, self.net, self.sessionmaker = dp, bot, net, sessionmaker
        self.clock = clock
        self.db_lock = asyncio.Lock()
        self.subs = FakeSubsClient()

    async def feed(self, update) -> None:
        await self.dp.feed_update(
            self.bot, update, sessionmaker=self.sessionmaker, db_lock=self.db_lock,
            subs_client=self.subs,
        )

    async def text(self, text: str, user_id: int = OWNER_ID) -> str | None:
        before = len(self.net.texts())
        await self.feed(text_update(text, user_id))
        return self._last_since(before)

    def _last_since(self, before: int) -> str | None:
        # Tests compare plain text: no HTML tags, ordinary spaces instead of NBSP in amounts
        texts = self.net.texts()
        if len(texts) <= before:
            return None
        return re.sub(r"<[^>]+>", "", texts[-1]).replace(NBSP, " ")

    async def tap(self, data: str) -> str | None:
        before = len(self.net.texts())
        await self.feed(callback_update(data))
        return self._last_since(before)

    async def all(self, model):
        async with self.sessionmaker() as session:
            return list(await session.scalars(select(model).order_by(model.id)))

    async def tick(self) -> None:
        await tick(self.sessionmaker, self.bot, self.clock.value, self.db_lock, self.subs)


@cache
def shared_dispatcher():
    # Routers are module-level and can be attached to only one dispatcher per process
    return build_dispatcher(OWNER_ID)


# FSM state is keyed by bot id, so a fresh bot id per test isolates dialog state
_bot_ids = itertools.count(100)


async def make_harness(db_path, clock) -> tuple[Harness, object]:
    # A file DB: every update opens its own connection, and :memory: is per-connection
    engine = create_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    net = FakeSession()
    bot = Bot(token=f"{next(_bot_ids)}:TEST", session=net)
    return Harness(shared_dispatcher(), bot, net, create_sessionmaker(engine), clock), engine


async def onboard(h: Harness, balance: str = "20000", income: str = "23 50000") -> None:
    await h.text("/start")
    await h.tap(f"tz:{TZ}")
    await h.tap("shift:2/2")
    await h.tap("spos:0")
    await h.text(income)
    await h.text(balance)
