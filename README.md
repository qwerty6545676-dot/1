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
access to the main API, just change `binance.rest_base` and `binance.ws_base`
in `settings.yaml`.

## Quick start

```bash
# 1. install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. configure
cp settings.example.yaml settings.yaml
# (edit thresholds if you want)

# 3. set Telegram env vars (optional — without them the scanner runs in
#    log-only mode and writes events to data/walls.jsonl)
export TG_BOT_TOKEN="123:abc..."
export TG_CHAT_ID="-100123..."   # group/channel id
# optional forum-topic ids (otherwise everything goes to the default topic)
export TG_TOPIC_LOW="…"
export TG_TOPIC_MID="…"
export TG_TOPIC_HIGH="…"

# 4. run
wall-scanner --settings settings.yaml
# or:
python -m walls.main --settings settings.yaml
```

Press `Ctrl+C` to stop cleanly.

## Configuration cheat-sheet

| Setting | Default | What it does |
|---|---|---|
| `universe.top_n` | 50 | How many spot pairs to watch (sorted by 24 h USD volume). |
| `universe.quote_assets` | `[USDT]` | Quote-asset filter. |
| `detector.size_tiers` | $1M / $500k / $150k | Minimum wall size in USD per volume tier. |
| `detector.max_distance_pct` | 3.0 | Walls farther than this from mid-price are ignored. |
| `detector.min_lifetime_sec` | 60 | How long a wall must sit before alerting (anti-spoof). |
| `detector.relative_size_multiplier` | 3.0 | Wall must be ≥ N × the median size of nearby levels. |
| `detector.zone_aggregation_pct` | 0.1 | Walls within this percentage are merged. |
| `detector.cold_start_grace_sec` | 120 | Silent observation window after startup. |
| `cooldown.fingerprint_ttl_sec` | 1800 | Same wall can't re-alert within this many seconds. |

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
