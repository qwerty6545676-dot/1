"""Tests for the wall state machine and cooldown."""

from __future__ import annotations

from walls.cooldown import Cooldown
from walls.detector import Candidate
from walls.orderbook import OrderBook
from walls.settings import DetectorCfg, SizeTier
from walls.state import StateMachine, WallState


def _cfg(min_lifetime_sec: float = 60.0, grace: float = 0.0) -> DetectorCfg:
    return DetectorCfg(
        size_tiers=(SizeTier(0.0, 10_000.0),),
        max_distance_pct=3.0,
        min_distance_pct=0.05,
        min_lifetime_sec=min_lifetime_sec,
        relative_size_multiplier=3.0,
        neighbour_levels=20,
        zone_aggregation_pct=0.10,
        cold_start_grace_sec=grace,
        execution_window_sec=5.0,
    )


def _cand(price: float = 99.0, side: str = "bid", usd: float = 1_000_000.0) -> Candidate:
    return Candidate(
        symbol="BTCUSDT",
        side=side,
        price=price,
        qty=usd / price,
        usd_value=usd,
        distance_pct=1.0,
        mid_price=100.0,
    )


def test_pending_promotes_to_active_after_min_lifetime() -> None:
    cfg = _cfg(min_lifetime_sec=60.0, grace=0.0)
    sm = StateMachine(cfg=cfg, started_at_ms=0)
    # Tick 0: first observation
    sm.observe([_cand()], now_ms=0)
    assert sm.tick({"BTCUSDT": OrderBook("BTCUSDT")}, now_ms=0) == []

    # Tick 30s: still pending, still observed
    sm.observe([_cand()], now_ms=30_000)
    assert sm.tick({"BTCUSDT": OrderBook("BTCUSDT")}, now_ms=30_000) == []

    # Tick 60s: should promote and emit "appeared"
    sm.observe([_cand()], now_ms=60_000)
    events = sm.tick({"BTCUSDT": OrderBook("BTCUSDT")}, now_ms=60_000)
    assert len(events) == 1
    assert events[0].kind == "appeared"
    assert events[0].wall.state == WallState.ACTIVE


def test_walls_in_grace_window_silently_activate() -> None:
    cfg = _cfg(min_lifetime_sec=10.0, grace=120.0)
    sm = StateMachine(cfg=cfg, started_at_ms=0)
    sm.observe([_cand()], now_ms=0)
    sm.observe([_cand()], now_ms=10_000)
    events = sm.tick({"BTCUSDT": OrderBook("BTCUSDT")}, now_ms=10_000)
    # First-seen ts (0) < grace_until (120_000) → silent.
    assert events == []
    # Wall is still tracked and ACTIVE.
    fp = StateMachine.fingerprint("BTCUSDT", "bid", 99.0, 1_000_000.0)
    assert sm.tracked[fp].state == WallState.ACTIVE


def test_wall_after_grace_emits_appeared() -> None:
    cfg = _cfg(min_lifetime_sec=10.0, grace=120.0)
    sm = StateMachine(cfg=cfg, started_at_ms=0)
    # Wall first seen at t=200_000 (after grace ends at 120_000)
    sm.observe([_cand()], now_ms=200_000)
    sm.observe([_cand()], now_ms=210_000)
    events = sm.tick({"BTCUSDT": OrderBook("BTCUSDT")}, now_ms=210_000)
    assert len(events) == 1
    assert events[0].kind == "appeared"


def test_active_wall_disappears_emits_cancelled_when_no_cross() -> None:
    cfg = _cfg(min_lifetime_sec=10.0, grace=0.0)
    sm = StateMachine(cfg=cfg, started_at_ms=0)
    book = OrderBook("BTCUSDT")
    book.synced = True
    # Plant some mid history that does NOT cross the wall at price 99.0.
    book.mid_history = [(0, 100.0), (5_000, 100.0), (10_000, 100.0)]

    sm.observe([_cand()], now_ms=0)
    sm.observe([_cand()], now_ms=10_000)
    events = sm.tick({"BTCUSDT": book}, now_ms=10_000)
    assert len(events) == 1 and events[0].kind == "appeared"

    # Now the wall vanishes (no observe call). After 5s+ stale → emit cancelled.
    events = sm.tick({"BTCUSDT": book}, now_ms=20_000)
    assert any(e.kind == "cancelled" for e in events)


def test_active_wall_disappears_emits_executed_when_cross() -> None:
    cfg = _cfg(min_lifetime_sec=10.0, grace=0.0)
    sm = StateMachine(cfg=cfg, started_at_ms=0)
    book = OrderBook("BTCUSDT")
    book.synced = True
    # Mid drops below the bid wall at 99.0 within the execution window.
    book.mid_history = [(15_000, 100.0), (18_000, 98.5)]

    sm.observe([_cand()], now_ms=0)
    sm.observe([_cand()], now_ms=10_000)
    sm.tick({"BTCUSDT": book}, now_ms=10_000)  # promote
    # Wall vanishes; tick at 20_000s with execution_window=5s checks since 15_000.
    events = sm.tick({"BTCUSDT": book}, now_ms=20_000)
    assert any(e.kind == "executed" for e in events)


def test_pending_wall_disappearing_emits_nothing() -> None:
    cfg = _cfg(min_lifetime_sec=60.0, grace=0.0)
    sm = StateMachine(cfg=cfg, started_at_ms=0)
    sm.observe([_cand()], now_ms=0)
    # Wall vanishes before 60s lifetime.
    events = sm.tick({"BTCUSDT": OrderBook("BTCUSDT")}, now_ms=10_000)
    # PENDING wall that vanishes is silently dropped.
    assert events == []


def test_fingerprint_is_stable_for_small_perturbations() -> None:
    fp1 = StateMachine.fingerprint("BTCUSDT", "bid", 99_000.0, 1_000_000.0)
    fp2 = StateMachine.fingerprint("BTCUSDT", "bid", 99_010.0, 1_010_000.0)  # ~0.01% / 1%
    assert fp1 == fp2


def test_fingerprint_changes_for_different_walls() -> None:
    fp1 = StateMachine.fingerprint("BTCUSDT", "bid", 99_000.0, 1_000_000.0)
    fp2 = StateMachine.fingerprint("BTCUSDT", "ask", 99_000.0, 1_000_000.0)  # different side
    fp3 = StateMachine.fingerprint("BTCUSDT", "bid", 95_000.0, 1_000_000.0)  # ~4% off
    fp4 = StateMachine.fingerprint("BTCUSDT", "bid", 99_000.0, 5_000_000.0)  # 5× size
    assert fp1 != fp2 and fp1 != fp3 and fp1 != fp4


def test_cooldown_blocks_within_ttl() -> None:
    cd = Cooldown(ttl_sec=60.0)
    assert cd.allow("X", now_ms=0) is True
    assert cd.allow("X", now_ms=30_000) is False  # within TTL
    assert cd.allow("X", now_ms=60_001) is True  # past TTL


def test_cooldown_independent_per_fingerprint() -> None:
    cd = Cooldown(ttl_sec=60.0)
    assert cd.allow("X", now_ms=0) is True
    assert cd.allow("Y", now_ms=0) is True
