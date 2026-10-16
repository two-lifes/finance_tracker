from datetime import UTC, datetime

import pytest

from bot import clock
from tests.harness import make_harness

# 17.10.2026 is a Saturday; 07:00 UTC is 10:00 in Moscow
NOW = datetime(2026, 10, 17, 7, 0, tzinfo=UTC)


@pytest.fixture
def now():
    """A controllable clock: ``now.set(datetime)`` moves time for the bot."""

    class Clock:
        value = NOW

        def set(self, value: datetime) -> None:
            self.value = value

    c = Clock()
    clock.set_clock(lambda: c.value)
    yield c
    clock.set_clock(lambda: datetime.now(UTC))


@pytest.fixture
async def h(tmp_path, now):
    harness, engine = await make_harness(tmp_path / "t.db", now)
    yield harness
    await engine.dispose()


@pytest.fixture(autouse=True)
def cbr(monkeypatch):
    """CBR is never called for real in tests; ``cbr.rates`` / ``cbr.down`` control it."""
    from datetime import date
    from decimal import Decimal

    from bot.integrations.cbr import CbrError

    class FakeCbr:
        rates = {"USD": Decimal("80"), "EUR": Decimal("90")}
        down = False
        calls = 0

        async def __call__(self, on: date | None = None, transport=None):
            self.calls += 1
            if self.down:
                raise CbrError("unavailable")
            return on, dict(self.rates)

    fake = FakeCbr()
    monkeypatch.setattr("bot.services.external.fetch_daily", fake)
    monkeypatch.setattr("bot.scheduler.fetch_daily", fake)
    return fake
