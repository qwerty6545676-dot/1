"""Bounded buffer of recent aggregated trades, indexed by ``(symbol, side, price)``.

The iceberg detector uses this to confirm that a level was actually *traded
through* — not just cancelled and replaced — before counting a regen. That
distinguishes real iceberg/refill orders from spoofers who repaint the book.

Binance ``@trade`` event semantics (relevant fields)::

    {
      "e": "trade", "E": <event_ts_ms>,
      "s": "BTCUSDT",
      "p": "<price>", "q": "<qty>", "T": <trade_ts_ms>,
      "m": true|false  # buyer is maker?
    }

Side mapping (which side of the book was filled):

- ``m=true``  → buyer was maker → seller initiated → trade hit a **bid**
- ``m=false`` → seller was maker → buyer initiated → trade hit an **ask**

Storage is a deque per ``(symbol, side, price)``, garbage collected by ts.
Memory is bounded by the configurable retention window.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class TradeFill:
    ts_ms: int
    qty: float


class TradeBuffer:
    """Ring buffer of recent fills per ``(symbol, side, price)``.

    ``retention_ms`` is the longest window any consumer might query — we drop
    fills older than that on every record. Single-threaded by design (asyncio
    loop), no locks.
    """

    def __init__(self, retention_ms: int = 30_000) -> None:
        self.retention_ms = max(1_000, int(retention_ms))
        # (symbol, side, price) -> deque[TradeFill]
        self._fills: dict[tuple[str, str, float], deque[TradeFill]] = defaultdict(deque)

    # ------------------------------------------------------------------- write
    def record(
        self,
        symbol: str,
        price: float,
        qty: float,
        ts_ms: int,
        buyer_is_maker: bool,
    ) -> None:
        """Record one trade fill.

        ``buyer_is_maker=True`` → bid was filled.
        ``buyer_is_maker=False`` → ask was filled.
        """
        if qty <= 0 or price <= 0 or ts_ms <= 0:
            return
        side = "bid" if buyer_is_maker else "ask"
        key = (symbol, side, price)
        d = self._fills[key]
        d.append(TradeFill(ts_ms=ts_ms, qty=qty))
        # In-place GC of head entries that are too old.
        cutoff = ts_ms - self.retention_ms
        while d and d[0].ts_ms < cutoff:
            d.popleft()

    # -------------------------------------------------------------------- read
    def total_qty_in_window(
        self,
        symbol: str,
        side: str,
        price: float,
        center_ts_ms: int,
        window_ms: int,
    ) -> float:
        """Sum of fill qty at ``(symbol, side, price)`` within ``±window_ms``."""
        key = (symbol, side, price)
        d = self._fills.get(key)
        if not d:
            return 0.0
        lo = center_ts_ms - window_ms
        hi = center_ts_ms + window_ms
        total = 0.0
        for f in d:
            if lo <= f.ts_ms <= hi:
                total += f.qty
        return total

    def has_fill(
        self,
        symbol: str,
        side: str,
        price: float,
        center_ts_ms: int,
        window_ms: int,
        min_qty: float = 0.0,
    ) -> bool:
        """``True`` iff some trade landed in the window and its total qty
        is at least ``min_qty``.

        The empty-buffer / no-trade case always returns False, regardless of
        ``min_qty`` (an absence of trades cannot satisfy any threshold).
        """
        total = self.total_qty_in_window(symbol, side, price, center_ts_ms, window_ms)
        return total > 0.0 and total >= min_qty

    # ---------------------------------------------------------------------- gc
    def gc(self, now_ms: int) -> None:
        """Drop fills older than retention; remove empty keys."""
        cutoff = now_ms - self.retention_ms
        empty: list[tuple[str, str, float]] = []
        for key, d in self._fills.items():
            while d and d[0].ts_ms < cutoff:
                d.popleft()
            if not d:
                empty.append(key)
        for k in empty:
            self._fills.pop(k, None)

    def __len__(self) -> int:
        return sum(len(d) for d in self._fills.values())
