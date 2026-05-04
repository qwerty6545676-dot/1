"""WebSocket subscriber for Binance public depth and trade streams.

Each stream class runs an independent connection (one per symbol). This keeps
reconnect handling simple and isolates symbols from each other — if one
stream stalls, the rest keep running.

Targets ``wss://data-stream.binance.vision`` by default — the public read-only
CDN endpoint that works in regions where stream.binance.com is geo-blocked.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from . import log

_log = log.get("ws")


class DepthStream:
    """Single-symbol depth-diff stream.

    Yields parsed event dicts. On disconnect, raises ``ConnectionClosed`` so
    the caller can decide whether to resync the local book.
    """

    def __init__(
        self,
        ws_base: str,
        symbol: str,
        *,
        update_speed_ms: int = 100,
    ) -> None:
        self._url = f"{ws_base.rstrip('/')}/ws/{symbol.lower()}@depth@{update_speed_ms}ms"
        self.symbol = symbol.upper()

    async def connect(self) -> ClientConnection:
        # Binance servers send pings; we still set a reasonable client-side
        # ping interval to detect dead sockets quickly.
        return await websockets.connect(
            self._url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=2**22,
        )

    async def stream(self) -> AsyncIterator[dict]:
        ws = await self.connect()
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    _log.warning("%s: dropping non-JSON frame", self.symbol)
                    continue
                yield evt
        finally:
            try:
                await ws.close()
            except Exception:
                pass


class TradeStream:
    """Single-symbol aggregated-trade stream.

    Subscribes to ``<symbol>@trade`` and yields parsed event dicts. Used by
    the iceberg detector to verify that levels were actually traded (not just
    cancelled / replaced).
    """

    def __init__(self, ws_base: str, symbol: str) -> None:
        self._url = f"{ws_base.rstrip('/')}/ws/{symbol.lower()}@trade"
        self.symbol = symbol.upper()

    async def connect(self) -> ClientConnection:
        return await websockets.connect(
            self._url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=2**20,
        )

    async def stream(self) -> AsyncIterator[dict]:
        ws = await self.connect()
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    _log.warning("%s trades: dropping non-JSON frame", self.symbol)
                    continue
                yield evt
        finally:
            try:
                await ws.close()
            except Exception:
                pass


async def run_with_reconnect(
    ws_base: str,
    symbol: str,
    on_event: callable[[dict], asyncio.Future[None] | None],
    on_disconnect: callable[[], asyncio.Future[None] | None],
    *,
    reconnect_delay_sec: float = 5.0,
) -> None:
    """Run a depth-diff stream forever, reconnecting on failure.

    ``on_event`` is called for every event. ``on_disconnect`` is called once
    per disconnect — typically the orderbook owner uses it to mark the book
    out of sync and trigger a resync.
    """
    stream = DepthStream(ws_base, symbol)
    while True:
        try:
            async for evt in stream.stream():
                res = on_event(evt)
                if asyncio.iscoroutine(res):
                    await res
        except (ConnectionClosed, OSError) as e:
            _log.warning("%s: WS disconnected (%s); reconnecting in %.1fs",
                         symbol, e.__class__.__name__, reconnect_delay_sec)
        except Exception as e:
            _log.exception("%s: unexpected WS error: %s", symbol, e)
        else:
            _log.info("%s: stream ended cleanly; reconnecting", symbol)

        res = on_disconnect()
        if asyncio.iscoroutine(res):
            await res
        await asyncio.sleep(reconnect_delay_sec)


async def run_trade_stream_with_reconnect(
    ws_base: str,
    symbol: str,
    on_event: callable[[dict], asyncio.Future[None] | None],
    *,
    reconnect_delay_sec: float = 5.0,
) -> None:
    """Run a ``@trade`` stream forever, reconnecting on failure.

    Trade losses across reconnects are acceptable — the iceberg verification
    window is short (≤2 s), so missing a few seconds of trades just means a
    handful of regen events get rejected as unconfirmed.
    """
    stream = TradeStream(ws_base, symbol)
    while True:
        try:
            async for evt in stream.stream():
                res = on_event(evt)
                if asyncio.iscoroutine(res):
                    await res
        except (ConnectionClosed, OSError) as e:
            _log.warning(
                "%s trades: WS disconnected (%s); reconnecting in %.1fs",
                symbol, e.__class__.__name__, reconnect_delay_sec,
            )
        except Exception as e:
            _log.exception("%s trades: unexpected WS error: %s", symbol, e)
        await asyncio.sleep(reconnect_delay_sec)
