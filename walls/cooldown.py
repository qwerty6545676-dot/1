"""Per-fingerprint cooldown to prevent the same wall re-alerting too often."""

from __future__ import annotations


class Cooldown:
    """Suppresses repeats of the same fingerprint within ``ttl_sec``."""

    __slots__ = ("_last", "ttl_ms")

    def __init__(self, ttl_sec: float) -> None:
        self.ttl_ms = int(ttl_sec * 1000)
        self._last: dict[str, int] = {}

    def allow(self, fingerprint: str, now_ms: int) -> bool:
        prev = self._last.get(fingerprint)
        if prev is None or now_ms - prev >= self.ttl_ms:
            self._last[fingerprint] = now_ms
            return True
        return False

    def gc(self, now_ms: int) -> None:
        """Drop entries older than 2× TTL to keep memory bounded."""
        cutoff = now_ms - 2 * self.ttl_ms
        for k in [k for k, v in self._last.items() if v < cutoff]:
            self._last.pop(k, None)
