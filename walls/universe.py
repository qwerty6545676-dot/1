"""Universe selection driven by enabled trading modes.

Each :class:`ModeCfg` contributes some symbols to the watched universe:

- Explicit-symbol modes (BTC, ETH) contribute the symbols listed in
  ``mode.symbols`` if those symbols exist as trading-enabled spot pairs.
- Top-N modes (Alts) contribute the top-``mode.top_n`` symbols by 24h quote
  volume, excluding base assets in ``mode.exclude_bases``.

Symbols are unique across modes — a symbol claimed by an explicit mode
won't be re-picked by a top-N mode.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .settings import ModeCfg


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    quote_volume_24h: float
    last_price: float

    @property
    def volume_24h_usd(self) -> float:
        # All currently-allowed quotes are USD-pegged stablecoins, so
        # quote_volume is already in USD terms.
        return self.quote_volume_24h


class _RestProto(Protocol):
    async def exchange_info(self) -> dict[str, Any]: ...
    async def ticker_24hr(self) -> list[dict[str, Any]]: ...


def _parse_spot_symbols(
    info: dict[str, Any], quote_assets: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """Returns ``{symbol: (base, quote)}`` for trading-enabled spot pairs."""
    quote_set = {q.upper() for q in quote_assets}
    out: dict[str, tuple[str, str]] = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if not s.get("isSpotTradingAllowed", False):
            continue
        quote = str(s.get("quoteAsset", "")).upper()
        if quote not in quote_set:
            continue
        symbol = str(s.get("symbol", "")).upper()
        base = str(s.get("baseAsset", "")).upper()
        out[symbol] = (base, quote)
    return out


def _build_symbol_info(
    sym: str,
    spot: dict[str, tuple[str, str]],
    ticker_by_sym: dict[str, dict[str, Any]],
) -> SymbolInfo | None:
    if sym not in spot or sym not in ticker_by_sym:
        return None
    t = ticker_by_sym[sym]
    try:
        qv = float(t.get("quoteVolume", "0") or 0.0)
        lp = float(t.get("lastPrice", "0") or 0.0)
    except (TypeError, ValueError):
        return None
    if qv <= 0 or lp <= 0:
        return None
    base, quote = spot[sym]
    return SymbolInfo(
        symbol=sym, base=base, quote=quote, quote_volume_24h=qv, last_price=lp,
    )


async def select_for_modes(
    rest: _RestProto,
    *,
    modes: Sequence[ModeCfg],
    quote_assets: Sequence[str],
) -> list[tuple[SymbolInfo, ModeCfg]]:
    """Build the watched universe as ``[(symbol_info, mode), ...]`` pairs."""
    info = await rest.exchange_info()
    tickers = await rest.ticker_24hr()
    ticker_by_sym = {str(t.get("symbol", "")).upper(): t for t in tickers}
    spot = _parse_spot_symbols(info, quote_assets)

    result: list[tuple[SymbolInfo, ModeCfg]] = []
    used: set[str] = set()

    # Explicit-symbol modes go first so their picks can't be stolen by top-N.
    for mode in modes:
        if not mode.enabled or not mode.symbols:
            continue
        for sym in mode.symbols:
            if sym in used:
                continue
            si = _build_symbol_info(sym, spot, ticker_by_sym)
            if si is None:
                continue
            result.append((si, mode))
            used.add(sym)

    # Top-N modes scan remaining symbols by descending volume.
    for mode in modes:
        if not mode.enabled or mode.top_n <= 0:
            continue
        exclude = {b.upper() for b in mode.exclude_bases}
        ranked = sorted(
            spot.keys(),
            key=lambda s: float(ticker_by_sym.get(s, {}).get("quoteVolume", 0) or 0),
            reverse=True,
        )
        picked = 0
        for sym in ranked:
            if picked >= mode.top_n:
                break
            if sym in used:
                continue
            base = spot[sym][0]
            if base in exclude:
                continue
            si = _build_symbol_info(sym, spot, ticker_by_sym)
            if si is None:
                continue
            result.append((si, mode))
            used.add(sym)
            picked += 1

    return result
