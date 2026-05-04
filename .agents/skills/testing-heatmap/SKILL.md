---
name: testing-heatmap
description: End-to-end test the wall-scanner heatmap dashboard and iceberg detector against live Binance data. Use when verifying changes to walls/web.py, walls/iceberg.py, walls/static/index.html, OrderBook callbacks, or anything that affects the /api/state payload.
---

# Testing the heatmap dashboard + iceberg detector

The scanner reads live Binance spot order books from `data-stream.binance.vision` (works without VPN/VPS in most geos). Both the iceberg detector and the heatmap dashboard are off by default — enable them via env vars.

## Devin secrets needed

None for the dashboard test path. Telegram routing requires `TG_BOT_TOKEN` + `TG_CHAT_ID`, but you should leave `TG_ENABLED=false` for UI testing.

## Fast-feedback `.env` (icebergs in <10 s, walls in <30 s)

Write this to `/tmp/.test_env` and pass with `--env`:

```
TG_ENABLED=false
MODE_BTC_ENABLED=true
MODE_ETH_ENABLED=true
MODE_ALTS_ENABLED=true
MODE_ALTS_TOP_N=4
MIN_LIFETIME_SEC=5
COLD_START_GRACE_SEC=3
RELATIVE_SIZE_MULTIPLIER=2.0
MODE_BTC_MIN_WALL_USD=300000
MODE_ETH_MIN_WALL_USD=200000
MODE_ALTS_MIN_WALL_USD=100000
ICEBERG_ENABLED=true
ICEBERG_MIN_VISIBLE_USD=10000
ICEBERG_MIN_REGENS=2
ICEBERG_REGEN_WINDOW_SEC=30
WEB_ENABLED=true
WEB_HOST=127.0.0.1
WEB_PORT=8765
WEB_REFRESH_MS=1500
WEB_LEVELS_PER_SIDE=20
WALLS_LOG_PATH=/tmp/.test_walls.jsonl
LOG_LEVEL=INFO
```

Production defaults (60 s persistence, 4 regens) are too slow for a single recorded test — use the values above only for testing. Keep them out of any committed config.

## Launch

The `wall-scanner` entry point lives in the project's venv at `<repo>/.venv/bin/wall-scanner`. The PATH in fresh shell sessions usually does NOT include it, so call it by absolute path:

```
cd <repo> && rm -f /tmp/.test_walls.jsonl && \
  <repo>/.venv/bin/wall-scanner --env /tmp/.test_env
```

Run in background (testing tools: `run_in_background=true`). Wait ~10 s for the books to sync (`book synced` lines in the log) before opening the dashboard.

## Sanity-check the API before opening the UI

```
curl -s http://127.0.0.1:8765/api/state | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("symbols=", len(d["symbols"]), "events=", len(d["events"]), "icebergs=", len(d["icebergs"]))'
```

At least 4–6 symbols with non-null `mid` should appear in the first 5 s.

## What "pass" looks like in the UI (`http://127.0.0.1:8765/`)

- Header: `N symbols · refresh every 1500 ms · last update HH:MM:SS`.
- Each symbol card shows mode tag (`btc`/`eth`/`alts`), `mid <price>`, then bid+ask ladders separated by a `— mid <price> —` divider.
- Wall markers come from `walls/static/index.html` — gold `▌` for ACTIVE walls, `·` for PENDING. They render to the LEFT of the price column. SOLUSDT and BTCUSDT are reliable producers.
- Right column has two stacked feeds: "Recent wall events" (green=appeared, red=cancelled, white=executed) and "Recent icebergs" (with `(N×)` regen count).

## Adversarial checks (each fails if a feature is broken)

- **Symbols/ladders empty after 30 s** → orderbook sync failure or `WEB_LEVELS_PER_SIDE=0`.
- **No ▌/· markers anywhere** → wall detector not firing into `tracked_walls` (check `walls/state.py` and `MIN_LIFETIME_SEC`).
- **Iceberg feed stays empty** → `on_level_change` callback not wired in `walls/main.py`, or `ICEBERG_ENABLED=false`.
- **Ladders frozen across snapshots** → frontend polling broken, or `WebState.snapshot()` returning a cached object instead of reading the live `OrderBook`.
- **Mid price never moves** → same as above; pick BTCUSDT as reference, it always drifts within 5 s.

## Recording tips

- Maximize the browser before recording: `wmctrl -r :ACTIVE: -b add,maximized_vert,maximized_horz`.
- The dashboard packs a lot per row — use `computer` zoom on (0,60,380,400) for BTC, (610,60,800,400) for SOLUSDT (best wall density), (820,460,1024,760) for the iceberg feed.
- Live-polling proof works best by sampling the BTCUSDT mid 3 times spread over ~10 s — production volatility on Binance is enough.

## Known not-tested-live (covered by unit tests)

- Telegram routing of iceberg alerts → `tests/test_notifier.py`.
- Cooldown windows ≥30 min → `tests/test_iceberg.py` (uses fake clock).
- Multi-client dashboard concurrency → not tested, FastAPI default loop handles it.

## Cleanup

Kill the scanner shell, then `rm -f /tmp/.test_env /tmp/.test_walls.jsonl`.
