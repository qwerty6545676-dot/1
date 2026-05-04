"""Tests for wall detection."""

from __future__ import annotations

from walls.detector import aggregate_zones, scan
from walls.orderbook import OrderBook
from walls.settings import DetectorCfg, SizeTier


def _cfg(**overrides) -> DetectorCfg:
    base = dict(
        size_tiers=(SizeTier(min_24h_volume_usd=0.0, min_wall_usd=10_000.0),),
        max_distance_pct=3.0,
        min_distance_pct=0.05,
        min_lifetime_sec=60.0,
        relative_size_multiplier=3.0,
        neighbour_levels=20,
        zone_aggregation_pct=0.10,
        cold_start_grace_sec=120.0,
        execution_window_sec=5.0,
    )
    base.update(overrides)
    return DetectorCfg(**base)


def _book_with_wall(
    *, mid: float, wall_side: str, wall_price: float, wall_qty: float
) -> OrderBook:
    book = OrderBook(symbol="BTCUSDT")
    book.synced = True
    # Build bid side: 50 levels descending from mid - 0.01 by 0.01 each, qty=1
    for i in range(1, 60):
        book.bids[mid - 0.01 * i] = 1.0
    # Ask side: 50 levels ascending
    for i in range(1, 60):
        book.asks[mid + 0.01 * i] = 1.0
    # Plant the wall
    if wall_side == "bid":
        book.bids[wall_price] = wall_qty
    else:
        book.asks[wall_price] = wall_qty
    return book


def test_scan_finds_obvious_wall() -> None:
    cfg = _cfg()
    book = _book_with_wall(mid=100.0, wall_side="bid", wall_price=99.0, wall_qty=10_000.0)
    cands = scan("BTCUSDT", book, cfg, min_wall_usd=10_000.0)
    assert any(c.side == "bid" and c.price == 99.0 for c in cands)


def test_scan_skips_walls_below_size_threshold() -> None:
    cfg = _cfg()
    # qty=50 → usd=99*50=4950 < 10_000 threshold
    book = _book_with_wall(mid=100.0, wall_side="bid", wall_price=99.0, wall_qty=50.0)
    cands = scan("BTCUSDT", book, cfg, min_wall_usd=10_000.0)
    assert all(c.price != 99.0 for c in cands)


def test_scan_skips_walls_too_far_from_mid() -> None:
    # Outside max_distance_pct=3% - wall at 90 is 10% from mid, should be skipped.
    book = OrderBook(symbol="BTCUSDT")
    book.synced = True
    book.bids[100.0] = 1.0
    book.asks[100.5] = 1.0
    for i in range(1, 30):
        book.bids[100.0 - 0.5 * i] = 1.0
    book.bids[90.0] = 10_000.0  # huge wall but far away
    cands = scan("BTCUSDT", book, _cfg(max_distance_pct=3.0), min_wall_usd=10_000.0)
    assert all(c.price != 90.0 for c in cands)


def test_scan_skips_walls_too_close_to_mid() -> None:
    cfg = _cfg(min_distance_pct=0.5)
    book = _book_with_wall(mid=100.0, wall_side="bid", wall_price=99.95, wall_qty=10_000.0)
    cands = scan("BTCUSDT", book, cfg, min_wall_usd=10_000.0)
    # 99.95 is 0.05% from mid 100.0, but min_distance is 0.5% — should be excluded.
    assert all(c.price != 99.95 for c in cands)


def test_scan_skips_walls_not_dwarfing_neighbours() -> None:
    cfg = _cfg(relative_size_multiplier=10.0)  # require 10× the neighbours
    book = OrderBook(symbol="BTCUSDT")
    book.synced = True
    # Many "fat" neighbours at qty=1000 each (USD ≈ 99k)
    for i in range(1, 60):
        book.bids[100.0 - 0.01 * i] = 1000.0
    book.asks[100.5] = 1.0
    # "Wall" only 3× the neighbours — not enough at multiplier=10
    book.bids[99.0] = 3000.0
    cands = scan("BTCUSDT", book, cfg, min_wall_usd=10_000.0)
    assert all(c.price != 99.0 for c in cands)


def test_aggregate_zones_merges_close_walls() -> None:
    cfg = _cfg()
    book = OrderBook(symbol="BTCUSDT")
    book.synced = True
    for i in range(1, 60):
        book.bids[100.0 - 0.01 * i] = 1.0
    book.asks[100.5] = 1.0
    # Two walls within 0.1% of each other (99.5 and 99.55)
    book.bids[99.50] = 5_000.0
    book.bids[99.55] = 5_000.0
    cands = scan("BTCUSDT", book, cfg, min_wall_usd=10_000.0)
    # Each wall alone is below 10k threshold (5k * 99.5 ≈ 497k actually, way above).
    # Actually qty=5000 * price=99.5 = ~497k, so they DO pass.
    # We just want to ensure aggregation merges them.
    merged = aggregate_zones(cands, zone_pct=0.10)
    bid_walls = [m for m in merged if m.side == "bid" and 99.4 < m.price < 99.6]
    assert len(bid_walls) == 1, f"expected 1 merged wall, got {bid_walls}"
    # Merged usd ≈ sum of both (note: qty * price)
    assert bid_walls[0].usd_value > 900_000  # both ~497k merged ≈ ~995k
