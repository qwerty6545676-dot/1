"""Tests for the local order-book maintainer."""

from __future__ import annotations

from walls.orderbook import FirstEventGate, OrderBook


def _snapshot(last_id: int) -> dict:
    return {
        "lastUpdateId": last_id,
        "bids": [["100.0", "1.0"], ["99.0", "5.0"], ["98.0", "10.0"]],
        "asks": [["101.0", "1.0"], ["102.0", "5.0"], ["103.0", "10.0"]],
    }


def _diff(U: int, u: int, *, b=None, a=None, E: int = 1) -> dict:
    return {
        "e": "depthUpdate",
        "E": E,
        "s": "BTCUSDT",
        "U": U,
        "u": u,
        "b": b or [],
        "a": a or [],
    }


def test_snapshot_loads_levels() -> None:
    book = OrderBook(symbol="BTCUSDT")
    book.apply_snapshot(_snapshot(1000))
    assert book.synced
    assert book.last_update_id == 1000
    assert book.best_bid() == 100.0
    assert book.best_ask() == 101.0
    assert book.mid() == 100.5


def test_diff_applies_when_strictly_after_snapshot() -> None:
    book = OrderBook(symbol="BTCUSDT")
    book.apply_snapshot(_snapshot(1000))
    # Diff strictly after snapshot — should apply.
    assert book.apply_diff(_diff(1001, 1010, b=[["99.5", "20.0"]])) is True
    assert book.bids[99.5] == 20.0
    assert book.last_update_id == 1010


def test_diff_with_zero_qty_removes_level() -> None:
    book = OrderBook(symbol="BTCUSDT")
    book.apply_snapshot(_snapshot(1000))
    assert book.apply_diff(_diff(1001, 1002, b=[["99.0", "0"]])) is True
    assert 99.0 not in book.bids


def test_stale_diff_is_dropped_silently() -> None:
    book = OrderBook(symbol="BTCUSDT")
    book.apply_snapshot(_snapshot(1000))
    # Stale (u <= last_update_id) — should be ignored, not failed.
    assert book.apply_diff(_diff(990, 999, b=[["95.0", "99.0"]])) is True
    assert 95.0 not in book.bids  # not applied
    assert book.last_update_id == 1000  # unchanged


def test_continuity_gap_returns_false() -> None:
    book = OrderBook(symbol="BTCUSDT")
    book.apply_snapshot(_snapshot(1000))
    assert book.apply_diff(_diff(1001, 1010)) is True
    # Next event should start at 1011, not 1015 — gap detected.
    assert book.apply_diff(_diff(1015, 1020)) is False


def test_first_event_gate_validates_straddle() -> None:
    gate = FirstEventGate()
    gate.reset(snapshot_id=1000)
    # First valid event must straddle 1001.
    assert gate.is_first_valid(_diff(995, 1005))
    assert gate.is_first_valid(_diff(1001, 1001))
    assert not gate.is_first_valid(_diff(1002, 1010))  # starts after 1001
    assert not gate.is_first_valid(_diff(900, 999))    # ends before 1001


def test_mid_history_records_and_evicts() -> None:
    book = OrderBook(symbol="BTCUSDT")
    book.apply_snapshot(_snapshot(1000))
    book.record_mid(1_000)
    book.record_mid(30_000)
    # Both within 60s window
    assert len(book.mid_history) == 2
    book.record_mid(100_000)
    # 1_000 should be evicted (older than 100_000 - 60_000 = 40_000)
    assert all(ts >= 40_000 for ts, _ in book.mid_history)


def test_crossed_detects_drop_for_bid_wall() -> None:
    book = OrderBook(symbol="BTCUSDT")
    book.apply_snapshot(_snapshot(1000))
    # Manually inject mid history.
    book.mid_history = [(1_000, 100.5), (2_000, 100.0), (3_000, 99.0)]
    # Bid wall at 99.5: at t=3_000 mid is 99.0 < 99.5, so crossed.
    assert book.crossed(price=99.5, side="bid", since_ms=0) is True
    # Same wall, but only look at history >= 1_500: still crossed (mid=99 at t=3000).
    assert book.crossed(price=99.5, side="bid", since_ms=1_500) is True
    # Wall at 95: never crossed.
    assert book.crossed(price=95.0, side="bid", since_ms=0) is False


def test_crossed_detects_rise_for_ask_wall() -> None:
    book = OrderBook(symbol="BTCUSDT")
    book.apply_snapshot(_snapshot(1000))
    book.mid_history = [(1_000, 100.5), (2_000, 102.0)]
    # Ask wall at 101.5: at t=2_000 mid=102.0 > 101.5, so crossed.
    assert book.crossed(price=101.5, side="ask", since_ms=0) is True
    assert book.crossed(price=105.0, side="ask", since_ms=0) is False
