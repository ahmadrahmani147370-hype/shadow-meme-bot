"""Shared HTTP helpers: per-source async rate-limit queues and polite JSON fetching.

Every free API has its own budget (see docs/DATA_SOURCES.md). A RateLimiter is a FIFO queue:
callers `await limiter.acquire()` and are released one by one, never faster than the budget.
A 429 pushes the whole queue back (Retry-After if given, else exponential).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("shadow.net")
UA = "ShadowMeme/0.2 (+https://github.com/ahmadrahmani147370-hype/shadow-meme-bot)"


class RateLimitError(Exception):
    pass


@dataclass
class RateLimiter:
    name: str
    per_minute: float
    clock: callable = time.monotonic
    sleep: callable = asyncio.sleep
    _next_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _penalty: float = 0.0
    calls: int = 0          # requests released by this queue
    hits_429: int = 0

    @property
    def interval(self) -> float:
        return 60.0 / self.per_minute

    async def acquire(self) -> None:
        async with self._lock:  # FIFO: asyncio.Lock wakes waiters in order
            now = self.clock()
            wait = self._next_at - now
            if wait > 0:
                await self.sleep(wait)
                now = self.clock()
            self._next_at = max(now, self._next_at) + self.interval
            self.calls += 1

    def penalize(self, retry_after: float | None = None) -> float:
        """Called on HTTP 429: delay everybody in this queue."""
        self.hits_429 += 1
        self._penalty = min(max(self._penalty * 2, self.interval), 300.0)
        delay = retry_after if retry_after else self._penalty
        self._next_at = max(self._next_at, self.clock() + delay)
        log.warning("%s rate limited, pausing %.0fs", self.name, delay)
        return delay

    def success(self) -> None:
        self._penalty = 0.0


# Budgets kept safely under the documented keyless limits.
DEFAULT_BUDGETS = {
    "geckoterminal": 6,      # docs ~10/min
    "rugcheck": 30,          # docs ~1-3 req/s
    "dexscreener": 240,      # tokens/pairs 300/min
    "dexscreener_profiles": 50,
    "jupiter": 25,           # keyless 30/min
    "solana_rpc": 60,
}


def make_limiters(budgets: dict | None = None) -> dict[str, RateLimiter]:
    return {k: RateLimiter(k, v) for k, v in (budgets or DEFAULT_BUDGETS).items()}


def limiter_stats(limiters: dict) -> dict:
    return {k: {"calls": v.calls, "429": v.hits_429} for k, v in limiters.items() if v.calls or v.hits_429}


async def fetch_json(client, limiter: RateLimiter, url: str, *, params=None, headers=None):
    """GET url through the limiter. Returns parsed JSON or raises (RateLimitError on 429)."""
    await limiter.acquire()
    r = await client.get(url, params=params, headers=headers)
    if r.status_code == 429:
        ra = r.headers.get("retry-after")
        limiter.penalize(float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        raise RateLimitError(f"{limiter.name} 429")
    r.raise_for_status()
    limiter.success()
    return r.json()
