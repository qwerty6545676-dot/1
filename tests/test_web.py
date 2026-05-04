"""Tests for the optional FastAPI heatmap dashboard.

Lazy-imported FastAPI is required; the dev extras include it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from walls.iceberg import IcebergEvent
from walls.orderbook import OrderBook
from walls.settings import WebCfg
from walls.state import StateEvent, TrackedWall, WallState
from walls.web import WebState, make_app


def _cfg() -> WebCfg:
    return WebCfg(
        enabled=True, host="127.0.0.1", port=8000,
        refresh_ms=1000, levels_per_side=5,
    )


def _book() -> OrderBook:
    b = OrderBook(symbol="BTCUSDT")
    snap = {
        "lastUpdateId": 1,
        "bids": [["79900", "0.5"], ["79800", "0.3"], ["79700", "10.0"], ["79600", "0.1"]],
        "asks": [["80000", "0.5"], ["80100", "0.4"], ["80200", "12.0"]],
    }
    b.apply_snapshot(snap)
    return b


def test_state_endpoint_returns_orderbook() -> None:
    state = WebState(_cfg())
    state.symbols = ["BTCUSDT"]
    state.modes = {"BTCUSDT": "btc"}
    state.books = {"BTCUSDT": _book()}
    app = make_app(state)
    with TestClient(app) as client:
        r = client.get("/api/state")
        assert r.status_code == 200
        data = r.json()
        assert data["refresh_ms"] == 1000
        assert len(data["symbols"]) == 1
        sym = data["symbols"][0]
        assert sym["symbol"] == "BTCUSDT"
        assert sym["mode"] == "btc"
        assert len(sym["bids"]) <= 5
        # Best bid is highest price; ordering preserved.
        assert sym["bids"][0]["price"] == 79900.0
        assert sym["asks"][0]["price"] == 80000.0
        assert sym["mid"] == pytest.approx((79900.0 + 80000.0) / 2.0)


def test_root_serves_html() -> None:
    state = WebState(_cfg())
    app = make_app(state)
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "Wall Scanner" in r.text


def test_recent_events_appear_in_state() -> None:
    state = WebState(_cfg())
    state.symbols = ["BTCUSDT"]
    state.books = {"BTCUSDT": _book()}
    wall = TrackedWall(
        fingerprint="fp1", symbol="BTCUSDT", side="bid", price=79700.0,
        qty=10.0, usd_value=797_000.0, state=WallState.ACTIVE,
        first_seen_ts_ms=1, last_seen_ts_ms=2,
        distance_pct=0.25, mid_price=79950.0,
    )
    evt = StateEvent(kind="appeared", wall=wall)
    state.record_wall_event(evt, ts_ms=12345)
    ic_evt = IcebergEvent(
        symbol="ETHUSDT", side="ask", price=2500.0,
        visible_qty=4.0, visible_usd=10_000.0,
        cumulative_qty=20.0, cumulative_usd=50_000.0,
        regen_count=5, first_seen_ts_ms=1, last_seen_ts_ms=2,
    )
    state.record_iceberg(ic_evt, ts_ms=23456)
    app = make_app(state)
    with TestClient(app) as client:
        data = client.get("/api/state").json()
        assert len(data["events"]) == 1
        assert data["events"][0]["symbol"] == "BTCUSDT"
        assert data["events"][0]["kind"] == "appeared"
        assert len(data["icebergs"]) == 1
        assert data["icebergs"][0]["symbol"] == "ETHUSDT"
        assert data["icebergs"][0]["regen_count"] == 5


def test_unsynced_book_excluded() -> None:
    state = WebState(_cfg())
    state.symbols = ["BTCUSDT"]
    state.books = {"BTCUSDT": OrderBook(symbol="BTCUSDT")}  # not synced
    app = make_app(state)
    with TestClient(app) as client:
        data = client.get("/api/state").json()
        assert data["symbols"] == []


def test_walls_attached_to_symbol() -> None:
    state = WebState(_cfg())
    state.symbols = ["BTCUSDT"]
    state.books = {"BTCUSDT": _book()}
    wall = TrackedWall(
        fingerprint="fp1", symbol="BTCUSDT", side="bid", price=79700.0,
        qty=10.0, usd_value=797_000.0, state=WallState.ACTIVE,
        first_seen_ts_ms=1, last_seen_ts_ms=2,
        distance_pct=0.25, mid_price=79950.0,
    )
    state.tracked_walls = {"fp1": wall}
    app = make_app(state)
    with TestClient(app) as client:
        sym = client.get("/api/state").json()["symbols"][0]
        assert len(sym["walls"]) == 1
        assert sym["walls"][0]["price"] == 79700.0
        assert sym["walls"][0]["state"] == "ACTIVE"
