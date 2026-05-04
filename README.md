# Binance Spot Wall Scanner

Real-time detector for **large limit orders that hold price** on Binance spot —
the "walls" that act as visible support or resistance, plus **iceberg-order
detection** for whales that hide their footprint, plus an optional **live
heatmap dashboard** in the browser. Sends Telegram alerts on four
high-signal events:

- **🟩 / 🟥 Wall appeared** — a new big bid/ask order has been sitting in the
  book for at least *60 seconds* (default), close to the mid-price.
- **⬜️ Wall cancelled** — an active wall vanished without being eaten.
  Often a leading signal that the level no longer holds.
- **💥 Wall executed** — an active wall was hit by aggressive flow and the
  price crossed it. Confirmed break of support/resistance.
- **🧊 Iceberg detected** — same price level keeps refilling after each fill
  with a similar visible quantity. Signals a large hidden order being worked
  through the book ([details below](#iceberg-detection)).

## Why this is not yet another spam bot

The single biggest design constraint was: **don't spam**. The scanner does
four things to keep alerts meaningful:

1. **Persistence filter (default 60 s).** A wall must sit in the book for at
   least a minute before it counts. Spoofing bots ping orders for milliseconds —
   they're filtered out automatically.
2. **State machine, not raw stream.** Alerts fire only on state *transitions*
   (`PENDING → ACTIVE`, `ACTIVE → EXECUTED/CANCELLED`). A wall sitting still
   stays silent.
3. **Fingerprint cooldown (default 30 min).** The same wall — by `(symbol,
   side, log-bucketed price, log-bucketed size)` — cannot re-alert for 30
   minutes, even across appearance/disappearance flickers.
4. **Zone aggregation.** Walls within 0.1 % of each other are merged into a
   single liquidity zone before alerting, so a "stack of walls" produces one
   alert, not five.

Plus a **2-minute cold-start grace period**: walls that were already in the
book when the scanner launched are tracked silently, so you don't get a flood
of "wall appeared" messages right after startup.

## How "executed" vs "cancelled" is decided

Every second the scanner records a sample of mid-price for each symbol. When
an active wall vanishes, it asks: *did mid-price cross the wall price within
the last 5 seconds?*

- Bid wall (support) at $99 — if mid dipped below $99 → **executed**
- Bid wall — if mid stayed above → **cancelled** (owner removed the order)
- Ask wall (resistance) at $101 — if mid rose above $101 → **executed**
- Ask wall — if mid stayed below → **cancelled**

## Geo-block-friendly endpoints

The scanner targets `data-api.binance.vision` (REST) and
`data-stream.binance.vision` (WebSocket) by default — Binance's public,
read-only **market-data CDN**. Those hosts work in many regions where the
main `api.binance.com` / `stream.binance.com` are geo-blocked, because
they expose only public market data (no trading). If you do have direct
access to the main API, override `BINANCE_REST_BASE` and `BINANCE_WS_BASE`
in your `.env`.

## Three trading modes

The scanner watches three independent universes that you can toggle
on / off:

| Mode | What it watches | Default USD threshold |
|---|---|---|
| **BTC**  | `BTCUSDT` only           | $1,000,000 |
| **ETH**  | `ETHUSDT` only           | $500,000  |
| **Alts** | Top-48 alt pairs by 24 h volume (excl. BTC, ETH) | $150,000  |

Disable any combination via `MODE_BTC_ENABLED=false`,
`MODE_ETH_ENABLED=false`, `MODE_ALTS_ENABLED=false` in your `.env`.

## Iceberg detection

A regular wall may be just a single big limit order. A **whale** typically
doesn't show a $50M order all at once — they use **iceberg orders**: a small
visible chunk that gets *replaced with the same size* every time it's eaten.
From the order-book viewer's side, the level keeps disappearing and
reappearing at the same price.

The scanner watches every level update (not just snapshots) and counts
those eat→regen cycles. When the same level refills `≥ ICEBERG_MIN_REGENS`
times in `ICEBERG_LOOKBACK_SEC` (defaults: 4 cycles in 10 minutes), it
emits a `🧊 iceberg` alert with the cumulative flow that's gone through
the level — a lower bound on the whale's hidden size.

Tunable knobs in `.env`:

```env
ICEBERG_ENABLED=true
ICEBERG_MIN_VISIBLE_USD=25000
ICEBERG_MAX_DISTANCE_PCT=1.5
ICEBERG_EAT_THRESHOLD_RATIO=0.30
ICEBERG_REGEN_WINDOW_SEC=10
ICEBERG_REGEN_MATCH_LO=0.7
ICEBERG_REGEN_MATCH_HI=1.4
ICEBERG_MIN_REGENS=4
ICEBERG_LOOKBACK_SEC=600
ICEBERG_COOLDOWN_TTL_SEC=1800

# v2: anti-spoofing — confirm every regen with a real trade
ICEBERG_REQUIRE_TRADE_CONFIRMATION=true
ICEBERG_TRADE_WINDOW_MS=2000
ICEBERG_TRADE_MIN_QTY_RATIO=0.30
```

**Anti-spoofing (v2).** The original detector counts a level as "iceberg"
whenever it keeps refilling — but a spoofer can imitate that with
`cancel/replace` games, never actually executing. With
`ICEBERG_REQUIRE_TRADE_CONFIRMATION=true` (the default), the scanner
opens a parallel `<symbol>@trade` WebSocket and only counts a regen if a
**real trade** was observed at that exact level within
`±ICEBERG_TRADE_WINDOW_MS` and the trade's qty was at least
`ICEBERG_TRADE_MIN_QTY_RATIO × eaten_qty`. Cancel/replace events that
weren't backed by an actual fill are dropped as spoofs and counted on a
separate `rejected_spoof` counter. Set the flag to `false` to fall back
to v1 behaviour (one WS per symbol, more false positives).

See [GUIDE.md §8](GUIDE.md#8-iceberg-detection--поиск-скрытых-китов)
for the full algorithm.

## Heatmap dashboard

Optional FastAPI web UI showing live order books for all watched symbols,
with active walls highlighted and a feed of recent wall + iceberg events.

```bash
pip install -e ".[web]"
# add to .env:
#   WEB_ENABLED=true
#   WEB_HOST=127.0.0.1
#   WEB_PORT=8000
wall-scanner
# open http://127.0.0.1:8000/
```

**v2 features:**

- **Mid-price sparkline** in every symbol card (≤60 points over the last
  ~5 minutes, bucket-averaged downsample, colour-coded green/red/blue
  with a Δ% delta).
- **Mode chips + symbol search** in the toolbar — purely client-side
  filter; no API refetch, just hide cards.
- **Export PNG** button — renders the entire dashboard to a PNG file
  using SVG `foreignObject` → `canvas.toBlob()` (no external libs like
  html2canvas).
- **Anti-spoof counter** in the header (`🧊 confirmed: N · rejected: M`)
  when `ICEBERG_REQUIRE_TRADE_CONFIRMATION=true`.

The dashboard is **localhost-only by default** and has no authentication —
do not expose `WEB_HOST=0.0.0.0` to the internet without putting a reverse
proxy with auth in front of it. See [GUIDE.md §9](GUIDE.md#9-heatmap-дашборд--live-картинка-стакана).

## Quick start

```bash
# 1. install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. configure
cp .env.example .env
# Edit .env: set TG_BOT_TOKEN, TG_CHAT_ID, tweak filters/modes if you want.

# 3. run
wall-scanner
# or:
python -m walls.main
```

Press `Ctrl+C` to stop cleanly.

See [GUIDE.md](GUIDE.md) for the full Russian-language guide covering setup,
each filter explained, alert reading, and tuning recipes.

## Configuration cheat-sheet (`.env`)

Filters that apply to every enabled mode:

| Variable | Default | What it does |
|---|---|---|
| `MODE_*_ENABLED` | `true` | Turn each of BTC / ETH / Alts modes on or off independently. |
| `MODE_BTC_MIN_WALL_USD` | `1000000` | Minimum wall size for BTCUSDT. |
| `MODE_ETH_MIN_WALL_USD` | `500000`  | Minimum wall size for ETHUSDT. |
| `MODE_ALTS_MIN_WALL_USD` | `150000` | Minimum wall size for alt pairs. |
| `MODE_ALTS_TOP_N` | `48` | How many alt pairs to follow (by 24 h USD volume). |
| `MAX_DISTANCE_PCT` | `3.0` | Walls farther than this from mid-price are ignored. |
| `MIN_DISTANCE_PCT` | `0.05` | Walls closer than this to mid-price are ignored. |
| `MIN_LIFETIME_SEC` | `60` | How long a wall must sit before alerting (anti-spoof). |
| `RELATIVE_SIZE_MULTIPLIER` | `3.0` | Wall must be ≥ N × the median of nearby levels. |
| `NEIGHBOUR_LEVELS` | `20` | Window over which the median is computed. |
| `ZONE_AGGREGATION_PCT` | `0.10` | Walls within this percentage are merged. |
| `COLD_START_GRACE_SEC` | `120` | Silent observation window after startup. |
| `EXECUTION_WINDOW_SEC` | `5` | Window for classifying executed vs cancelled. |
| `COOLDOWN_TTL_SEC` | `1800` | Same wall can't re-alert within this many seconds. |
| `TG_BOT_TOKEN` | — | Bot token from @BotFather. |
| `TG_CHAT_ID` | — | Your numeric chat id (DM, group, or channel). |
| `TG_TIER_LOW_USD` / `TG_TIER_MID_USD` / `TG_TIER_HIGH_USD` | `150k / 500k / 2M` | Tier thresholds for routing. |
| `ICEBERG_ENABLED` | `true` | Toggle iceberg detector. |
| `ICEBERG_MIN_REGENS` | `4` | Refill cycles required for an iceberg alert. |
| `ICEBERG_REGEN_WINDOW_SEC` | `10` | Time window after an eat for a refill to count. |
| `WEB_ENABLED` | `false` | Run the optional heatmap dashboard. |
| `WEB_PORT` | `8000` | Port for the dashboard. |

## Output

- **Telegram** — formatted HTML messages, optionally routed to three forum
  topics (low / mid / high) by USD size.
- **`data/walls.jsonl`** — every state-transition event (incl. iceberg
  detections), one JSON record per line, for offline analysis or backtesting.
- **Heatmap dashboard** (optional, `WEB_ENABLED=true`) — `http://127.0.0.1:8000/`
  with live bid/ask ladder per symbol, active-wall markers, recent events feed,
  and a separate iceberg feed.

## Architecture

```
┌────────────────────┐     ┌──────────────────────┐
│ data-api.vision    │     │ data-stream.vision   │
│   REST snapshots   │     │   depth diff streams │
└─────────┬──────────┘     └──────────┬───────────┘
          │                           │
          ▼                           ▼
   ┌────────────────────────────────────────┐
   │ OrderBook (per symbol)                 │
   │   • snapshot + diff merge              │
   │   • continuity tracking                │
   │   • mid-price history                  │
   │   • on_level_change callback ──────────┼───────────┐
   └────────────────┬───────────────────────┘           │
                    │                                   ▼
                    ▼                       ┌──────────────────────┐
        ┌────────────────────────┐          │ Iceberg Detector     │
        │ Detector               │          │   • per-level state  │
        │   • USD size threshold │          │   • eat / regen      │
        │   • distance from mid  │          │     counter          │
        │   • relative size vs   │          │   • lookback window  │
        │     median neighbours  │          │   • cooldown         │
        │   • zone aggregation   │          └──────────┬───────────┘
        └────────────┬───────────┘                     │
                     ▼                                 │
        ┌────────────────────────┐                     │
        │ State Machine          │                     │
        │   PENDING → ACTIVE     │                     │
        │   ACTIVE  → EXECUTED   │                     │
        │   ACTIVE  → CANCELLED  │                     │
        └────────────┬───────────┘                     │
                     ▼                                 ▼
   ┌─────────────┐  ┌─────────────────────────────────────┐
   │  Cooldown   │→ │  Notifier  (Telegram, tier-routed)  │
   │ (30 min/fp) │  └─────────────────────────────────────┘
   └─────────────┘                  │
                                    ▼
                       ┌─────────┐  ┌──────────────────┐
                       │ JSONL   │  │ Web heatmap      │
                       │ log     │  │ (optional, FastAPI)│
                       └─────────┘  └──────────────────┘
```

## Tests

```bash
pytest -q
```

## License

MIT.
