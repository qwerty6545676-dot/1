"""Wall detection algorithm.

Given an :class:`OrderBook`, find price levels that look like genuine "walls" —
large limit orders that may be acting as support/resistance.

The output of :func:`scan` is a list of :class:`Candidate`. Whether a candidate
becomes an actual ``ACTIVE`` wall (and thus an alert) is decided by the state
machine in :mod:`walls.state` — based on persistence, fingerprint cooldown, etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .orderbook import OrderBook
from .settings import DetectorCfg


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: str  # "bid" or "ask"
    price: float
    qty: float
    usd_value: float
    distance_pct: float  # signed % from mid (negative for bids, positive for asks)
    mid_price: float


def _distance_pct(price: float, mid: float, side: str) -> float:
    if side == "bid":
        return (mid - price) / mid * 100.0  # positive number = how far below mid
    return (price - mid) / mid * 100.0


def _scan_side(
    symbol: str,
    book: OrderBook,
    side: str,
    cfg: DetectorCfg,
    min_wall_usd: float,
) -> list[Candidate]:
    levels = book.bids if side == "bid" else book.asks
    if not levels:
        return []
    mid = book.mid()
    if mid is None:
        return []

    # Sort levels by distance from mid (closest first).
    sorted_levels = sorted(levels.items(), key=lambda kv: -kv[0] if side == "bid" else kv[0])
    if len(sorted_levels) < cfg.neighbour_levels + 1:
        return []

    # Pre-compute USD values for the top N+window levels we care about.
    # We only need to look up to ~max_distance_pct away.
    relevant: list[tuple[float, float, float]] = []  # (price, qty, usd)
    for price, qty in sorted_levels:
        d = _distance_pct(price, mid, side)
        if d > cfg.max_distance_pct:
            break
        relevant.append((price, qty, price * qty))

    if not relevant:
        return []

    out: list[Candidate] = []
    for i, (price, qty, usd) in enumerate(relevant):
        d = _distance_pct(price, mid, side)
        if d < cfg.min_distance_pct:
            continue
        if usd < min_wall_usd:
            continue

        # Relative-size check: wall must dwarf its neighbours.
        nb = cfg.neighbour_levels
        lo = max(0, i - nb)
        hi = min(len(relevant), i + nb + 1)
        neighbour_usds = [relevant[j][2] for j in range(lo, hi) if j != i]
        if neighbour_usds:
            med = median(neighbour_usds)
            if med > 0 and usd < med * cfg.relative_size_multiplier:
                continue

        out.append(
            Candidate(
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                usd_value=usd,
                distance_pct=d,
                mid_price=mid,
            )
        )
    return out


def scan(
    symbol: str,
    book: OrderBook,
    cfg: DetectorCfg,
    min_wall_usd: float,
) -> list[Candidate]:
    """Find candidate walls in the current snapshot of the book."""
    if not book.synced:
        return []
    return _scan_side(symbol, book, "bid", cfg, min_wall_usd) + _scan_side(
        symbol, book, "ask", cfg, min_wall_usd
    )


def aggregate_zones(candidates: list[Candidate], zone_pct: float) -> list[Candidate]:
    """Merge candidates that are within ``zone_pct`` % of each other on the same side.

    The merged candidate keeps the price of the *largest* member and sums the
    USD value/qty of the others. Distance is recomputed from the merged price.
    """
    if not candidates:
        return []

    # Group by (symbol, side)
    by_key: dict[tuple[str, str], list[Candidate]] = {}
    for c in candidates:
        by_key.setdefault((c.symbol, c.side), []).append(c)

    merged: list[Candidate] = []
    for (sym, side), group in by_key.items():
        # Sort by distance from mid (closest first for both sides — distance_pct
        # is the absolute distance, so just sort ascending).
        group = sorted(group, key=lambda c: c.distance_pct)
        used = [False] * len(group)
        for i, base in enumerate(group):
            if used[i]:
                continue
            members = [base]
            used[i] = True
            for j in range(i + 1, len(group)):
                if used[j]:
                    continue
                other = group[j]
                # Walls at similar prices belong to one zone.
                rel = abs(other.price - base.price) / base.price * 100.0
                if rel <= zone_pct:
                    members.append(other)
                    used[j] = True
            if len(members) == 1:
                merged.append(base)
                continue
            biggest = max(members, key=lambda m: m.usd_value)
            total_qty = sum(m.qty for m in members)
            total_usd = sum(m.usd_value for m in members)
            d = _distance_pct(biggest.price, biggest.mid_price, side)
            merged.append(
                Candidate(
                    symbol=sym,
                    side=side,
                    price=biggest.price,
                    qty=total_qty,
                    usd_value=total_usd,
                    distance_pct=d,
                    mid_price=biggest.mid_price,
                )
            )
    return merged
