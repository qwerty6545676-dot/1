"""Tests for the iceberg/refill detector."""

from __future__ import annotations

import pytest

from walls.iceberg import IcebergDetector
from walls.settings import IcebergCfg


def _cfg(**overrides: object) -> IcebergCfg:
    base = dict(
        enabled=True,
        min_visible_usd=10_000.0,
        max_distance_pct=0.0,  # 0 disables distance filter
        eat_threshold_ratio=0.30,
        regen_window_sec=5.0,
        regen_match_lo=0.7,
        regen_match_hi=1.4,
        min_regens=3,
        lookback_sec=60.0,
        cooldown_ttl_sec=300.0,
    )
    base.update(overrides)
    return IcebergCfg(**base)  # type: ignore[arg-type]


def _eat_then_regen(
    det: IcebergDetector,
    symbol: str,
    side: str,
    price: float,
    qty: float,
    t0_ms: int,
) -> object:
    """Simulate one eat→regen cycle. Returns event-or-None from the regen step."""
    # Establish baseline by applying an "increase" so nominal_qty is set.
    det.observe_change(symbol, side, price, 0.0, qty, t0_ms - 1000)
    # Eat: drop to 5% of qty.
    det.observe_change(symbol, side, price, qty, qty * 0.05, t0_ms)
    # Regen 100 ms later, same size.
    return det.observe_change(symbol, side, price, qty * 0.05, qty, t0_ms + 100)


def test_three_regens_emit_event() -> None:
    cfg = _cfg(min_regens=3)
    det = IcebergDetector(cfg)
    sym, side, price, qty = "BTCUSDT", "bid", 80000.0, 1.0  # $80k visible
    # 1st eat+regen
    det.observe_change(sym, side, price, 0.0, qty, 1_000)  # set baseline
    det.observe_change(sym, side, price, qty, 0.05, 2_000)
    ev1 = det.observe_change(sym, side, price, 0.05, qty, 2_500)
    assert ev1 is None
    # 2nd
    det.observe_change(sym, side, price, qty, 0.05, 3_000)
    ev2 = det.observe_change(sym, side, price, 0.05, qty, 3_500)
    assert ev2 is None
    # 3rd: should fire
    det.observe_change(sym, side, price, qty, 0.05, 4_000)
    ev3 = det.observe_change(sym, side, price, 0.05, qty, 4_500)
    assert ev3 is not None
    assert ev3.symbol == sym
    assert ev3.side == side
    assert ev3.regen_count == 3
    assert ev3.cumulative_qty == pytest.approx(qty * 3)
    assert ev3.cumulative_usd == pytest.approx(qty * 3 * price)


def test_below_min_regens_does_not_fire() -> None:
    cfg = _cfg(min_regens=5)
    det = IcebergDetector(cfg)
    sym, side, price, qty = "ETHUSDT", "ask", 2500.0, 10.0
    for _ in range(3):
        ev = _eat_then_regen(det, sym, side, price, qty, t0_ms=10_000)
        assert ev is None


def test_regen_outside_window_resets() -> None:
    cfg = _cfg(min_regens=2, regen_window_sec=5.0)
    det = IcebergDetector(cfg)
    sym, side, price, qty = "SOLUSDT", "bid", 200.0, 100.0
    # Establish baseline
    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    # Eat
    det.observe_change(sym, side, price, qty, 5.0, 2_000)
    # Regen *after* the window — should NOT count.
    ev = det.observe_change(sym, side, price, 5.0, qty, 2_000 + 6_000)
    assert ev is None
    # Now do a real cycle inside the window.
    det.observe_change(sym, side, price, qty, 5.0, 10_000)
    det.observe_change(sym, side, price, 5.0, qty, 10_500)
    det.observe_change(sym, side, price, qty, 5.0, 11_000)
    ev2 = det.observe_change(sym, side, price, 5.0, qty, 11_500)
    assert ev2 is not None
    # Only the in-window regens are counted.
    assert ev2.regen_count == 2


def test_regen_with_wrong_size_resets_baseline() -> None:
    """If new qty is way bigger/smaller than eaten, treat as fresh order, no regen."""
    cfg = _cfg(min_regens=2, regen_match_lo=0.7, regen_match_hi=1.4)
    det = IcebergDetector(cfg)
    sym, side, price, qty = "BNBUSDT", "ask", 600.0, 50.0
    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    det.observe_change(sym, side, price, qty, 1.0, 2_000)
    # Re-add with 3× the size — not an iceberg.
    ev = det.observe_change(sym, side, price, 1.0, qty * 3, 2_500)
    assert ev is None


def test_disabled_returns_none() -> None:
    cfg = _cfg(enabled=False)
    det = IcebergDetector(cfg)
    ev = _eat_then_regen(det, "BTCUSDT", "bid", 80000.0, 1.0, 1_000)
    assert ev is None


def test_min_visible_usd_filter() -> None:
    """Levels with USD value below the threshold are ignored."""
    cfg = _cfg(min_visible_usd=100_000.0, min_regens=2)
    det = IcebergDetector(cfg)
    sym, side, price, qty = "DOGEUSDT", "bid", 0.10, 100.0  # $10 — way below threshold
    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    det.observe_change(sym, side, price, qty, 5.0, 2_000)
    ev = det.observe_change(sym, side, price, 5.0, qty, 2_500)
    assert ev is None  # tiny levels not tracked


def test_cooldown_blocks_repeat() -> None:
    cfg = _cfg(min_regens=2, cooldown_ttl_sec=300.0)
    det = IcebergDetector(cfg)
    sym, side, price, qty = "BTCUSDT", "bid", 80000.0, 1.0
    # First detection
    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    det.observe_change(sym, side, price, qty, 0.05, 2_000)
    det.observe_change(sym, side, price, 0.05, qty, 2_500)
    det.observe_change(sym, side, price, qty, 0.05, 3_000)
    ev1 = det.observe_change(sym, side, price, 0.05, qty, 3_500)
    assert ev1 is not None
    # Try to fire again 10 seconds later — should be cooled down.
    det.observe_change(sym, side, price, qty, 0.05, 4_000)
    det.observe_change(sym, side, price, 0.05, qty, 4_500)
    det.observe_change(sym, side, price, qty, 0.05, 5_000)
    ev2 = det.observe_change(sym, side, price, 0.05, qty, 5_500)
    assert ev2 is None


def test_distance_filter_excludes_far_levels() -> None:
    """Levels too far from mid are skipped."""
    cfg = _cfg(min_regens=1, max_distance_pct=1.0)
    det = IcebergDetector(cfg)
    sym, side, price, qty = "BTCUSDT", "bid", 70_000.0, 1.0
    # mid is 80_000 → distance is 12.5%, exceeds 1.0%
    det.observe_change(sym, side, price, 0.0, qty, 1_000, mid_price=80_000.0)
    det.observe_change(sym, side, price, qty, 0.05, 2_000, mid_price=80_000.0)
    ev = det.observe_change(sym, side, price, 0.05, qty, 2_500, mid_price=80_000.0)
    assert ev is None


def test_gc_drops_stale_levels() -> None:
    cfg = _cfg(min_regens=2, lookback_sec=60.0)
    det = IcebergDetector(cfg)
    sym, side, price, qty = "BTCUSDT", "bid", 80000.0, 1.0
    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    det.observe_change(sym, side, price, qty, 0.05, 2_000)
    # Fast-forward 10 minutes.
    det.gc(2_000 + 10 * 60_000)
    # Symbol bucket should be empty.
    assert sym not in det._levels or not det._levels[sym]
