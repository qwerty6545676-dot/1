"""Optional FastAPI heatmap dashboard.

Exposes a single HTML page and a JSON API endpoint that the frontend polls
every ``WEB_REFRESH_MS``. Rendering happens entirely client-side; the server
just returns the current order-book / wall / iceberg state.

Disabled by default — enable with ``WEB_ENABLED=true`` in ``.env``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import log
from .iceberg import IcebergEvent
from .orderbook import OrderBook
from .settings import WebCfg
from .state import StateEvent, TrackedWall, WallState

_log = log.get("web")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class WebState:
    """Read-only snapshot the dashboard reads from the running scanner."""

    def __init__(self, cfg: WebCfg) -> None:
        self.cfg = cfg
        self.symbols: list[str] = []
        # symbol -> OrderBook (live reference, updated in scan loop)
        self.books: dict[str, OrderBook] = {}
        # symbol -> mode name
        self.modes: dict[str, str] = {}
        # fingerprint -> TrackedWall (live reference)
        self.tracked_walls: dict[str, TrackedWall] = {}
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=200)
        self.recent_icebergs: deque[dict[str, Any]] = deque(maxlen=100)
        self.started_at_ms: int = 0

    def record_wall_event(self, evt: StateEvent, ts_ms: int) -> None:
        self.recent_events.appendleft(
            {
                "ts_ms": ts_ms,
                "kind": evt.kind,
                "symbol": evt.wall.symbol,
                "side": evt.wall.side,
                "price": evt.wall.price,
                "usd_value": evt.wall.usd_value,
                "distance_pct": evt.wall.distance_pct,
            }
        )

    def record_iceberg(self, evt: IcebergEvent, ts_ms: int) -> None:
        self.recent_icebergs.appendleft(
            {
                "ts_ms": ts_ms,
                "symbol": evt.symbol,
                "side": evt.side,
                "price": evt.price,
                "visible_usd": evt.visible_usd,
                "cumulative_usd": evt.cumulative_usd,
                "regen_count": evt.regen_count,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        n = self.cfg.levels_per_side
        for sym in self.symbols:
            book = self.books.get(sym)
            if book is None or not book.synced:
                continue
            mid = book.mid()
            if mid is None:
                continue
            bb = book.best_bid() or mid
            ba = book.best_ask() or mid
            top_bids = sorted(book.bids.items(), key=lambda kv: -kv[0])[:n]
            top_asks = sorted(book.asks.items(), key=lambda kv: kv[0])[:n]
            walls_here = [
                {
                    "side": w.side,
                    "price": w.price,
                    "usd_value": w.usd_value,
                    "state": w.state.value if isinstance(w.state, WallState) else str(w.state),
                }
                for w in self.tracked_walls.values()
                if w.symbol == sym
            ]
            out.append(
                {
                    "symbol": sym,
                    "mode": self.modes.get(sym, ""),
                    "mid": mid,
                    "best_bid": bb,
                    "best_ask": ba,
                    "bids": [{"price": p, "qty": q, "usd": p * q} for p, q in top_bids],
                    "asks": [{"price": p, "qty": q, "usd": p * q} for p, q in top_asks],
                    "walls": walls_here,
                }
            )
        return {
            "started_at_ms": self.started_at_ms,
            "refresh_ms": self.cfg.refresh_ms,
            "symbols": out,
            "events": list(self.recent_events)[:50],
            "icebergs": list(self.recent_icebergs)[:30],
        }


def make_app(state: WebState) -> Any:
    """Build a FastAPI app over the given WebState. Imports are lazy so the
    web dependency is optional."""
    try:
        from fastapi import FastAPI
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise RuntimeError(
            "FastAPI is not installed. Install with `pip install -e .[web]`."
        ) from e

    app = FastAPI(title="wall-scanner heatmap", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(state.snapshot())

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


async def serve(cfg: WebCfg, state: WebState, stop: Callable[[], bool]) -> None:
    """Run uvicorn in-process. Returns when ``stop()`` becomes True."""
    try:
        import uvicorn
    except ImportError as e:
        raise RuntimeError(
            "uvicorn is not installed. Install with `pip install -e .[web]`."
        ) from e

    app = make_app(state)
    config = uvicorn.Config(
        app, host=cfg.host, port=cfg.port, log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    _log.info("web dashboard listening on http://%s:%d/", cfg.host, cfg.port)
    # Run in the same loop; stop() is checked by main loop, server is cancelled there.
    await server.serve()
