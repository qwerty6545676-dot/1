"""Tests for the trade buffer (anti-spoof support)."""

from __future__ import annotations

from walls.trade_buffer import TradeBuffer


def test_records_and_finds_fill() -> None:
    buf = TradeBuffer(retention_ms=10_000)
    buf.record("BTCUSDT", 80_000.0, 0.5, 1_000, buyer_is_maker=True)
    # bid was filled because buyer is maker
    assert buf.has_fill("BTCUSDT", "bid", 80_000.0, center_ts_ms=1_000, window_ms=200)
    # Different price → no fill.
    assert not buf.has_fill("BTCUSDT", "bid", 79_000.0, center_ts_ms=1_000, window_ms=200)
    # Different side → no fill.
    assert not buf.has_fill("BTCUSDT", "ask", 80_000.0, center_ts_ms=1_000, window_ms=200)


def test_window_bounds_inclusive() -> None:
    buf = TradeBuffer(retention_ms=10_000)
    buf.record("ETHUSDT", 2_500.0, 1.0, 5_000, buyer_is_maker=False)
    # ±200ms around 5_000 covers [4_800, 5_200]
    assert buf.has_fill("ETHUSDT", "ask", 2_500.0, center_ts_ms=5_100, window_ms=200)
    assert buf.has_fill("ETHUSDT", "ask", 2_500.0, center_ts_ms=4_900, window_ms=200)
    # outside window
    assert not buf.has_fill("ETHUSDT", "ask", 2_500.0, center_ts_ms=5_300, window_ms=200)


def test_total_qty_aggregates() -> None:
    buf = TradeBuffer(retention_ms=10_000)
    buf.record("BTCUSDT", 80_000.0, 0.1, 1_000, buyer_is_maker=True)
    buf.record("BTCUSDT", 80_000.0, 0.2, 1_050, buyer_is_maker=True)
    buf.record("BTCUSDT", 80_000.0, 0.3, 1_100, buyer_is_maker=True)
    total = buf.total_qty_in_window(
        "BTCUSDT", "bid", 80_000.0, center_ts_ms=1_050, window_ms=100,
    )
    assert abs(total - 0.6) < 1e-9


def test_min_qty_threshold() -> None:
    buf = TradeBuffer(retention_ms=10_000)
    buf.record("BTCUSDT", 80_000.0, 0.5, 1_000, buyer_is_maker=True)
    assert buf.has_fill("BTCUSDT", "bid", 80_000.0, 1_000, 200, min_qty=0.1)
    assert not buf.has_fill("BTCUSDT", "bid", 80_000.0, 1_000, 200, min_qty=1.0)


def test_min_qty_threshold_inclusive_at_boundary() -> None:
    """A trade whose qty exactly equals the threshold must count as a fill."""
    buf = TradeBuffer(retention_ms=10_000)
    buf.record("BTCUSDT", 80_000.0, 0.30, 1_000, buyer_is_maker=True)
    # Boundary case: total == min_qty.
    assert buf.has_fill("BTCUSDT", "bid", 80_000.0, 1_000, 200, min_qty=0.30)


def test_empty_buffer_returns_false_regardless_of_min_qty() -> None:
    """No trades in the window → False even when min_qty is 0."""
    buf = TradeBuffer(retention_ms=10_000)
    assert not buf.has_fill("BTCUSDT", "bid", 80_000.0, 1_000, 200, min_qty=0.0)
    assert not buf.has_fill("BTCUSDT", "bid", 80_000.0, 1_000, 200, min_qty=1.0)


def test_old_records_are_evicted_on_record() -> None:
    """retention_ms is enforced on every record() call to bound memory."""
    buf = TradeBuffer(retention_ms=1_000)
    # First record at ts=1000
    buf.record("BTCUSDT", 80_000.0, 0.1, 1_000, buyer_is_maker=True)
    # Second record at ts=10_000 → first should be GC'd (older than 1s).
    buf.record("BTCUSDT", 80_000.0, 0.2, 10_000, buyer_is_maker=True)
    total_old = buf.total_qty_in_window(
        "BTCUSDT", "bid", 80_000.0, center_ts_ms=1_000, window_ms=200,
    )
    # The 1000ms record was evicted, so window around 1_000 has nothing.
    assert total_old == 0.0
    # But the new record is still there.
    total_new = buf.total_qty_in_window(
        "BTCUSDT", "bid", 80_000.0, center_ts_ms=10_000, window_ms=200,
    )
    assert abs(total_new - 0.2) < 1e-9


def test_explicit_gc_drops_empty_keys() -> None:
    buf = TradeBuffer(retention_ms=1_000)
    buf.record("BTCUSDT", 80_000.0, 0.1, 1_000, buyer_is_maker=True)
    buf.record("ETHUSDT", 2_500.0, 1.0, 1_000, buyer_is_maker=False)
    assert len(buf) == 2
    buf.gc(now_ms=10_000)  # all stale
    assert len(buf) == 0


def test_buyer_maker_flag_maps_to_side() -> None:
    """Verify the bid/ask mapping is what the iceberg detector expects."""
    buf = TradeBuffer(retention_ms=10_000)
    # buyer_is_maker=True → trade hit a bid (seller initiated).
    buf.record("BTCUSDT", 80_000.0, 1.0, 1_000, buyer_is_maker=True)
    # buyer_is_maker=False → trade hit an ask (buyer initiated).
    buf.record("BTCUSDT", 80_000.0, 1.0, 1_000, buyer_is_maker=False)
    assert buf.has_fill("BTCUSDT", "bid", 80_000.0, 1_000, 100)
    assert buf.has_fill("BTCUSDT", "ask", 80_000.0, 1_000, 100)


def test_invalid_inputs_ignored() -> None:
    buf = TradeBuffer(retention_ms=10_000)
    buf.record("BTCUSDT", 0.0, 1.0, 1_000, buyer_is_maker=True)   # bad price
    buf.record("BTCUSDT", 80_000.0, 0.0, 1_000, buyer_is_maker=True)  # bad qty
    buf.record("BTCUSDT", 80_000.0, 1.0, 0, buyer_is_maker=True)  # bad ts
    assert len(buf) == 0


def test_retention_clamped_to_min() -> None:
    """Retention always ≥ 1 second to avoid pathological tiny windows."""
    buf = TradeBuffer(retention_ms=10)  # too small
    assert buf.retention_ms >= 1_000
