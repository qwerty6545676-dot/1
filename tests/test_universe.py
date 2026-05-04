"""Tests for mode-based universe selection (mocked REST)."""

from __future__ import annotations

from typing import Any

import pytest

from walls.settings import ModeCfg
from walls.universe import select_for_modes


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


def _info(*pairs: tuple[str, str, bool]) -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": sym,
                "baseAsset": sym.replace(quote, "") if sym.endswith(quote) else sym[:-3],
                "quoteAsset": quote,
                "status": "TRADING" if active else "BREAK",
                "isSpotTradingAllowed": True,
            }
            for sym, quote, active in pairs
        ],
    }


def _btc() -> ModeCfg:
    return ModeCfg(
        name="btc", enabled=True, min_wall_usd=1_000_000,
        symbols=("BTCUSDT",), top_n=0, exclude_bases=(),
    )


def _eth() -> ModeCfg:
    return ModeCfg(
        name="eth", enabled=True, min_wall_usd=500_000,
        symbols=("ETHUSDT",), top_n=0, exclude_bases=(),
    )


def _alts(top_n: int = 5) -> ModeCfg:
    return ModeCfg(
        name="alts", enabled=True, min_wall_usd=150_000,
        symbols=(), top_n=top_n, exclude_bases=("BTC", "ETH"),
    )


@pytest.mark.asyncio
async def test_btc_eth_modes_pick_only_their_symbols() -> None:
    info = _info(
        ("BTCUSDT", "USDT", True),
        ("ETHUSDT", "USDT", True),
        ("SOLUSDT", "USDT", True),
    )
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "1000000000", "lastPrice": "60000"},
        {"symbol": "ETHUSDT", "quoteVolume": "500000000",  "lastPrice": "3000"},
        {"symbol": "SOLUSDT", "quoteVolume": "100000000",  "lastPrice": "150"},
    ]
    btc_only = ModeCfg(
        name="btc", enabled=True, min_wall_usd=1_000_000,
        symbols=("BTCUSDT",), top_n=0, exclude_bases=(),
    )
    rows = await select_for_modes(
        _MockRest(info, tickers),
        modes=(btc_only,),
        quote_assets=("USDT",),
    )
    assert [r[0].symbol for r in rows] == ["BTCUSDT"]
    assert all(r[1].name == "btc" for r in rows)


@pytest.mark.asyncio
async def test_alts_mode_excludes_btc_eth_and_sorts_by_volume() -> None:
    info = _info(
        ("BTCUSDT", "USDT", True),
        ("ETHUSDT", "USDT", True),
        ("SOLUSDT", "USDT", True),
        ("XRPUSDT", "USDT", True),
        ("DOGEUSDT", "USDT", True),
    )
    tickers = [
        {"symbol": "BTCUSDT",  "quoteVolume": "1000000000", "lastPrice": "60000"},
        {"symbol": "ETHUSDT",  "quoteVolume": "500000000",  "lastPrice": "3000"},
        {"symbol": "SOLUSDT",  "quoteVolume": "200000000",  "lastPrice": "150"},
        {"symbol": "XRPUSDT",  "quoteVolume": "300000000",  "lastPrice": "0.5"},
        {"symbol": "DOGEUSDT", "quoteVolume": "50000000",   "lastPrice": "0.1"},
    ]
    rows = await select_for_modes(
        _MockRest(info, tickers),
        modes=(_alts(top_n=10),),
        quote_assets=("USDT",),
    )
    syms = [r[0].symbol for r in rows]
    # BTCUSDT and ETHUSDT excluded; remaining sorted by volume desc
    assert syms == ["XRPUSDT", "SOLUSDT", "DOGEUSDT"]


@pytest.mark.asyncio
async def test_three_modes_combined_no_duplicates() -> None:
    info = _info(
        ("BTCUSDT", "USDT", True),
        ("ETHUSDT", "USDT", True),
        ("SOLUSDT", "USDT", True),
        ("XRPUSDT", "USDT", True),
    )
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "1000000000", "lastPrice": "60000"},
        {"symbol": "ETHUSDT", "quoteVolume": "500000000",  "lastPrice": "3000"},
        {"symbol": "SOLUSDT", "quoteVolume": "200000000",  "lastPrice": "150"},
        {"symbol": "XRPUSDT", "quoteVolume": "300000000",  "lastPrice": "0.5"},
    ]
    rows = await select_for_modes(
        _MockRest(info, tickers),
        modes=(_btc(), _eth(), _alts(top_n=10)),
        quote_assets=("USDT",),
    )
    syms = [r[0].symbol for r in rows]
    modes = [r[1].name for r in rows]
    assert syms == ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]
    assert modes == ["btc", "eth", "alts", "alts"]


@pytest.mark.asyncio
async def test_top_n_caps_alts_count() -> None:
    info = _info(
        ("XRPUSDT",  "USDT", True),
        ("SOLUSDT",  "USDT", True),
        ("DOGEUSDT", "USDT", True),
    )
    tickers = [
        {"symbol": "XRPUSDT",  "quoteVolume": "300000000", "lastPrice": "0.5"},
        {"symbol": "SOLUSDT",  "quoteVolume": "200000000", "lastPrice": "150"},
        {"symbol": "DOGEUSDT", "quoteVolume": "100000000", "lastPrice": "0.1"},
    ]
    rows = await select_for_modes(
        _MockRest(info, tickers),
        modes=(_alts(top_n=2),),
        quote_assets=("USDT",),
    )
    assert [r[0].symbol for r in rows] == ["XRPUSDT", "SOLUSDT"]


@pytest.mark.asyncio
async def test_disabled_modes_contribute_nothing() -> None:
    info = _info(("BTCUSDT", "USDT", True), ("ETHUSDT", "USDT", True))
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "1000000000", "lastPrice": "60000"},
        {"symbol": "ETHUSDT", "quoteVolume": "500000000",  "lastPrice": "3000"},
    ]
    btc_off = ModeCfg(
        name="btc", enabled=False, min_wall_usd=1_000_000,
        symbols=("BTCUSDT",), top_n=0, exclude_bases=(),
    )
    rows = await select_for_modes(
        _MockRest(info, tickers),
        modes=(btc_off, _eth()),
        quote_assets=("USDT",),
    )
    assert [r[0].symbol for r in rows] == ["ETHUSDT"]
