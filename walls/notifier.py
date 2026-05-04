"""Telegram notifier for wall events.

Free-form HTTP POST to the bot API — no third-party SDK required. Each event
is routed to one of three forum topics by USD size (low / mid / high).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

from . import log
from .settings import TelegramCfg
from .state import StateEvent, TrackedWall

_log = log.get("tg")


def _fmt_usd(usd: float) -> str:
    if usd >= 1_000_000:
        return f"${usd / 1_000_000:.2f}M"
    if usd >= 1_000:
        return f"${usd / 1_000:.0f}k"
    return f"${usd:.0f}"


def _fmt_qty(qty: float) -> str:
    if qty >= 1000:
        return f"{qty:,.0f}"
    if qty >= 1:
        return f"{qty:,.2f}"
    return f"{qty:.6f}"


def _emoji_for(event_kind: str, side: str) -> str:
    if event_kind == "appeared":
        return "🟩" if side == "bid" else "🟥"
    if event_kind == "cancelled":
        return "⬜️"
    if event_kind == "executed":
        return "💥"
    return "•"


def format_message(evt: StateEvent) -> str:
    w: TrackedWall = evt.wall
    side_word = "BUY" if w.side == "bid" else "SELL"
    emoji = _emoji_for(evt.kind, w.side)

    if evt.kind == "appeared":
        title = f"{emoji} <b>{w.symbol}</b> — {side_word} wall"
        line2 = (
            f"Price: <code>{w.price:g}</code>  "
            f"({w.distance_pct:.2f}% from mid <code>{w.mid_price:g}</code>)"
        )
        line3 = f"Size: <b>{_fmt_usd(w.usd_value)}</b> ({_fmt_qty(w.qty)} {w.symbol[:-4] if w.symbol.endswith('USDT') else ''})"
        return f"{title}\n{line2}\n{line3}"

    if evt.kind == "cancelled":
        title = f"{emoji} <b>{w.symbol}</b> — {side_word} wall <i>cancelled</i>"
        return (
            f"{title}\n"
            f"Price: <code>{w.price:g}</code> "
            f"(was {_fmt_usd(w.usd_value)})\n"
            f"Wall removed without execution — possible "
            f"{'support gone' if w.side == 'bid' else 'resistance lifted'}."
        )

    if evt.kind == "executed":
        title = f"{emoji} <b>{w.symbol}</b> — {side_word} wall <i>executed</i>"
        return (
            f"{title}\n"
            f"Price: <code>{w.price:g}</code> "
            f"({_fmt_usd(w.usd_value)})\n"
            f"Wall hit by aggressive flow — "
            f"{'support broken' if w.side == 'bid' else 'resistance broken'}."
        )

    return f"{emoji} {w.symbol} {side_word} {evt.kind} {_fmt_usd(w.usd_value)} @ {w.price}"


@dataclass
class _Tier:
    topic_id: int | None
    threshold_usd: float
    name: str


class TelegramNotifier:
    def __init__(self, cfg: TelegramCfg) -> None:
        self.cfg = cfg
        self._token = cfg.token()
        self._chat_id = cfg.chat_id()
        self._tiers = [
            _Tier(cfg.topic_for(cfg.topic_high_env), cfg.tier_high_usd, "high"),
            _Tier(cfg.topic_for(cfg.topic_mid_env), cfg.tier_mid_usd, "mid"),
            _Tier(cfg.topic_for(cfg.topic_low_env), cfg.tier_low_usd, "low"),
        ]
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled and bool(self._token) and bool(self._chat_id)

    async def __aenter__(self) -> TelegramNotifier:
        if self.enabled:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _route(self, evt: StateEvent) -> _Tier | None:
        usd = evt.wall.usd_value
        # Promote executed/cancelled of large walls to high tier regardless of size,
        # since these are the highest-signal events.
        if evt.kind in ("executed", "cancelled") and usd >= self.cfg.tier_mid_usd:
            return self._tiers[0]
        for tier in self._tiers:  # high → mid → low
            if usd >= tier.threshold_usd:
                return tier
        return None  # below low threshold — don't send

    async def send(self, evt: StateEvent) -> None:
        if not self.enabled or self._session is None:
            return
        tier = self._route(evt)
        if tier is None:
            return
        text = format_message(evt)
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if tier.topic_id is not None:
            payload["message_thread_id"] = tier.topic_id

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    _log.warning("telegram send %s: %s", resp.status, body[:300])
        except (TimeoutError, aiohttp.ClientError) as e:
            _log.warning("telegram send error: %s", e)
