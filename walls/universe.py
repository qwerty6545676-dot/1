"""Top-N spot symbol selection by 24h quote volume."""

from __future__ import annotations

from dataclasses import dataclass

from .binance_rest import BinanceRest


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    quote_volume_24h: float
    last_price: float

    @property
    def volume_24h_usd(self) -> float:
        # all currently-allowed quotes are USD-pegged stablecoins, so
        # quote_volume is already in USD terms
        return self.quote_volume_24h


async def select_top_n(
    rest: BinanceRest,
    *,
    top_n: int,
    quote_assets: tuple[str, ...],
) -> list[SymbolInfo]:
    info = await rest.exchange_info()
    tickers = await rest.ticker_24hr()

    quote_set = {q.upper() for q in quote_assets}

    # Build symbol -> (base, quote, status) map for spot trading symbols.
    spot: dict[str, tuple[str, str]] = {}
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
        spot[symbol] = (base, quote)

    rows: list[SymbolInfo] = []
    for t in tickers:
        sym = str(t.get("symbol", "")).upper()
        if sym not in spot:
            continue
        try:
            qv = float(t.get("quoteVolume", "0") or 0.0)
            lp = float(t.get("lastPrice", "0") or 0.0)
        except (TypeError, ValueError):
            continue
        if qv <= 0 or lp <= 0:
            continue
        base, quote = spot[sym]
        rows.append(
            SymbolInfo(
                symbol=sym,
                base=base,
                quote=quote,
                quote_volume_24h=qv,
                last_price=lp,
            )
        )

    rows.sort(key=lambda r: r.quote_volume_24h, reverse=True)
    return rows[:top_n]
