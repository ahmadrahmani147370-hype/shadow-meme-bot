"""v1.1 - Fast price lane (~1 s) for the coins where seconds matter.

Hot set (max FAST_MAX_MINTS = one DexScreener `tokens/v1` batch of 30 mints): open paper positions first, then
open watches / shadow tracks, then coins that showed a strategy setup in the last FAST_NEAR_SEC seconds
(highest score first). Every FAST_REFRESH_SEC one batch call refreshes their price + pool liquidity in the
collectors' market cache, then the signal engine's fast tick runs SL / TP / trailing / time-stop / rug exits.
All other coins stay on their normal 15-30 s cycles.

Why DexScreener: it is the cheapest free source that gives price AND pool liquidity (needed for the rug
exit) for 30 mints per call, documented at 300 requests/min; 1 call/s = 60/min goes through the shared
own `dexscreener_fast` limiter (70/min; slow lanes keep 180/min, total 250 < 300), so the slow lanes keep their share and nothing hits a 429. Jupiter's
keyless price API shares a ~30/min bucket with sell quotes here, so it cannot do 1 s.

Jupiter sub-lane: DexScreener's own price for a coin changes only every ~30 s (measured 13 Nov: p50 31 s on
16 coins >= $1M), while Jupiter price v3 changed ~6x more often. Keyless Jupiter allows 30 requests/min shared
with sell quotes, so every FAST_JUP_SEC (5 s = 12/min, through the shared `jupiter` limiter) one call prices
the open paper positions + watches (<= 50 mints) and that price is used for them (DexScreener still provides
pool liquidity for the rug exit). A Jupiter price older than 15 s falls back to DexScreener.

Measured, not assumed: `stats()` reports the achieved interval between successful refreshes per hot coin
(p50 / p95) and how often the price value really changed (DexScreener's own update cadence).
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import Counter, deque

from .collector import DS_BATCH, JUP_BATCH, JUP_PRICE, group_pairs, market_from_pairs, standard_quote
from .net import RateLimitError, fetch_json

log = logging.getLogger("shadow.fastprice")
DS_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"


class FastPricer:
    def __init__(self, settings, sources, engine, *, client, limiters, clock=time.time):
        self.s, self.src, self.eng = settings, sources, engine
        self._client, self.limiters, self.clock = client, limiters, clock
        self.counters: Counter = Counter()
        self.intervals: deque = deque(maxlen=5000)      # seconds between successful refreshes of the same coin
        self.change_intervals: deque = deque(maxlen=5000)  # seconds between real price changes of the same coin
        self.tick_ms: deque = deque(maxlen=600)
        self.last_ok: dict[str, float] = {}
        self.last_change: dict[str, tuple[float, float]] = {}
        self.hot_n = 0
        self.jup: dict[str, tuple[float, float]] = {}   # mint -> (price, ts) from Jupiter price v3
        self._jup_last = 0.0
        self._stop = asyncio.Event()

    def hot_set(self) -> list[str]:
        e, now = self.eng, self.clock()
        out: list[str] = []
        def add(m):
            if m and m not in out:
                out.append(m)
        if e.paper is not None:
            for p in e.paper.open_positions():
                add(p["mint"])
        for o in sorted(e.open.values(), key=lambda o: (o.get("shadow", False), not o.get("approved"))):
            add(o["mint"])
        near = sorted(((sc, m) for m, (sc, ts) in e.proximity.items() if now - ts <= self.s.fast_near_sec), reverse=True)
        for _, m in near:
            add(m)
        return out[: min(self.s.fast_max_mints, DS_BATCH)]

    def jup_set(self) -> list[str]:
        e = self.eng
        out = [p["mint"] for p in e.paper.open_positions()] if e.paper is not None else []
        out += [o["mint"] for o in e.open.values() if o["mint"] not in out]
        return out[:JUP_BATCH]

    async def _jup_refresh(self) -> None:
        mints = self.jup_set()
        lim = self.limiters.get("jupiter")
        if not mints or lim is None:
            return
        headers = {"x-api-key": self.s.jupiter_api_key} if getattr(self.s, "jupiter_api_key", "") else None
        try:
            data = await fetch_json(self._client, lim, JUP_PRICE, params={"ids": ",".join(mints)}, headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  (429 / network) DexScreener prices stay in use
            self.counters["jup_errors"] += 1
            return
        self.counters["jup_calls"] += 1
        if not isinstance(data, dict):
            self.counters["jup_errors"] += 1
            return
        now = self.clock()
        for m in mints:
            try:
                info = data.get(m)
                p = float(info.get("usdPrice") or 0) if isinstance(info, dict) else 0.0
            except (TypeError, ValueError):
                p = 0.0
            if p > 0:
                self.jup[m] = (p, now)

    def _apply(self, mint: str, md, now: float) -> None:
        j = self.jup.get(mint)
        if j is not None and now - j[1] <= 15:
            md.price_usd = j[0]                  # fresher on-chain price for positions / watches
            self.counters["jup_used"] += 1
        owner = self.src.owner(mint)
        t = owner.tracked.get(mint) if owner is not None else None
        cur = t.market if t is not None else None
        if cur is not None:
            cur.price_usd, cur.liquidity_usd = md.price_usd, md.liquidity_usd
            cur.liquidity_source = md.liquidity_source or "dexscreener"
            if md.market_cap:
                cur.market_cap = md.market_cap
            cur.updated_ts = now
        elif self.eng.paper is not None:         # a position whose coin left the tracked set
            self.eng.paper.fast_prices[mint] = (md.price_usd, md.liquidity_usd, md.liquidity_source or "dexscreener", now)
        prev = self.last_ok.get(mint)
        if prev is not None and now - prev < 60:
            self.intervals.append(now - prev)
        self.last_ok[mint] = now
        lc = self.last_change.get(mint)
        if lc is None or md.price_usd != lc[1]:
            if lc is not None and now - lc[0] < 600:
                self.change_intervals.append(now - lc[0])
            self.last_change[mint] = (now, md.price_usd)

    async def tick(self) -> int:
        mints = self.hot_set()
        self.hot_n = len(mints)
        if not mints:
            return 0
        t0, now = time.monotonic(), self.clock()
        if self.s.fast_jup_sec > 0 and now - self._jup_last >= self.s.fast_jup_sec:
            self._jup_last = now
            await self._jup_refresh()
        try:
            pairs = await fetch_json(self._client, self.limiters.get("dexscreener_fast") or self.limiters["dexscreener"], DS_TOKENS + ",".join(mints))
        except asyncio.CancelledError:
            raise
        except RateLimitError:
            self.counters["rate_limited"] += 1
            return 0
        except Exception as e:  # noqa: BLE001  the fast lane must never break the bot
            self.counters["errors"] += 1
            if self.counters["errors"] % 50 == 1:
                log.warning("fast price fetch failed: %s: %s", type(e).__name__, str(e)[:100])
            return 0
        self.counters["calls"] += 1
        n = 0
        now = self.clock()
        for m, ps in group_pairs(pairs or [], mints).items():
            good = [x for x in ps if standard_quote(x)]
            if not good:
                continue
            md = market_from_pairs(m, good, now)
            if md.price_usd:
                self._apply(m, md, now)
                n += 1
        self.counters["prices"] += n
        await self.eng.fast_tick()
        self.tick_ms.append((time.monotonic() - t0) * 1000)
        return n

    async def run(self) -> None:
        if self.s.fast_refresh_sec <= 0:
            return
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.counters["errors"] += 1
                log.warning("fast tick failed: %s: %s", type(e).__name__, e)
            delay = max(0.05, self.s.fast_refresh_sec - (time.monotonic() - t0))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _q(xs, q):
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1)))], 2) if xs else None

    def stats(self) -> dict:
        iv, ch = list(self.intervals), list(self.change_intervals)
        return {"hot": self.hot_n, "calls": self.counters["calls"], "errors": self.counters["errors"],
                "rate_limited": self.counters["rate_limited"],
                "refresh_s_p50": self._q(iv, .5), "refresh_s_p95": self._q(iv, .95),
                "refresh_s_mean": round(statistics.fmean(iv), 2) if iv else None, "refresh_n": len(iv),
                "price_change_s_p50": self._q(ch, .5), "price_change_n": len(ch),
                "tick_ms_p50": self._q(list(self.tick_ms), .5), "jup_calls": self.counters["jup_calls"],
                "jup_errors": self.counters["jup_errors"]}


__all__ = ["FastPricer"]
