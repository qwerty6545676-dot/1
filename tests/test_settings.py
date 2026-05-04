"""Tests for YAML settings loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from walls.settings import load


def test_loads_example(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parent.parent / "settings.example.yaml"
    dst = tmp_path / "settings.yaml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    s = load(dst)
    assert s.universe.top_n == 50
    assert "USDT" in s.universe.quote_assets
    assert s.binance.ws_base.startswith("wss://")
    assert s.detector.min_lifetime_sec >= 60.0
    # Tier ordering: highest volume first
    tiers = s.detector.size_tiers
    assert tiers[0].min_24h_volume_usd >= tiers[-1].min_24h_volume_usd


@pytest.mark.parametrize(
    "vol,expected_min_wall",
    [
        (10_000_000_000, 1_000_000),  # >= $5B/day → $1M wall
        (1_000_000_000, 500_000),     # $500M – $5B → $500k wall
        (100_000_000, 150_000),       # below $500M → $150k wall
    ],
)
def test_tier_lookup(vol: float, expected_min_wall: float, tmp_path: Path) -> None:
    src = Path(__file__).resolve().parent.parent / "settings.example.yaml"
    s = load(src)
    assert s.detector.min_wall_usd_for(vol) == expected_min_wall
