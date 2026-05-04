"""Entrypoint and orchestrator: wires everything together and runs forever."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
from dataclasses import dataclass

with contextlib.suppress(ImportError):  # uvloop is optional / unix-only
    import uvloop

    uvloop.install()

from . import log
from .binance_rest import BinanceRest, BinanceRestError
from .binance_ws import run_trade_stream_with_reconnect, run_with_reconnect
from .cooldown import Cooldown
from .detector import aggregate_zones, scan
from .iceberg import IcebergDetector, IcebergEvent
from .notifier import TelegramNotifier
from .orderbook import FirstEventGate, OrderBook
from .persistence import JsonlWriter
from .settings import ModeCfg, Settings, load
from .state import StateEvent, StateMachine, WallState
from .trade_buffer import TradeBuffer
from .universe import SymbolInfo, select_for_modes

_log = log.get("main")


@dataclass
class _SymbolCtx:
    info: SymbolInfo
    mode: ModeCfg
    book: OrderBook
    gate: FirstEventGate
    pending_evts: list[dict]
    min_wall_usd: float


class Scanner:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.started_at_ms = int(time.time() * 1000)
        self.state = StateMachine(cfg=settings.detector, started_at_ms=self.started_at_ms)
        self.cooldown = Cooldown(ttl_sec=settings.cooldown.fingerprint_ttl_sec)
        self.writer = JsonlWriter(settings.persistence.walls_log_path)
        # Trade buffer is created only when iceberg + anti-spoof are enabled
        # (no point paying for the trade WS connections otherwise).
        self.trade_buffer: TradeBuffer | None = None
        if settings.iceberg.enabled and settings.iceberg.require_trade_confirmation:
            # Retain trades long enough to confirm any eat that happens in the
            # regen window plus a safety margin.
            retention_ms = (
                int(settings.iceberg.regen_window_sec * 1000)
                + settings.iceberg.trade_window_ms
                + 5000
            )
            self.trade_buffer = TradeBuffer(retention_ms=retention_ms)
        self.iceberg = IcebergDetector(settings.iceberg, trade_buffer=self.trade_buffer)
        self.symbols: dict[str, _SymbolCtx] = {}
        # Pending iceberg events drained by the detect loop; populated by the
        # WS-driven on_level_change callback (sync) and read back asynchronously.
        self._iceberg_queue: list[IcebergEvent] = []
        self._stop = asyncio.Event()
        # Optional web dashboard state — only populated if WEB_ENABLED.
        self.web_state = None
        if settings.web.enabled:
            from .web import WebState  # local import: optional dep
            self.web_state = WebState(settings.web)
            self.web_state.started_at_ms = self.started_at_ms
            self.web_state.tracked_walls = self.state.tracked
            self.web_state.iceberg = self.iceberg

    # --------------------------------------------------------------- lifecycle
    def request_stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------- universe
    async def _build_universe(self, rest: BinanceRest) -> list[tuple[SymbolInfo, ModeCfg]]:
        modes = self.s.enabled_modes()
        if not modes:
            raise RuntimeError(
                "All trading modes are disabled — enable at least one of "
                "MODE_BTC_ENABLED / MODE_ETH_ENABLED / MODE_ALTS_ENABLED."
            )
        rows = await select_for_modes(
            rest, modes=modes, quote_assets=self.s.quote_assets,
        )
        if not rows:
            raise RuntimeError("Universe is empty — REST returned no usable tickers.")
        # Log per-mode summary
        by_mode: dict[str, list[SymbolInfo]] = {}
        for info, m in rows:
            by_mode.setdefault(m.name, []).append(info)
        _log.info("universe: %d symbols across %d mode(s)", len(rows), len(by_mode))
        for mode_name, infos in by_mode.items():
            usd_cap = next(m.min_wall_usd for m in modes if m.name == mode_name)
            _log.info(
                "  mode=%s symbols=%d min_wall=$%.0fk",
                mode_name, len(infos), usd_cap / 1000.0,
            )
            for r in infos[:3]:
                _log.info(
                    "    - %s vol=$%.0fM  last=%s",
                    r.symbol, r.quote_volume_24h / 1e6, r.last_price,
                )
            if len(infos) > 3:
                _log.info("    ... and %d more", len(infos) - 3)
        return rows

    # -------------------------------------------------------------- per-symbol
    async def _resync_book(self, rest: BinanceRest, ctx: _SymbolCtx) -> bool:
        """Fetch a snapshot and apply buffered events. Returns True on success."""
        try:
            snap = await rest.depth(ctx.info.symbol, limit=self.s.orderbook.snapshot_limit)
        except BinanceRestError as e:
            _log.warning("%s: snapshot failed: %s", ctx.info.symbol, e)
            return False
        # Take a stable view of buffered events and clear the buffer atomically
        # under the asyncio single-thread model — no awaits between snapshot()
        # and here.
        pending = list(ctx.pending_evts)
        ctx.pending_evts.clear()
        ctx.book.apply_snapshot(snap)
        ctx.gate.reset(ctx.book.last_update_id)
        applied = 0
        first_applied = False
        for evt in pending:
            if not first_applied:
                if not ctx.gate.is_first_valid(evt):
                    continue
                first_applied = True
            if not ctx.book.apply_diff(evt):
                _log.warning("%s: continuity gap during snapshot drain", ctx.info.symbol)
                ctx.book.synced = False
                return False
            applied += 1
        _log.info(
            "%s: book synced (lastUpdateId=%d, drained=%d)",
            ctx.info.symbol,
            ctx.book.last_update_id,
            applied,
        )
        return True

    async def _trade_stream_for_symbol(self, ctx: _SymbolCtx) -> None:
        """One async task per symbol: receive @trade frames, append to buffer."""
        if self.trade_buffer is None:
            return
        buf = self.trade_buffer
        symbol = ctx.info.symbol

        def on_trade(evt: dict) -> None:
            # Binance @trade payload — see walls/trade_buffer.py docstring.
            try:
                price = float(evt["p"])
                qty = float(evt["q"])
                ts_ms = int(evt.get("T") or evt.get("E") or 0)
                buyer_is_maker = bool(evt.get("m", False))
            except (KeyError, TypeError, ValueError):
                return
            buf.record(symbol, price, qty, ts_ms, buyer_is_maker)

        await run_trade_stream_with_reconnect(
            ws_base=self.s.binance.ws_base,
            symbol=symbol,
            on_event=on_trade,
            reconnect_delay_sec=self.s.binance.ws_reconnect_delay_sec,
        )

    async def _ws_for_symbol(self, ctx: _SymbolCtx, rest: BinanceRest) -> None:
        """One async task per symbol: receive WS frames, apply to book."""
        # Pre-snapshot phase: open WS first, buffer events, then snapshot.
        # This is the order Binance recommends.
        snapshot_pending = asyncio.Event()
        snapshot_pending.set()  # need snapshot initially

        async def on_event(evt: dict) -> None:
            if snapshot_pending.is_set():
                # Buffer until snapshot is ready.
                ctx.pending_evts.append(evt)
                return
            if not ctx.book.synced:
                return
            ok = ctx.book.apply_diff(evt)
            if not ok:
                _log.warning("%s: out-of-order event, will resync", ctx.info.symbol)
                ctx.book.synced = False
                ctx.pending_evts.clear()
                snapshot_pending.set()

        async def on_disconnect() -> None:
            ctx.book.synced = False
            ctx.pending_evts.clear()
            snapshot_pending.set()

        ws_task = asyncio.create_task(
            run_with_reconnect(
                ws_base=self.s.binance.ws_base,
                symbol=ctx.info.symbol,
                on_event=on_event,
                on_disconnect=on_disconnect,
                reconnect_delay_sec=self.s.binance.ws_reconnect_delay_sec,
            ),
            name=f"ws-{ctx.info.symbol}",
        )

        async def snapshot_loop() -> None:
            while not self._stop.is_set():
                if snapshot_pending.is_set():
                    # small delay so the WS has buffered some events
                    await asyncio.sleep(0.5)
                    ok = await self._resync_book(rest, ctx)
                    if ok and ctx.book.synced:
                        snapshot_pending.clear()
                    else:
                        # Stay in pending mode so events keep buffering.
                        await asyncio.sleep(2.0)
                else:
                    await asyncio.sleep(0.2)

        snap_task = asyncio.create_task(snapshot_loop(), name=f"snap-{ctx.info.symbol}")
        try:
            await self._stop.wait()
        finally:
            ws_task.cancel()
            snap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ws_task
            with contextlib.suppress(asyncio.CancelledError):
                await snap_task

    # ----------------------------------------------------------------- detect
    async def _detect_loop(self, notifier: TelegramNotifier) -> None:
        cfg = self.s.detector
        interval = self.s.orderbook.scan_interval_sec
        while not self._stop.is_set():
            now_ms = int(time.time() * 1000)
            for ctx in self.symbols.values():
                if not ctx.book.synced:
                    continue
                ctx.book.record_mid(now_ms)
                cands = scan(ctx.info.symbol, ctx.book, cfg, ctx.min_wall_usd)
                cands = aggregate_zones(cands, cfg.zone_aggregation_pct)
                self.state.observe(cands, now_ms)

            events: list[StateEvent] = self.state.tick(
                {ctx.info.symbol: ctx.book for ctx in self.symbols.values()},
                now_ms,
            )

            for evt in events:
                fp = evt.wall.fingerprint
                if not self.cooldown.allow(f"{fp}|{evt.kind}", now_ms):
                    continue
                self._persist(evt, now_ms)
                _log.info(
                    "ALERT %-9s %s %s @ %g  size=%.0fk USD  dist=%.2f%%",
                    evt.kind.upper(),
                    evt.wall.symbol,
                    evt.wall.side,
                    evt.wall.price,
                    evt.wall.usd_value / 1000.0,
                    evt.wall.distance_pct,
                )
                if self.web_state is not None:
                    self.web_state.record_wall_event(evt, now_ms)
                await notifier.send(evt)

            # Drain any iceberg events accumulated by the on_level_change cb.
            if self._iceberg_queue:
                pending_ic = self._iceberg_queue[:]
                self._iceberg_queue.clear()
                for ic in pending_ic:
                    self._persist_iceberg(ic, now_ms)
                    _log.info(
                        "ICEBERG %s %s @ %g  visible=%.0fk  flow=%.0fk  regens=%d",
                        ic.symbol, ic.side, ic.price,
                        ic.visible_usd / 1000.0,
                        ic.cumulative_usd / 1000.0,
                        ic.regen_count,
                    )
                    if self.web_state is not None:
                        self.web_state.record_iceberg(ic, now_ms)
                    await notifier.send_iceberg(ic)

            self.cooldown.gc(now_ms)
            self.iceberg.gc(now_ms)
            if self.trade_buffer is not None:
                self.trade_buffer.gc(now_ms)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    def _persist(self, evt: StateEvent, now_ms: int) -> None:
        w = evt.wall
        rec = {
            "ts_ms": now_ms,
            "kind": evt.kind,
            "fingerprint": w.fingerprint,
            "symbol": w.symbol,
            "side": w.side,
            "state": w.state.value if isinstance(w.state, WallState) else str(w.state),
            "price": w.price,
            "qty": w.qty,
            "usd_value": w.usd_value,
            "distance_pct": w.distance_pct,
            "mid_price": w.mid_price,
            "first_seen_ms": w.first_seen_ts_ms,
            "last_seen_ms": w.last_seen_ts_ms,
        }
        try:
            self.writer.write(rec)
        except OSError as e:
            _log.warning("walls.jsonl write failed: %s", e)

    def _persist_iceberg(self, evt: IcebergEvent, now_ms: int) -> None:
        rec = {
            "ts_ms": now_ms,
            "kind": "iceberg",
            "fingerprint": evt.fingerprint,
            "symbol": evt.symbol,
            "side": evt.side,
            "price": evt.price,
            "visible_qty": evt.visible_qty,
            "visible_usd": evt.visible_usd,
            "cumulative_qty": evt.cumulative_qty,
            "cumulative_usd": evt.cumulative_usd,
            "regen_count": evt.regen_count,
            "first_seen_ms": evt.first_seen_ts_ms,
            "last_seen_ms": evt.last_seen_ts_ms,
        }
        try:
            self.writer.write(rec)
        except OSError as e:
            _log.warning("walls.jsonl write failed: %s", e)

    # ------------------------------------------------------------------- run
    async def run(self) -> None:
        log.configure(self.s.log_level)
        async with BinanceRest(
            self.s.binance.rest_base, timeout_sec=self.s.binance.rest_request_timeout_sec
        ) as rest, TelegramNotifier(self.s.telegram) as notifier:
            universe = await self._build_universe(rest)
            for info, mode in universe:
                book = OrderBook(symbol=info.symbol)
                # Wire the iceberg detector to this book's level changes.
                if self.s.iceberg.enabled:
                    sym = info.symbol

                    def _make_cb(b: OrderBook, s: str) -> object:
                        def _on_change(
                            side: str, price: float,
                            old_qty: float, new_qty: float, ts_ms: int,
                        ) -> None:
                            mid = b.mid()
                            ev = self.iceberg.observe_change(
                                s, side, price, old_qty, new_qty, ts_ms, mid,
                            )
                            if ev is not None:
                                self._iceberg_queue.append(ev)
                        return _on_change

                    book.on_level_change = _make_cb(book, sym)  # type: ignore[assignment]
                ctx = _SymbolCtx(
                    info=info,
                    mode=mode,
                    book=book,
                    gate=FirstEventGate(),
                    pending_evts=[],
                    min_wall_usd=mode.min_wall_usd,
                )
                self.symbols[info.symbol] = ctx

            # Populate the optional web state with live references.
            if self.web_state is not None:
                self.web_state.symbols = [info.symbol for info, _ in universe]
                self.web_state.books = {
                    sym: ctx.book for sym, ctx in self.symbols.items()
                }
                self.web_state.modes = {info.symbol: mode.name for info, mode in universe}

            tasks = [
                asyncio.create_task(self._ws_for_symbol(ctx, rest), name=f"sym-{sym}")
                for sym, ctx in self.symbols.items()
            ]
            if self.trade_buffer is not None:
                _log.info(
                    "iceberg anti-spoof on: subscribing %d trade streams "
                    "(window=%d ms, min ratio=%.2f)",
                    len(self.symbols),
                    self.s.iceberg.trade_window_ms,
                    self.s.iceberg.trade_min_qty_ratio,
                )
                for sym, ctx in self.symbols.items():
                    tasks.append(asyncio.create_task(
                        self._trade_stream_for_symbol(ctx), name=f"trd-{sym}",
                    ))
            tasks.append(asyncio.create_task(self._detect_loop(notifier), name="detect"))

            web_server = None
            if self.web_state is not None:
                from .web import make_app  # type: ignore[import-not-found]
                try:
                    import uvicorn
                except ImportError as e:
                    raise RuntimeError(
                        "WEB_ENABLED=true but FastAPI/uvicorn are not installed. "
                        "Install with: pip install -e .[web]"
                    ) from e
                app = make_app(self.web_state)
                config = uvicorn.Config(
                    app,
                    host=self.s.web.host,
                    port=self.s.web.port,
                    log_level="warning",
                    access_log=False,
                )
                web_server = uvicorn.Server(config)
                _log.info(
                    "starting web dashboard on http://%s:%d/",
                    self.s.web.host, self.s.web.port,
                )
                tasks.append(asyncio.create_task(web_server.serve(), name="web"))

            await self._stop.wait()
            if web_server is not None:
                web_server.should_exit = True
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t


def _install_signal_handlers(scanner: Scanner) -> None:
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, scanner.request_stop)
        except NotImplementedError:
            # Windows
            pass


async def _amain(env_path: str | None) -> None:
    settings = load(env_path)
    log.configure(settings.log_level)
    enabled = [m.name for m in settings.enabled_modes()]
    _log.info("starting wall-scanner — env=%s  modes=%s", env_path or "<none>", enabled)
    scanner = Scanner(settings)
    _install_signal_handlers(scanner)
    await scanner.run()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="wall-scanner")
    parser.add_argument(
        "--env",
        default=os.environ.get("WALL_SCANNER_ENV", ".env"),
        help="Path to .env file (default: ./.env). Pass empty string to skip "
             "and rely purely on existing environment variables.",
    )
    args = parser.parse_args()
    env_path: str | None = args.env if args.env else None
    try:
        asyncio.run(_amain(env_path))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    cli()
