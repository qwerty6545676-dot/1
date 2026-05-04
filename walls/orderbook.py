"""Local L2 order-book maintainer for a single Binance spot symbol.

Implements the official "How to manage a local order book" algorithm:
https://binance-docs.github.io/apidocs/spot/en/#how-to-manage-a-local-order-book-correctly

We keep bid/ask levels as plain dicts {price: qty}. With ~5000 levels per side
and a 1Hz scan cadence, sorting on demand is well within the latency budget and
keeps the dependency surface minimal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

# Signature: (side, price, old_qty, new_qty, ts_ms) -> None
LevelChangeCallback = Callable[[str, float, float, float, int], None]


@dataclass
class OrderBook:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int = 0
    last_event_ts_ms: int = 0
    synced: bool = False
    # Recent mid-price samples for executed/cancelled classification AND for
    # the dashboard sparkline: list of (ts_ms, mid_price). Retention bounded
    # by ``MID_HISTORY_RETENTION_MS``.
    mid_history: list[tuple[int, float]] = field(default_factory=list)
    # Optional callback fired for every level change in apply_diff. Used by
    # the iceberg detector. Set to None for stand-alone use (default).
    on_level_change: LevelChangeCallback | None = None

    # How long to keep mid samples. Long enough for both crossed-detection
    # (60s) and the dashboard sparkline (~5min).
    MID_HISTORY_RETENTION_MS: ClassVar[int] = 300_000

    # ------------------------------------------------------------------ helpers
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    # ---------------------------------------------------------------- snapshot
    def apply_snapshot(self, snapshot: dict) -> None:
        self.bids.clear()
        self.asks.clear()
        for price_s, qty_s in snapshot.get("bids", []):
            qty = float(qty_s)
            if qty > 0:
                self.bids[float(price_s)] = qty
        for price_s, qty_s in snapshot.get("asks", []):
            qty = float(qty_s)
            if qty > 0:
                self.asks[float(price_s)] = qty
        self.last_update_id = int(snapshot["lastUpdateId"])
        self.synced = True

    # ----------------------------------------------------------------- updates
    def apply_diff(self, evt: dict) -> bool:
        """Apply a single depth-update event.

        Returns True if applied, False if event is stale / out-of-order and
        the caller should re-snapshot.
        """
        first_u = int(evt["U"])
        last_u = int(evt["u"])

        # Drop events older than the snapshot.
        if last_u <= self.last_update_id:
            return True  # stale but not an error

        if not self.synced:
            return False

        # Continuity check (after first apply): each event must start no later
        # than prev.u + 1. A gap means we missed messages and need to resync.
        if self.last_update_id != 0 and first_u > self.last_update_id + 1:
            return False

        ts_ms = int(evt.get("E", 0)) or self.last_event_ts_ms
        cb = self.on_level_change

        for price_s, qty_s in evt.get("b", []):
            price = float(price_s)
            qty = float(qty_s)
            old_qty = self.bids.get(price, 0.0)
            if qty == 0.0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
            if cb is not None and ts_ms > 0:
                cb("bid", price, old_qty, qty, ts_ms)
        for price_s, qty_s in evt.get("a", []):
            price = float(price_s)
            qty = float(qty_s)
            old_qty = self.asks.get(price, 0.0)
            if qty == 0.0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
            if cb is not None and ts_ms > 0:
                cb("ask", price, old_qty, qty, ts_ms)

        self.last_update_id = last_u
        self.last_event_ts_ms = ts_ms
        return True

    def record_mid(self, ts_ms: int) -> None:
        m = self.mid()
        if m is None:
            return
        self.mid_history.append((ts_ms, m))
        cutoff = ts_ms - self.MID_HISTORY_RETENTION_MS
        while self.mid_history and self.mid_history[0][0] < cutoff:
            self.mid_history.pop(0)

    def crossed(self, price: float, side: str, since_ms: int) -> bool:
        """Did mid-price cross ``price`` from the book side since ``since_ms``?

        For a bid-side wall (support), "crossed" means mid dropped *below*
        the wall price. For an ask-side wall (resistance), mid rose *above*.
        """
        if not self.mid_history:
            return False
        for ts, mid in self.mid_history:
            if ts < since_ms:
                continue
            if side == "bid" and mid < price:
                return True
            if side == "ask" and mid > price:
                return True
        return False


class FirstEventGate:
    """Tracks whether the first valid post-snapshot event has been applied."""

    __slots__ = ("armed", "snapshot_id")

    def __init__(self) -> None:
        self.snapshot_id: int = 0
        self.armed: bool = False

    def reset(self, snapshot_id: int) -> None:
        self.snapshot_id = snapshot_id
        self.armed = True

    def is_first_valid(self, evt: dict) -> bool:
        """First valid post-snapshot event satisfies U <= snapshot_id+1 <= u."""
        if not self.armed:
            return False
        first_u = int(evt["U"])
        last_u = int(evt["u"])
        return first_u <= self.snapshot_id + 1 <= last_u
