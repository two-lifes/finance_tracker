from datetime import date
from decimal import Decimal

import httpx

from bot.services.fx import FxParseError, parse_cbr_daily

CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"


class CbrError(RuntimeError):
    pass


async def fetch_daily(
    on: date | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[date, dict[str, Decimal]]:
    params = {"date_req": f"{on:%d/%m/%Y}"} if on else None
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            response = await client.get(CBR_DAILY_URL, params=params)
            response.raise_for_status()
        return parse_cbr_daily(response.content)
    except (httpx.HTTPError, FxParseError) as exc:
        raise CbrError(str(exc) or type(exc).__name__) from exc
