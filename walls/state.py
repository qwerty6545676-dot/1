"""Wall state machine.

The lifecycle of a single tracked wall:

    PENDING ──── min_lifetime_sec ────▶ ACTIVE
       │                                  │
       │                                  ├─── disappears + price crossed ──▶ EXECUTED
       │                                  └─── disappears + no cross ────────▶ CANCELLED
       │
       └── disappears before active ──▶ silently dropped (was just a flicker)

Events emitted by :meth:`StateMachine.tick` are:

* ``appeared`` — a candidate just transitioned PENDING → ACTIVE.
* ``executed`` — an ACTIVE wall vanished and the price crossed it.
* ``cancelled`` — an ACTIVE wall vanished without the price crossing.

During the ``cold_start_grace_sec`` window after startup, no ``appeared`` event
is emitted. Walls that survive past the grace window are silently promoted to
ACTIVE without an alert (they were already there before we started watching).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from .detector import Candidate
from .orderbook import OrderBook
from .settings import DetectorCfg


class WallState(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"


@dataclass
class TrackedWall:
    fingerprint: str
    symbol: str
    side: str
    price: float
    qty: float
    usd_value: float
    state: WallState
    first_seen_ts_ms: int
    last_seen_ts_ms: int
    distance_pct: float
    mid_price: float


@dataclass(frozen=True)
class StateEvent:
    kind: str  # "appeared" | "executed" | "cancelled"
    wall: TrackedWall


@dataclass
class StateMachine:
    cfg: DetectorCfg
    started_at_ms: int
    # fingerprint -> tracked wall
    tracked: dict[str, TrackedWall] = field(default_factory=dict)

    # ----------------------------------------------------------- fingerprints
    @staticmethod
    def fingerprint(symbol: str, side: str, price: float, usd_value: float) -> str:
        # Logarithmic bucketing so the bucket width is a constant *percentage*
        # of price/size regardless of asset (BTC vs SHIB).
        #   log(p) * 2000 ≈ bucket of width 0.05% in price space
        #   log(usd) * 20  ≈ bucket of width 5%   in size space
        price_bucket = math.floor(math.log(price) * 2000) if price > 0 else 0
        size_bucket = math.floor(math.log(usd_value) * 20) if usd_value > 0 else 0
        return f"{symbol}|{side}|{price_bucket}|{size_bucket}"

    # --------------------------------------------------------------- updates
    def observe(self, candidates: list[Candidate], now_ms: int) -> None:
        """Mark all current candidates as seen at ``now_ms``."""
        for c in candidates:
            fp = self.fingerprint(c.symbol, c.side, c.price, c.usd_value)
            existing = self.tracked.get(fp)
            if existing is None:
                self.tracked[fp] = TrackedWall(
                    fingerprint=fp,
                    symbol=c.symbol,
                    side=c.side,
                    price=c.price,
                    qty=c.qty,
                    usd_value=c.usd_value,
                    state=WallState.PENDING,
                    first_seen_ts_ms=now_ms,
                    last_seen_ts_ms=now_ms,
                    distance_pct=c.distance_pct,
                    mid_price=c.mid_price,
                )
            else:
                existing.last_seen_ts_ms = now_ms
                existing.qty = c.qty
                existing.usd_value = c.usd_value
                existing.distance_pct = c.distance_pct
                existing.mid_price = c.mid_price

    def tick(self, books: dict[str, OrderBook], now_ms: int) -> list[StateEvent]:
        """Run state transitions and return any events to emit.

        Should be called periodically (e.g. once per second), AFTER all
        symbols' candidate lists have been pushed via :meth:`observe`.
        """
        events: list[StateEvent] = []
        grace_until_ms = self.started_at_ms + int(self.cfg.cold_start_grace_sec * 1000)

        # Walls that haven't been seen for a few seconds are considered gone.
        gone_threshold_ms = 5_000

        to_drop: list[str] = []
        for fp, w in self.tracked.items():
            stale = (now_ms - w.last_seen_ts_ms) > gone_threshold_ms
            if stale:
                if w.state == WallState.ACTIVE:
                    book = books.get(w.symbol)
                    crossed = bool(book) and book.crossed(
                        price=w.price,
                        side=w.side,
                        since_ms=now_ms - int(self.cfg.execution_window_sec * 1000),
                    )
                    events.append(
                        StateEvent(kind="executed" if crossed else "cancelled", wall=w)
                    )
                # Whether or not it was ACTIVE, drop the entry.
                to_drop.append(fp)
                continue

            # Promote PENDING → ACTIVE when it has lived long enough.
            if w.state == WallState.PENDING:
                age_ms = now_ms - w.first_seen_ts_ms
                if age_ms >= self.cfg.min_lifetime_sec * 1000.0:
                    w.state = WallState.ACTIVE
                    # Walls first observed during the grace window were almost
                    # certainly already in the book before we started — silently
                    # activate without firing an alert.
                    if w.first_seen_ts_ms >= grace_until_ms:
                        events.append(StateEvent(kind="appeared", wall=w))

        for fp in to_drop:
            self.tracked.pop(fp, None)

        return events
