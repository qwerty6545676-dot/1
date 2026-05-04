"""Iceberg-order detector.

An *iceberg* (or refill) order keeps replenishing the same price level after
each fill. From the point of view of someone watching the book, the level
shows a relatively small visible quantity — but every time it gets eaten,
a fresh chunk of similar size appears almost immediately.

Algorithm (per ``(symbol, side, price)`` level)::

    each level update {old_qty, new_qty}:
      if previously nominal-sized and new_qty <= eat_threshold * nominal:
          record an "eat" — remember the nominal qty as eaten_qty
      elif waiting for regen and new_qty is close to eaten_qty
           within `regen_window_sec`:
          record a "regen"
          if regen_count >= min_regens:
              emit IcebergEvent
              (cooldown + reset)

Walls are characterised by absolute USD value; icebergs are characterised by
*total flow through* a level — even a small visible size can be a multi-million
USD iceberg if it refills a hundred times.

The detector is plugged into :class:`walls.orderbook.OrderBook` via the
``on_level_change`` callback so it sees every update (not just 1 Hz scan
samples). All bookkeeping is in-memory; old entries are garbage-collected.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .settings import IcebergCfg


@dataclass(frozen=True)
class IcebergEvent:
    """Detection of an iceberg/refill order.

    ``visible_qty`` is the typical size that was visible at the level (the
    average of the regenerated chunks). ``cumulative_qty`` is the sum of all
    chunks the iceberg has shown — the lower bound of what the owner is
    actually trying to trade.
    """

    symbol: str
    side: str
    price: float
    visible_qty: float
    visible_usd: float
    cumulative_qty: float
    cumulative_usd: float
    regen_count: int
    first_seen_ts_ms: int
    last_seen_ts_ms: int

    @property
    def fingerprint(self) -> str:
        return f"{self.symbol}|{self.side}|{self.price:g}|iceberg"


@dataclass
class _LevelHist:
    """Per-(side, price) tracking state for the iceberg detector."""

    nominal_qty: float = 0.0  # last "full" qty observed at this level
    nominal_usd: float = 0.0
    eaten_at_ms: int = 0      # ts when level was eaten; 0 = not currently eaten
    eaten_qty: float = 0.0    # qty that was sitting before the eat
    regen_count: int = 0
    cumulative_qty: float = 0.0
    cumulative_usd: float = 0.0
    first_eat_ts_ms: int = 0
    last_event_ts_ms: int = 0
    # How many regens happened recently (bounded by lookback_sec)
    regen_history: deque[int] = field(default_factory=deque)


class IcebergDetector:
    """Tracks level updates and emits iceberg events.

    Single-threaded by design — it lives inside the scanner's asyncio loop.
    """

    def __init__(self, cfg: IcebergCfg) -> None:
        self.cfg = cfg
        # symbol -> (side, price) -> hist
        self._levels: dict[str, dict[tuple[str, float], _LevelHist]] = {}
        # fingerprint -> last_emit_ts_ms (cooldown)
        self._emitted: dict[str, int] = {}

    # ------------------------------------------------------------------- core
    def observe_change(
        self,
        symbol: str,
        side: str,
        price: float,
        old_qty: float,
        new_qty: float,
        ts_ms: int,
        mid_price: float | None = None,
    ) -> IcebergEvent | None:
        """Process one level change. Returns an IcebergEvent if just detected."""
        if not self.cfg.enabled:
            return None
        if price <= 0 or ts_ms <= 0:
            return None
        # Filter out levels that are too tiny to be interesting (in USD).
        old_usd = old_qty * price
        new_usd = new_qty * price
        if max(old_usd, new_usd) < self.cfg.min_visible_usd:
            return None
        # Optional: filter levels far from mid.
        if mid_price is not None and self.cfg.max_distance_pct > 0:
            dist = abs(price - mid_price) / mid_price * 100.0
            if dist > self.cfg.max_distance_pct:
                return None

        bucket = self._levels.setdefault(symbol, {})
        key = (side, price)
        hist = bucket.get(key)
        if hist is None:
            hist = _LevelHist(nominal_qty=old_qty, nominal_usd=old_usd)
            bucket[key] = hist

        emitted: IcebergEvent | None = None

        # --- Eat detection: was a real level, now it's mostly gone -----------
        eat_threshold_qty = hist.nominal_qty * self.cfg.eat_threshold_ratio
        if (
            hist.eaten_at_ms == 0
            and hist.nominal_qty > 0
            and old_qty > eat_threshold_qty
            and new_qty <= eat_threshold_qty
        ):
            hist.eaten_at_ms = ts_ms
            hist.eaten_qty = hist.nominal_qty
            if hist.first_eat_ts_ms == 0:
                hist.first_eat_ts_ms = ts_ms

        # --- Regen detection: shortly after an eat, qty restores -------------
        elif hist.eaten_at_ms != 0 and new_qty > 0:
            elapsed = ts_ms - hist.eaten_at_ms
            if elapsed <= self.cfg.regen_window_sec * 1000:
                lo = hist.eaten_qty * self.cfg.regen_match_lo
                hi = hist.eaten_qty * self.cfg.regen_match_hi
                if lo <= new_qty <= hi:
                    hist.regen_count += 1
                    hist.cumulative_qty += hist.eaten_qty
                    hist.cumulative_usd += hist.eaten_qty * price
                    hist.last_event_ts_ms = ts_ms
                    hist.regen_history.append(ts_ms)
                    self._gc_regen_history(hist, ts_ms)
                    # Reset eat state to be ready for the next cycle.
                    hist.eaten_at_ms = 0
                    hist.eaten_qty = 0.0
                    # Update nominal — track the iceberg's apparent size.
                    hist.nominal_qty = new_qty
                    hist.nominal_usd = new_qty * price
                    if (
                        len(hist.regen_history) >= self.cfg.min_regens
                        and self._cooldown_ok(symbol, side, price, ts_ms)
                    ):
                        avg_qty = hist.cumulative_qty / max(hist.regen_count, 1)
                        emitted = IcebergEvent(
                            symbol=symbol,
                            side=side,
                            price=price,
                            visible_qty=avg_qty,
                            visible_usd=avg_qty * price,
                            cumulative_qty=hist.cumulative_qty,
                            cumulative_usd=hist.cumulative_usd,
                            regen_count=hist.regen_count,
                            first_seen_ts_ms=hist.first_eat_ts_ms,
                            last_seen_ts_ms=ts_ms,
                        )
                        self._emitted[emitted.fingerprint] = ts_ms
                else:
                    # Restored to a *different* size — not an iceberg pattern,
                    # treat the level as a fresh one going forward.
                    hist.eaten_at_ms = 0
                    hist.eaten_qty = 0.0
                    hist.nominal_qty = new_qty
                    hist.nominal_usd = new_usd
            else:
                # Window expired: reset eat state, this was just a normal fill.
                hist.eaten_at_ms = 0
                hist.eaten_qty = 0.0
                hist.nominal_qty = new_qty
                hist.nominal_usd = new_usd

        # --- Generic baseline update -----------------------------------------
        elif new_qty >= hist.nominal_qty * 0.8:
            # Level grew or stayed roughly the same — update nominal.
            hist.nominal_qty = new_qty
            hist.nominal_usd = new_usd

        return emitted

    # ---------------------------------------------------------------- helpers
    def _cooldown_ok(self, symbol: str, side: str, price: float, ts_ms: int) -> bool:
        fp = f"{symbol}|{side}|{price:g}|iceberg"
        prev = self._emitted.get(fp)
        if prev is None:
            return True
        return (ts_ms - prev) >= self.cfg.cooldown_ttl_sec * 1000

    def _gc_regen_history(self, hist: _LevelHist, now_ms: int) -> None:
        cutoff = now_ms - int(self.cfg.lookback_sec * 1000)
        while hist.regen_history and hist.regen_history[0] < cutoff:
            hist.regen_history.popleft()

    # -------------------------------------------------------------------- gc
    def gc(self, now_ms: int) -> None:
        """Drop level-history entries that haven't seen activity recently."""
        cutoff = now_ms - int(self.cfg.lookback_sec * 1000) * 2
        empty_symbols: list[str] = []
        for symbol, bucket in self._levels.items():
            stale: list[tuple[str, float]] = []
            for key, hist in bucket.items():
                last_ts = max(
                    hist.last_event_ts_ms,
                    hist.eaten_at_ms,
                    hist.first_eat_ts_ms,
                )
                if last_ts and last_ts < cutoff:
                    stale.append(key)
                elif (
                    last_ts == 0
                    and hist.nominal_qty == 0.0
                ):
                    stale.append(key)
            for k in stale:
                bucket.pop(k, None)
            if not bucket:
                empty_symbols.append(symbol)
        for s in empty_symbols:
            self._levels.pop(s, None)
        # Cooldown garbage collection
        cd_cutoff = now_ms - int(self.cfg.cooldown_ttl_sec * 1000)
        for fp in [k for k, ts in self._emitted.items() if ts < cd_cutoff]:
            self._emitted.pop(fp, None)
