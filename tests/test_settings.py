"""Tests for env-based settings loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from walls.settings import load


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe wall-scanner env vars so tests are deterministic."""
    for k in list(os.environ.keys()):
        if (
            k.startswith("MODE_")
            or k.startswith("TG_")
            or k.startswith("BINANCE_")
            or k.startswith("ICEBERG_")
            or k.startswith("WEB_")
            or k
            in {
                "QUOTE_ASSETS",
                "MAX_DISTANCE_PCT",
                "MIN_DISTANCE_PCT",
                "MIN_LIFETIME_SEC",
                "RELATIVE_SIZE_MULTIPLIER",
                "NEIGHBOUR_LEVELS",
                "ZONE_AGGREGATION_PCT",
                "COLD_START_GRACE_SEC",
                "EXECUTION_WINDOW_SEC",
                "SNAPSHOT_LIMIT",
                "SCAN_INTERVAL_SEC",
                "COOLDOWN_TTL_SEC",
                "WALLS_LOG_PATH",
                "LOG_LEVEL",
            }
        ):
            monkeypatch.delenv(k, raising=False)


def test_defaults_with_no_env() -> None:
    """All-defaults load: 3 modes enabled with sensible thresholds."""
    s = load(env_file=None)
    enabled = s.enabled_modes()
    assert {m.name for m in enabled} == {"btc", "eth", "alts"}

    btc = next(m for m in enabled if m.name == "btc")
    eth = next(m for m in enabled if m.name == "eth")
    alts = next(m for m in enabled if m.name == "alts")
    assert btc.symbols == ("BTCUSDT",)
    assert btc.min_wall_usd == 1_000_000
    assert eth.symbols == ("ETHUSDT",)
    assert eth.min_wall_usd == 500_000
    assert alts.top_n == 48
    assert alts.exclude_bases == ("BTC", "ETH")
    assert alts.min_wall_usd == 150_000

    assert s.detector.min_lifetime_sec == 60.0
    assert s.detector.relative_size_multiplier == 3.0
    assert s.binance.ws_base.startswith("wss://")
    assert "USDT" in s.quote_assets


def test_loads_example_env(tmp_path: Path) -> None:
    """The committed .env.example template loads without errors."""
    src = Path(__file__).resolve().parent.parent / ".env.example"
    s = load(src)
    enabled = {m.name for m in s.enabled_modes()}
    assert {"btc", "eth", "alts"} <= enabled


def test_can_disable_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """User can turn off any combination of modes."""
    monkeypatch.setenv("MODE_BTC_ENABLED", "false")
    monkeypatch.setenv("MODE_ETH_ENABLED", "false")
    monkeypatch.setenv("MODE_ALTS_ENABLED", "true")
    s = load(env_file=None)
    enabled = s.enabled_modes()
    assert len(enabled) == 1
    assert enabled[0].name == "alts"


def test_per_mode_thresholds_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom USD thresholds via env are respected."""
    monkeypatch.setenv("MODE_BTC_MIN_WALL_USD", "750000")
    monkeypatch.setenv("MODE_ETH_MIN_WALL_USD", "300000")
    monkeypatch.setenv("MODE_ALTS_MIN_WALL_USD", "75000")
    monkeypatch.setenv("MODE_ALTS_TOP_N", "30")
    s = load(env_file=None)
    btc = next(m for m in s.enabled_modes() if m.name == "btc")
    eth = next(m for m in s.enabled_modes() if m.name == "eth")
    alts = next(m for m in s.enabled_modes() if m.name == "alts")
    assert btc.min_wall_usd == 750_000
    assert eth.min_wall_usd == 300_000
    assert alts.min_wall_usd == 75_000
    assert alts.top_n == 30


def test_filter_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detector and cooldown values are configurable from env."""
    monkeypatch.setenv("MIN_LIFETIME_SEC", "30")
    monkeypatch.setenv("RELATIVE_SIZE_MULTIPLIER", "5.0")
    monkeypatch.setenv("MAX_DISTANCE_PCT", "1.5")
    monkeypatch.setenv("COOLDOWN_TTL_SEC", "600")
    s = load(env_file=None)
    assert s.detector.min_lifetime_sec == 30.0
    assert s.detector.relative_size_multiplier == 5.0
    assert s.detector.max_distance_pct == 1.5
    assert s.cooldown.fingerprint_ttl_sec == 600.0


def test_telegram_optional_topics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Topic IDs are optional — defaults to None for plain DMs."""
    monkeypatch.setenv("TG_BOT_TOKEN", "12345:abc")
    monkeypatch.setenv("TG_CHAT_ID", "987654321")
    s = load(env_file=None)
    assert s.telegram.bot_token == "12345:abc"
    assert s.telegram.chat_id == "987654321"
    assert s.telegram.topic_low is None
    assert s.telegram.topic_mid is None
    assert s.telegram.topic_high is None


def test_iceberg_defaults() -> None:
    """Iceberg detector enabled by default with sensible defaults."""
    s = load(env_file=None)
    ic = s.iceberg
    assert ic.enabled is True
    assert ic.min_visible_usd == 25_000.0
    assert ic.eat_threshold_ratio == 0.30
    assert ic.regen_window_sec == 10.0
    assert ic.min_regens == 4
    assert ic.cooldown_ttl_sec == 1800.0
    # v2: anti-spoof gate is on by default.
    assert ic.require_trade_confirmation is True
    assert ic.trade_window_ms == 2000
    assert ic.trade_min_qty_ratio == 0.30


def test_iceberg_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICEBERG_ENABLED", "false")
    monkeypatch.setenv("ICEBERG_MIN_REGENS", "8")
    s = load(env_file=None)
    assert s.iceberg.enabled is False
    assert s.iceberg.min_regens == 8


def test_iceberg_anti_spoof_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trade confirmation is opt-out for users who can't afford another WS conn."""
    monkeypatch.setenv("ICEBERG_REQUIRE_TRADE_CONFIRMATION", "false")
    monkeypatch.setenv("ICEBERG_TRADE_WINDOW_MS", "500")
    monkeypatch.setenv("ICEBERG_TRADE_MIN_QTY_RATIO", "0.10")
    s = load(env_file=None)
    assert s.iceberg.require_trade_confirmation is False
    assert s.iceberg.trade_window_ms == 500
    assert s.iceberg.trade_min_qty_ratio == 0.10


def test_web_defaults_disabled() -> None:
    """Web dashboard is opt-in: WEB_ENABLED defaults to false."""
    s = load(env_file=None)
    assert s.web.enabled is False
    assert s.web.host == "127.0.0.1"
    assert s.web.port == 8000


def test_web_can_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_ENABLED", "true")
    monkeypatch.setenv("WEB_PORT", "9090")
    monkeypatch.setenv("WEB_LEVELS_PER_SIDE", "10")
    s = load(env_file=None)
    assert s.web.enabled is True
    assert s.web.port == 9090
    assert s.web.levels_per_side == 10
