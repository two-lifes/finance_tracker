"""Read-only client for the subscription bot's HTTP API."""

from datetime import date
from typing import Any, Protocol

import httpx


class SubsApiError(RuntimeError):
    pass


class SubsClient(Protocol):
    async def subscriptions(self) -> list[dict[str, Any]]: ...

    async def upcoming(self, until: date) -> list[dict[str, Any]]: ...

    async def payments(self, start: date, end: date) -> list[dict[str, Any]]: ...


class HttpSubsClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SubsApiError(f"{path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise SubsApiError(f"{path}: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SubsApiError(f"{path}: invalid JSON") from exc
        if not isinstance(data, list):
            raise SubsApiError(f"{path}: expected a JSON list")
        return data

    async def subscriptions(self) -> list[dict[str, Any]]:
        return await self._get("/api/v1/subscriptions", {"status": "active"})

    async def upcoming(self, until: date) -> list[dict[str, Any]]:
        return await self._get("/api/v1/subscriptions/upcoming", {"until": until.isoformat()})

    async def payments(self, start: date, end: date) -> list[dict[str, Any]]:
        return await self._get(
            "/api/v1/payments", {"from": start.isoformat(), "to": end.isoformat()}
        )

    async def aclose(self) -> None:
        await self._client.aclose()
