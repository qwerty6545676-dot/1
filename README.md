# Binance Spot Wall Scanner

Real-time detector for **large limit orders that hold price** on Binance spot —
the "walls" that act as visible support or resistance. Sends Telegram alerts
on three high-signal events:

- **🟩 / 🟥 Wall appeared** — a new big bid/ask order has been sitting in the
  book for at least *60 seconds* (default), close to the mid-price.
- **⬜️ Wall cancelled** — an active wall vanished without being eaten.
  Often a leading signal that the level no longer holds.
- **💥 Wall executed** — an active wall was hit by aggressive flow and the
  price crossed it. Confirmed break of support/resistance.

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

## Output

- **Telegram** — formatted HTML messages, optionally routed to three forum
  topics (low / mid / high) by USD size.
- **`data/walls.jsonl`** — every state-transition event, one JSON record per
  line, for offline analysis or backtesting.

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
   │   • mid-price history (for executed/   │
   │     cancelled classification)          │
   └────────────────┬───────────────────────┘
                    │
                    ▼
        ┌────────────────────────┐
        │ Detector               │
        │   • USD size threshold │
        │   • distance from mid  │
        │   • relative size vs   │
        │     median neighbours  │
        │   • zone aggregation   │
        └────────────┬───────────┘
                     ▼
        ┌────────────────────────┐
        │ State Machine          │
        │   PENDING → ACTIVE     │
        │   ACTIVE  → EXECUTED   │
        │   ACTIVE  → CANCELLED  │
        └────────────┬───────────┘
                     ▼
   ┌─────────────┐  ┌─────────────┐
   │  Cooldown   │→ │  Notifier   │→ Telegram
   │ (30 min/fp) │  │ (tier route)│
   └─────────────┘  └─────────────┘
                     ▼
                ┌─────────┐
                │ JSONL   │
                └─────────┘
```

## Tests

```bash
pytest -q
```

## License

MIT.
