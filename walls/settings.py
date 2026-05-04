"""Typed configuration loaded from YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class BinanceCfg:
    rest_base: str
    ws_base: str
    ws_streams_per_connection: int
    ws_reconnect_delay_sec: float
    rest_request_timeout_sec: float


@dataclass(frozen=True)
class UniverseCfg:
    top_n: int
    quote_assets: tuple[str, ...]
    refresh_minutes: int


@dataclass(frozen=True)
class OrderbookCfg:
    snapshot_limit: int
    scan_interval_sec: float


@dataclass(frozen=True)
class SizeTier:
    min_24h_volume_usd: float
    min_wall_usd: float


@dataclass(frozen=True)
class DetectorCfg:
    size_tiers: tuple[SizeTier, ...]
    max_distance_pct: float
    min_distance_pct: float
    min_lifetime_sec: float
    relative_size_multiplier: float
    neighbour_levels: int
    zone_aggregation_pct: float
    cold_start_grace_sec: float
    execution_window_sec: float

    def min_wall_usd_for(self, volume_24h_usd: float) -> float:
        for tier in self.size_tiers:  # tiers are sorted high-to-low at load time
            if volume_24h_usd >= tier.min_24h_volume_usd:
                return tier.min_wall_usd
        return float("inf")


@dataclass(frozen=True)
class CooldownCfg:
    fingerprint_ttl_sec: float


@dataclass(frozen=True)
class TelegramCfg:
    enabled: bool
    token_env: str
    chat_id_env: str
    topic_low_env: str
    topic_mid_env: str
    topic_high_env: str
    tier_low_usd: float
    tier_mid_usd: float
    tier_high_usd: float

    def token(self) -> str | None:
        v = os.environ.get(self.token_env)
        return v if v else None

    def chat_id(self) -> str | None:
        v = os.environ.get(self.chat_id_env)
        return v if v else None

    def topic_for(self, env_name: str) -> int | None:
        v = os.environ.get(env_name)
        if v:
            try:
                return int(v)
            except ValueError:
                return None
        return None


@dataclass(frozen=True)
class PersistenceCfg:
    walls_log_path: str


@dataclass(frozen=True)
class Settings:
    binance: BinanceCfg
    universe: UniverseCfg
    orderbook: OrderbookCfg
    detector: DetectorCfg
    cooldown: CooldownCfg
    telegram: TelegramCfg
    persistence: PersistenceCfg
    log_level: str


def load(path: str | os.PathLike[str]) -> Settings:
    with Path(path).open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    b = raw["binance"]
    u = raw["universe"]
    ob = raw["orderbook"]
    d = raw["detector"]
    cd = raw["cooldown"]
    tg = raw["telegram"]
    pst = raw["persistence"]

    tiers_raw = list(d["size_tiers"])
    # sort tiers by min_24h_volume_usd descending so first-match-wins works correctly
    tiers_raw.sort(key=lambda t: float(t["min_24h_volume_usd"]), reverse=True)
    tiers = tuple(
        SizeTier(
            min_24h_volume_usd=float(t["min_24h_volume_usd"]),
            min_wall_usd=float(t["min_wall_usd"]),
        )
        for t in tiers_raw
    )

    return Settings(
        binance=BinanceCfg(
            rest_base=str(b["rest_base"]).rstrip("/"),
            ws_base=str(b["ws_base"]).rstrip("/"),
            ws_streams_per_connection=int(b["ws_streams_per_connection"]),
            ws_reconnect_delay_sec=float(b["ws_reconnect_delay_sec"]),
            rest_request_timeout_sec=float(b["rest_request_timeout_sec"]),
        ),
        universe=UniverseCfg(
            top_n=int(u["top_n"]),
            quote_assets=tuple(str(q).upper() for q in u["quote_assets"]),
            refresh_minutes=int(u["refresh_minutes"]),
        ),
        orderbook=OrderbookCfg(
            snapshot_limit=int(ob["snapshot_limit"]),
            scan_interval_sec=float(ob["scan_interval_sec"]),
        ),
        detector=DetectorCfg(
            size_tiers=tiers,
            max_distance_pct=float(d["max_distance_pct"]),
            min_distance_pct=float(d["min_distance_pct"]),
            min_lifetime_sec=float(d["min_lifetime_sec"]),
            relative_size_multiplier=float(d["relative_size_multiplier"]),
            neighbour_levels=int(d["neighbour_levels"]),
            zone_aggregation_pct=float(d["zone_aggregation_pct"]),
            cold_start_grace_sec=float(d["cold_start_grace_sec"]),
            execution_window_sec=float(d["execution_window_sec"]),
        ),
        cooldown=CooldownCfg(
            fingerprint_ttl_sec=float(cd["fingerprint_ttl_sec"]),
        ),
        telegram=TelegramCfg(
            enabled=bool(tg["enabled"]),
            token_env=str(tg["token_env"]),
            chat_id_env=str(tg["chat_id_env"]),
            topic_low_env=str(tg["topic_low_env"]),
            topic_mid_env=str(tg["topic_mid_env"]),
            topic_high_env=str(tg["topic_high_env"]),
            tier_low_usd=float(tg["tier_low_usd"]),
            tier_mid_usd=float(tg["tier_mid_usd"]),
            tier_high_usd=float(tg["tier_high_usd"]),
        ),
        persistence=PersistenceCfg(
            walls_log_path=str(pst["walls_log_path"]),
        ),
        log_level=str(raw.get("logging", {}).get("level", "INFO")),
    )
