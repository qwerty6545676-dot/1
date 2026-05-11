"""Tests for the iceberg/refill detector."""

from __future__ import annotations

import pytest

from walls.iceberg import IcebergDetector
from walls.settings import IcebergCfg
from walls.trade_buffer import TradeBuffer


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
        # Anti-spoof gate is opt-in for the v1 tests below; v2 tests turn it on.
        require_trade_confirmation=False,
        trade_window_ms=2000,
        trade_min_qty_ratio=0.30,
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


# ----------------------------------------------------------- v2: anti-spoofing
def test_anti_spoof_rejects_regen_without_trade() -> None:
    """When require_trade_confirmation=True, regens without matching fills
    are rejected as spoofs and must NOT increment the regen counter."""
    cfg = _cfg(min_regens=2, require_trade_confirmation=True, trade_window_ms=2000)
    buf = TradeBuffer(retention_ms=10_000)  # empty buffer = no trades seen
    det = IcebergDetector(cfg, trade_buffer=buf)
    sym, side, price, qty = "BTCUSDT", "bid", 80000.0, 1.0

    # 5 eat→regen cycles without any matching trades.
    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    for i in range(5):
        det.observe_change(sym, side, price, qty, 0.05, 2_000 + i * 1000)
        ev = det.observe_change(sym, side, price, 0.05, qty, 2_500 + i * 1000)
        assert ev is None
    assert det.confirmed_count == 0
    assert det.rejected_spoof_count == 5


def test_anti_spoof_accepts_regen_with_trade() -> None:
    """A regen that follows a real trade at the level must be counted."""
    cfg = _cfg(min_regens=2, require_trade_confirmation=True, trade_window_ms=2000,
               trade_min_qty_ratio=0.30)
    buf = TradeBuffer(retention_ms=10_000)
    det = IcebergDetector(cfg, trade_buffer=buf)
    sym, side, price, qty = "BTCUSDT", "bid", 80000.0, 1.0

    # Baseline
    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    # Cycle 1: eat at 2000 ms, real trade at 2050 ms.
    det.observe_change(sym, side, price, qty, 0.05, 2_000)
    buf.record(sym, price, qty * 0.5, 2_050, buyer_is_maker=True)  # bid hit
    ev1 = det.observe_change(sym, side, price, 0.05, qty, 2_500)
    assert ev1 is None

    # Cycle 2: eat at 3000 ms, real trade at 3100 ms.
    det.observe_change(sym, side, price, qty, 0.05, 3_000)
    buf.record(sym, price, qty * 0.5, 3_100, buyer_is_maker=True)
    ev2 = det.observe_change(sym, side, price, 0.05, qty, 3_500)

    assert ev2 is not None
    assert ev2.regen_count == 2
    assert det.confirmed_count == 2
    assert det.rejected_spoof_count == 0


def test_anti_spoof_rejects_when_trade_qty_too_small() -> None:
    """A trade smaller than ``trade_min_qty_ratio * eaten_qty`` must NOT confirm."""
    cfg = _cfg(min_regens=2, require_trade_confirmation=True, trade_window_ms=2000,
               trade_min_qty_ratio=0.50)
    buf = TradeBuffer(retention_ms=10_000)
    det = IcebergDetector(cfg, trade_buffer=buf)
    sym, side, price, qty = "ETHUSDT", "ask", 2500.0, 10.0

    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    # Eat at 2000ms; trade only 10% (need 50%).
    det.observe_change(sym, side, price, qty, 0.5, 2_000)
    buf.record(sym, price, qty * 0.10, 2_100, buyer_is_maker=False)  # ask hit
    ev = det.observe_change(sym, side, price, 0.5, qty, 2_500)
    assert ev is None
    assert det.rejected_spoof_count == 1
    assert det.confirmed_count == 0


def test_anti_spoof_rejects_when_trade_outside_window() -> None:
    """A trade outside ``trade_window_ms`` of the eat must NOT confirm."""
    cfg = _cfg(min_regens=2, require_trade_confirmation=True, trade_window_ms=200)
    buf = TradeBuffer(retention_ms=10_000)
    det = IcebergDetector(cfg, trade_buffer=buf)
    sym, side, price, qty = "BTCUSDT", "bid", 80000.0, 1.0

    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    det.observe_change(sym, side, price, qty, 0.05, 2_000)
    # Trade is 500 ms after eat — outside the 200 ms window.
    buf.record(sym, price, qty, 2_500, buyer_is_maker=True)
    ev = det.observe_change(sym, side, price, 0.05, qty, 2_600)
    assert ev is None
    assert det.rejected_spoof_count == 1


def test_anti_spoof_disabled_by_flag() -> None:
    """``require_trade_confirmation=False`` keeps v1 behaviour even with empty buffer."""
    cfg = _cfg(min_regens=2, require_trade_confirmation=False)
    buf = TradeBuffer(retention_ms=10_000)
    det = IcebergDetector(cfg, trade_buffer=buf)
    sym, side, price, qty = "BTCUSDT", "bid", 80000.0, 1.0

    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    det.observe_change(sym, side, price, qty, 0.05, 2_000)
    ev1 = det.observe_change(sym, side, price, 0.05, qty, 2_500)
    assert ev1 is None
    det.observe_change(sym, side, price, qty, 0.05, 3_000)
    ev2 = det.observe_change(sym, side, price, 0.05, qty, 3_500)
    assert ev2 is not None  # fired, as in v1
    assert det.rejected_spoof_count == 0


def test_anti_spoof_no_buffer_degrades_gracefully() -> None:
    """Anti-spoof on but no buffer attached → treat as confirmed (degraded mode)."""
    cfg = _cfg(min_regens=2, require_trade_confirmation=True)
    det = IcebergDetector(cfg, trade_buffer=None)
    sym, side, price, qty = "BTCUSDT", "bid", 80000.0, 1.0

    det.observe_change(sym, side, price, 0.0, qty, 1_000)
    det.observe_change(sym, side, price, qty, 0.05, 2_000)
    ev1 = det.observe_change(sym, side, price, 0.05, qty, 2_500)
    assert ev1 is None
    det.observe_change(sym, side, price, qty, 0.05, 3_000)
    ev2 = det.observe_change(sym, side, price, 0.05, qty, 3_500)
    assert ev2 is not None  # would have fired in v1


def test_anti_spoof_uses_correct_side() -> None:
    """Ask-side fills (buyer_is_maker=False) must NOT confirm a bid-side eat."""
    cfg = _cfg(min_regens=2, require_trade_confirmation=True, trade_window_ms=2000,
               trade_min_qty_ratio=0.30)
    buf = TradeBuffer(retention_ms=10_000)
    det = IcebergDetector(cfg, trade_buffer=buf)
    sym, price, qty = "BTCUSDT", 80000.0, 1.0

    det.observe_change(sym, "bid", price, 0.0, qty, 1_000)
    det.observe_change(sym, "bid", price, qty, 0.05, 2_000)
    # Trade hits the ASK at this price; the BID eat must not confirm.
    buf.record(sym, price, qty, 2_050, buyer_is_maker=False)
    ev = det.observe_change(sym, "bid", price, 0.05, qty, 2_500)
    assert ev is None
    assert det.rejected_spoof_count == 1
