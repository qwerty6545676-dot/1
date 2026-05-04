"""Tests for top-N universe selection (mocked REST)."""

from __future__ import annotations

from typing import Any

import pytest

from walls.universe import select_top_n


class _MockRest:
    def __init__(
        self,
        info: dict[str, Any],
        tickers: list[dict[str, Any]],
    ) -> None:
        self._info = info
        self._tickers = tickers

    async def exchange_info(self) -> dict[str, Any]:
        return self._info

    async def ticker_24hr(self) -> list[dict[str, Any]]:
        return self._tickers


@pytest.mark.asyncio
async def test_select_top_n_filters_by_quote_and_status() -> None:
    info = {
        "symbols": [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC",
             "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "DEADUSDT", "baseAsset": "DEAD", "quoteAsset": "USDT",
             "status": "BREAK", "isSpotTradingAllowed": True},
            {"symbol": "MARGINUSDT", "baseAsset": "MARG", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": False},
        ],
    }
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "1000000000", "lastPrice": "50000"},
        {"symbol": "ETHUSDT", "quoteVolume":  "500000000", "lastPrice":  "3000"},
        {"symbol": "ETHBTC",  "quoteVolume":     "100000", "lastPrice":   "0.06"},
        {"symbol": "DEADUSDT", "quoteVolume": "10000", "lastPrice": "1"},
    ]
    rest = _MockRest(info, tickers)
    rows = await select_top_n(rest, top_n=10, quote_assets=("USDT",))
    syms = [r.symbol for r in rows]
    assert syms == ["BTCUSDT", "ETHUSDT"]  # only USDT, only TRADING+spot, sorted by volume


@pytest.mark.asyncio
async def test_select_top_n_drops_invalid_rows() -> None:
    info = {
        "symbols": [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": True},
        ],
    }
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "0", "lastPrice": "0"},
    ]
    rows = await select_top_n(_MockRest(info, tickers), top_n=10, quote_assets=("USDT",))
    assert rows == []
