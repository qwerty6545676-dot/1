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
from .binance_ws import run_with_reconnect
from .cooldown import Cooldown
from .detector import aggregate_zones, scan
from .notifier import TelegramNotifier
from .orderbook import FirstEventGate, OrderBook
from .persistence import JsonlWriter
from .settings import Settings, load
from .state import StateEvent, StateMachine, WallState
from .universe import SymbolInfo, select_top_n

_log = log.get("main")


@dataclass
class _SymbolCtx:
    info: SymbolInfo
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
        self.symbols: dict[str, _SymbolCtx] = {}
        self._stop = asyncio.Event()

    # --------------------------------------------------------------- lifecycle
    def request_stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------- universe
    async def _build_universe(self, rest: BinanceRest) -> list[SymbolInfo]:
        rows = await select_top_n(
            rest,
            top_n=self.s.universe.top_n,
            quote_assets=self.s.universe.quote_assets,
        )
        if not rows:
            raise RuntimeError("Universe is empty — REST returned no usable tickers.")
        _log.info("universe: %d symbols selected", len(rows))
        for r in rows[:5]:
            _log.info("  - %s vol=$%.0fM  last=%s", r.symbol, r.quote_volume_24h / 1e6, r.last_price)
        if len(rows) > 5:
            _log.info("  ... and %d more", len(rows) - 5)
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
                await notifier.send(evt)

            self.cooldown.gc(now_ms)
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

    # ------------------------------------------------------------------- run
    async def run(self) -> None:
        log.configure(self.s.log_level)
        async with BinanceRest(
            self.s.binance.rest_base, timeout_sec=self.s.binance.rest_request_timeout_sec
        ) as rest, TelegramNotifier(self.s.telegram) as notifier:
            universe = await self._build_universe(rest)
            for info in universe:
                ctx = _SymbolCtx(
                    info=info,
                    book=OrderBook(symbol=info.symbol),
                    gate=FirstEventGate(),
                    pending_evts=[],
                    min_wall_usd=self.s.detector.min_wall_usd_for(info.volume_24h_usd),
                )
                self.symbols[info.symbol] = ctx

            tasks = [
                asyncio.create_task(self._ws_for_symbol(ctx, rest), name=f"sym-{sym}")
                for sym, ctx in self.symbols.items()
            ]
            tasks.append(asyncio.create_task(self._detect_loop(notifier), name="detect"))

            await self._stop.wait()
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


async def _amain(settings_path: str) -> None:
    settings = load(settings_path)
    log.configure(settings.log_level)
    _log.info("starting wall-scanner — settings: %s", settings_path)
    scanner = Scanner(settings)
    _install_signal_handlers(scanner)
    await scanner.run()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="wall-scanner")
    parser.add_argument(
        "--settings",
        default=os.environ.get("WALL_SCANNER_SETTINGS", "settings.yaml"),
        help="Path to YAML settings file (default: ./settings.yaml)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_amain(args.settings))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    cli()
