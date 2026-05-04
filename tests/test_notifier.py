"""Tests for Telegram message formatting and tier routing."""

from __future__ import annotations

from walls.notifier import TelegramNotifier, format_message
from walls.settings import TelegramCfg
from walls.state import StateEvent, TrackedWall, WallState


def _cfg() -> TelegramCfg:
    return TelegramCfg(
        enabled=True,
        bot_token=None,
        chat_id=None,
        topic_low=None,
        topic_mid=None,
        topic_high=None,
        tier_low_usd=150_000.0,
        tier_mid_usd=500_000.0,
        tier_high_usd=2_000_000.0,
    )


def _wall(usd: float = 1_000_000.0, side: str = "bid") -> TrackedWall:
    return TrackedWall(
        fingerprint="X",
        symbol="BTCUSDT",
        side=side,
        price=99_000.0,
        qty=usd / 99_000.0,
        usd_value=usd,
        state=WallState.ACTIVE,
        first_seen_ts_ms=0,
        last_seen_ts_ms=10_000,
        distance_pct=1.0,
        mid_price=100_000.0,
    )


def test_format_message_appeared_includes_key_facts() -> None:
    msg = format_message(StateEvent(kind="appeared", wall=_wall(usd=1_500_000.0)))
    assert "BTCUSDT" in msg
    assert "BUY wall" in msg
    assert "$1.50M" in msg
    assert "1.00%" in msg


def test_format_message_executed_marks_resistance_or_support() -> None:
    msg_bid = format_message(StateEvent(kind="executed", wall=_wall(side="bid")))
    assert "support broken" in msg_bid
    msg_ask = format_message(StateEvent(kind="executed", wall=_wall(side="ask")))
    assert "resistance broken" in msg_ask


def test_route_picks_high_tier_for_large() -> None:
    n = TelegramNotifier(_cfg())
    tier = n._route(StateEvent(kind="appeared", wall=_wall(usd=3_000_000.0)))
    assert tier is not None and tier.name == "high"


def test_route_returns_none_for_below_threshold() -> None:
    n = TelegramNotifier(_cfg())
    assert n._route(StateEvent(kind="appeared", wall=_wall(usd=50_000.0))) is None


def test_route_promotes_executed_cancelled_to_high() -> None:
    n = TelegramNotifier(_cfg())
    tier = n._route(StateEvent(kind="executed", wall=_wall(usd=600_000.0)))
    assert tier is not None and tier.name == "high"
