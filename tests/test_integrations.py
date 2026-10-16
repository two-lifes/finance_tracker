from datetime import date
from decimal import Decimal

import httpx
import pytest

from bot.integrations.cbr import CbrError, fetch_daily
from bot.integrations.subs_client import HttpSubsClient, SubsApiError
from bot.services.fx import pick_rate

CBR_XML = (
    '<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="16.10.2026" '
    'name="Foreign Currency Market"><Valute ID="R01235"><NumCode>840</NumCode>'
    "<CharCode>USD</CharCode><Nominal>1</Nominal><Name>Доллар США</Name>"
    "<Value>80,5000</Value><VunitRate>80,5</VunitRate></Valute>"
    '<Valute ID="R01820"><NumCode>392</NumCode><CharCode>JPY</CharCode>'
    "<Nominal>100</Nominal><Name>Иен</Name><Value>55,0000</Value></Valute></ValCurs>"
).encode("cp1251")


async def test_cbr_parsing():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=CBR_XML))
    on, rates = await fetch_daily(date(2026, 10, 16), transport=transport)
    assert on == date(2026, 10, 16)
    assert rates == {"USD": Decimal("80.5"), "JPY": Decimal("0.55")}


async def test_cbr_errors_are_wrapped():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(CbrError):
        await fetch_daily(transport=transport)


def test_pick_rate_falls_back_to_last_known():
    history = [(date(2026, 10, 10), Decimal("80")), (date(2026, 10, 14), Decimal("81"))]
    assert pick_rate(history, date(2026, 10, 17)) == Decimal("81")
    assert pick_rate(history, date(2026, 10, 12)) == Decimal("80")
    assert pick_rate(history, date(2026, 1, 1)) == Decimal("80")
    assert pick_rate([], date(2026, 1, 1)) is None


async def test_subs_client_sends_token_and_params():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = HttpSubsClient("http://subs-api:8080/", "secret", transport=transport)
    await client.upcoming(date(2026, 10, 25))
    await client.payments(date(2026, 9, 1), date(2026, 10, 17))
    await client.subscriptions()
    await client.aclose()
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert str(seen[0].url) == "http://subs-api:8080/api/v1/subscriptions/upcoming?until=2026-10-25"
    assert seen[1].url.params["from"] == "2026-09-01"
    assert seen[2].url.params["status"] == "active"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(401, json={"error": "x"}), httpx.Response(200, text="oops"),
     httpx.Response(200, json={"not": "list"})],
)
async def test_subs_client_errors(response):
    client = HttpSubsClient("http://x", "t", transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(SubsApiError):
        await client.subscriptions()


async def test_subs_client_network_error():
    def boom(request):
        raise httpx.ConnectError("refused")

    client = HttpSubsClient("http://x", "t", transport=httpx.MockTransport(boom))
    with pytest.raises(SubsApiError):
        await client.subscriptions()
