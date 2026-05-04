"""Typed configuration loaded from environment variables / .env file.

All knobs the user is expected to tweak live in `.env`. The schema is documented
inline in `.env.example` and in GUIDE.md.

Three trading "modes" are supported and can be enabled / disabled independently:

- BTC mode  — watches one symbol (default ``BTCUSDT``), high USD threshold.
- ETH mode  — watches one symbol (default ``ETHUSDT``), mid USD threshold.
- Alts mode — watches the top-N spot pairs by 24h volume excluding BTC and
              ETH, lower USD threshold.

Common detector / cooldown / order-book params are shared by all enabled modes;
only the wall-size threshold is per-mode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# --------------------------------------------------------------------- helpers
def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if (v is not None and v != "") else default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return int(v)


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return float(v)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_opt_int(name: str) -> int | None:
    v = os.environ.get(name)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


# --------------------------------------------------------------------- schema
@dataclass(frozen=True)
class BinanceCfg:
    rest_base: str
    ws_base: str
    ws_reconnect_delay_sec: float
    rest_request_timeout_sec: float


@dataclass(frozen=True)
class ModeCfg:
    """One trading mode (BTC / ETH / Alts).

    Each mode contributes some symbols to the watched universe. When enabled,
    a mode's ``min_wall_usd`` is used as the size threshold for those symbols.
    """

    name: str
    enabled: bool
    min_wall_usd: float
    # Explicit symbols for fixed-asset modes (BTC, ETH). Empty for Alts.
    symbols: tuple[str, ...]
    # For top-N modes (Alts): number of symbols to pick.
    top_n: int
    # For top-N modes: base assets to exclude (so Alts skips BTC / ETH).
    exclude_bases: tuple[str, ...]


@dataclass(frozen=True)
class DetectorCfg:
    """Shared by every enabled mode."""

    max_distance_pct: float
    min_distance_pct: float
    min_lifetime_sec: float
    relative_size_multiplier: float
    neighbour_levels: int
    zone_aggregation_pct: float
    cold_start_grace_sec: float
    execution_window_sec: float


@dataclass(frozen=True)
class OrderbookCfg:
    snapshot_limit: int
    scan_interval_sec: float


@dataclass(frozen=True)
class CooldownCfg:
    fingerprint_ttl_sec: float


@dataclass(frozen=True)
class TelegramCfg:
    enabled: bool
    bot_token: str | None
    chat_id: str | None
    # Optional forum-topic IDs. Use them only if you've set up a Telegram
    # supergroup with topics. For a regular DM with the bot, leave them blank.
    topic_low: int | None
    topic_mid: int | None
    topic_high: int | None
    tier_low_usd: float
    tier_mid_usd: float
    tier_high_usd: float


@dataclass(frozen=True)
class PersistenceCfg:
    walls_log_path: str


@dataclass(frozen=True)
class Settings:
    binance: BinanceCfg
    modes: tuple[ModeCfg, ...]
    quote_assets: tuple[str, ...]
    detector: DetectorCfg
    orderbook: OrderbookCfg
    cooldown: CooldownCfg
    telegram: TelegramCfg
    persistence: PersistenceCfg
    log_level: str

    def enabled_modes(self) -> tuple[ModeCfg, ...]:
        return tuple(m for m in self.modes if m.enabled)


# --------------------------------------------------------------------- loader
def load(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """Load settings from environment, optionally seeded by a ``.env`` file.

    Existing ``os.environ`` values always take precedence over the .env file.
    Pass ``env_file=None`` to skip loading any file (useful for tests where
    the test sets env vars directly).
    """
    if env_file is not None:
        path = Path(env_file)
        if path.exists():
            load_dotenv(path, override=False)

    binance = BinanceCfg(
        rest_base=_env_str("BINANCE_REST_BASE", "https://data-api.binance.vision").rstrip("/"),
        ws_base=_env_str("BINANCE_WS_BASE", "wss://data-stream.binance.vision").rstrip("/"),
        ws_reconnect_delay_sec=_env_float("BINANCE_WS_RECONNECT_DELAY_SEC", 5.0),
        rest_request_timeout_sec=_env_float("BINANCE_REST_TIMEOUT_SEC", 15.0),
    )

    modes = (
        ModeCfg(
            name="btc",
            enabled=_env_bool("MODE_BTC_ENABLED", True),
            min_wall_usd=_env_float("MODE_BTC_MIN_WALL_USD", 1_000_000.0),
            symbols=(_env_str("MODE_BTC_SYMBOL", "BTCUSDT").upper(),),
            top_n=0,
            exclude_bases=(),
        ),
        ModeCfg(
            name="eth",
            enabled=_env_bool("MODE_ETH_ENABLED", True),
            min_wall_usd=_env_float("MODE_ETH_MIN_WALL_USD", 500_000.0),
            symbols=(_env_str("MODE_ETH_SYMBOL", "ETHUSDT").upper(),),
            top_n=0,
            exclude_bases=(),
        ),
        ModeCfg(
            name="alts",
            enabled=_env_bool("MODE_ALTS_ENABLED", True),
            min_wall_usd=_env_float("MODE_ALTS_MIN_WALL_USD", 150_000.0),
            symbols=(),
            top_n=_env_int("MODE_ALTS_TOP_N", 48),
            exclude_bases=("BTC", "ETH"),
        ),
    )

    quote_assets = tuple(
        s.strip().upper()
        for s in _env_str("QUOTE_ASSETS", "USDT").split(",")
        if s.strip()
    )

    detector = DetectorCfg(
        max_distance_pct=_env_float("MAX_DISTANCE_PCT", 3.0),
        min_distance_pct=_env_float("MIN_DISTANCE_PCT", 0.05),
        min_lifetime_sec=_env_float("MIN_LIFETIME_SEC", 60.0),
        relative_size_multiplier=_env_float("RELATIVE_SIZE_MULTIPLIER", 3.0),
        neighbour_levels=_env_int("NEIGHBOUR_LEVELS", 20),
        zone_aggregation_pct=_env_float("ZONE_AGGREGATION_PCT", 0.10),
        cold_start_grace_sec=_env_float("COLD_START_GRACE_SEC", 120.0),
        execution_window_sec=_env_float("EXECUTION_WINDOW_SEC", 5.0),
    )

    orderbook = OrderbookCfg(
        snapshot_limit=_env_int("SNAPSHOT_LIMIT", 1000),
        scan_interval_sec=_env_float("SCAN_INTERVAL_SEC", 1.0),
    )

    cooldown = CooldownCfg(
        fingerprint_ttl_sec=_env_float("COOLDOWN_TTL_SEC", 1800.0),
    )

    telegram = TelegramCfg(
        enabled=_env_bool("TG_ENABLED", True),
        bot_token=os.environ.get("TG_BOT_TOKEN") or None,
        chat_id=os.environ.get("TG_CHAT_ID") or None,
        topic_low=_env_opt_int("TG_TOPIC_LOW"),
        topic_mid=_env_opt_int("TG_TOPIC_MID"),
        topic_high=_env_opt_int("TG_TOPIC_HIGH"),
        tier_low_usd=_env_float("TG_TIER_LOW_USD", 150_000.0),
        tier_mid_usd=_env_float("TG_TIER_MID_USD", 500_000.0),
        tier_high_usd=_env_float("TG_TIER_HIGH_USD", 2_000_000.0),
    )

    persistence = PersistenceCfg(
        walls_log_path=_env_str("WALLS_LOG_PATH", "data/walls.jsonl"),
    )

    return Settings(
        binance=binance,
        modes=modes,
        quote_assets=quote_assets,
        detector=detector,
        orderbook=orderbook,
        cooldown=cooldown,
        telegram=telegram,
        persistence=persistence,
        log_level=_env_str("LOG_LEVEL", "INFO"),
    )
