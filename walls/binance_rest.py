"""Thin async REST client for Binance public market data.

Targets ``data-api.binance.vision`` by default — the public read-only CDN
endpoint that works in regions where api.binance.com is geo-blocked.
"""

from __future__ import annotations

from typing import Any

import aiohttp


class BinanceRestError(RuntimeError):
    pass


class BinanceRest:
    def __init__(self, base_url: str, timeout_sec: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> BinanceRest:
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise BinanceRestError("BinanceRest used outside of `async with`")
        return self._session

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        sess = self._ensure_session()
        url = f"{self._base}{path}"
        async with sess.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise BinanceRestError(f"GET {url} -> {resp.status}: {text[:200]}")
            return await resp.json()

    async def exchange_info(self) -> dict[str, Any]:
        return await self._get("/api/v3/exchangeInfo")

    async def ticker_24hr(self) -> list[dict[str, Any]]:
        data = await self._get("/api/v3/ticker/24hr")
        if not isinstance(data, list):
            raise BinanceRestError("ticker/24hr did not return a list")
        return data

    async def depth(self, symbol: str, limit: int = 5000) -> dict[str, Any]:
        return await self._get(
            "/api/v3/depth",
            params={"symbol": symbol.upper(), "limit": limit},
        )
