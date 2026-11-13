"""PHASE 3 - Data collection.

Takes TokenEvents from the scanner, enriches them with DexScreener `tokens/v1` (30 mints per call),
filters by real age, tracks a capped set of active tokens with periodic price snapshots and builds
1-minute candles locally. GeckoTerminal OHLCV is fetched only for a small "hot" set, and Jupiter
price v3 fills in liquidity (DexScreener has none for pump.fun bonding pairs) and acts as a price
fallback. Every request goes through the shared per-source rate-limit queues in shadow.net.

Interface for phases 4/5:
    collector.get_market(mint)                 -> MarketData | None
    collector.get_candles(mint, minutes=60)    -> list[Candle] (local; source="geckoterminal" for hot set)
    collector.candles_df(mint)                 -> pandas DataFrame
    collector.tracked_mints()                  -> list[str]
    collector.updates                          -> asyncio.Queue[MarketData] (new enrichments + refreshes)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace

from .candles import CandleBuilder, parse_gt_ohlcv
from .config import Settings
from .db import Database
from .models import AMM, BONDING, MIGRATED, MIGRATION, NEW_TOKEN, UNKNOWN, Candle, MarketData, TokenEvent
from .net import UA, RateLimitError, fetch_json, make_limiters
from .scanner import QUOTE_MINTS

PHASE = 3
IMPLEMENTED = True
# Budget at MAX_TRACKED=200 (keyless): DexScreener 7 calls / 15 s snapshot = 28/min + enrichment <= 12/min
# (limit 300/min); Jupiter price fill-in 4 calls / 60 s (limit 30/min, shared with phase-4 quotes);
# GeckoTerminal hot set 3 pools / 90 s = 2/min (limit ~10/min, shared with the scanner's 3/min).
log = logging.getLogger("shadow.collector")

DS_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"
JUP_PRICE = "https://api.jup.ag/price/v3"
GT_OHLCV = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
DS_BATCH = 30
JUP_BATCH = 50
# DexScreener dexIds of launchpad bonding curves (everything else is an AMM pool)
# "bags" = Bags launchpad on Meteora DBC (no liquidity field, later gets a meteora damm-v2 pair; verified phase 4)
DS_BONDING = {"pumpfun", "meteoradbc", "launchlab", "raydiumlaunchlab", "moonshot", "boop", "believe", "heaven", "bags"}
DEFERRED_MAX = 1000


# ---------------------------------------------------------------- pure helpers (unit-tested)
def chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _f(x):
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def group_pairs(pairs: list, mints) -> dict[str, list]:
    """DexScreener pairs -> {mint: [pairs where the mint is the base token]}."""
    want = set(mints)
    out: dict[str, list] = {}
    for p in pairs or []:
        b = ((p.get("baseToken") or {}).get("address")) or ""
        if b in want and p.get("chainId", "solana") == "solana":
            out.setdefault(b, []).append(p)
    return out


def standard_quote(p: dict) -> bool:
    return ((p.get("quoteToken") or {}).get("address")) in QUOTE_MINTS


def pick_main_pair(pairs: list) -> dict:
    """Prefer SOL/USDC/USDT quotes, then the most liquidity, then the most 24h volume."""
    return max(pairs, key=lambda p: (standard_quote(p), _f((p.get("liquidity") or {}).get("usd")) or 0.0,
                                     _f((p.get("volume") or {}).get("h24")) or 0.0))


def derive_status(pairs: list, launchpad_hint: bool = False) -> str:
    dexes = {p.get("dexId", "") for p in pairs}
    bonding, amm = dexes & DS_BONDING, dexes - DS_BONDING
    if amm and (bonding or launchpad_hint):
        return MIGRATED
    if amm:
        return AMM
    if bonding:
        return BONDING
    return UNKNOWN


def market_from_pairs(mint: str, pairs: list, now: float, launch_ts: float | None = None,
                      launchpad_hint: bool = False) -> MarketData:
    p = pick_main_pair(pairs)
    tx, vol, chg = p.get("txns") or {}, p.get("volume") or {}, p.get("priceChange") or {}
    created = [c / 1000.0 for c in (q.get("pairCreatedAt") for q in pairs) if c]
    pair_created = min(created) if created else None
    launch = min([t for t in (launch_ts, pair_created) if t], default=None)
    liq = _f((p.get("liquidity") or {}).get("usd"))
    t = lambda w, k: int(((tx.get(w) or {}).get(k)) or 0)  # noqa: E731
    return MarketData(
        mint=mint, symbol=(p.get("baseToken") or {}).get("symbol") or "", name=(p.get("baseToken") or {}).get("name") or "",
        pair_address=p.get("pairAddress") or "", dex_id=p.get("dexId") or "",
        quote_symbol=(p.get("quoteToken") or {}).get("symbol") or "",
        price_usd=_f(p.get("priceUsd")), price_native=_f(p.get("priceNative")), market_cap=_f(p.get("marketCap")),
        fdv=_f(p.get("fdv")), liquidity_usd=liq, liquidity_source="dexscreener" if liq is not None else "",
        vol_m5=_f(vol.get("m5")) or 0.0, vol_h1=_f(vol.get("h1")) or 0.0, vol_h6=_f(vol.get("h6")) or 0.0,
        vol_h24=_f(vol.get("h24")) or 0.0,
        buys_m5=t("m5", "buys"), sells_m5=t("m5", "sells"), buys_h1=t("h1", "buys"), sells_h1=t("h1", "sells"),
        buys_h6=t("h6", "buys"), sells_h6=t("h6", "sells"), buys_h24=t("h24", "buys"), sells_h24=t("h24", "sells"),
        price_change_m5=_f(chg.get("m5")), price_change_h1=_f(chg.get("h1")),
        pair_created_ts=pair_created, launch_ts=launch, migration_status=derive_status(pairs, launchpad_hint),
        n_pairs=len(pairs), source="dexscreener", updated_ts=now)


@dataclass
class Pending:
    ev: TokenEvent
    first_ts: float
    attempts: int = 0
    next_ts: float = 0.0
    enriched: bool = False   # has a pair, waiting for enough trades


@dataclass
class Tracked:
    market: MarketData
    builder: CandleBuilder
    last_active_ts: float
    last_txns: int | None = None
    misses: int = 0
    started_ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------- collector
class Collector:
    def __init__(self, settings: Settings, db: Database, *, client=None, limiters=None, clock=time.time,
                 tick: float = 1.0, on_enriched=None, on_stop=None):
        self.s, self.db = settings, db
        self._client = client
        self.limiters = limiters or make_limiters()
        self.clock, self.tick = clock, tick
        self.on_enriched, self.on_stop = on_enriched, on_stop
        self.pending: dict[str, Pending] = {}
        self.tracked: dict[str, Tracked] = {}
        self.deferred: dict[str, MarketData] = {}   # tracking_full tokens, re-checked when slots free up
        self.stats: Counter = Counter()
        self.drops: Counter = Counter()
        self.errors: deque = deque(maxlen=50)
        self.updates: asyncio.Queue[MarketData] = asyncio.Queue(maxsize=1000)
        self._stop = asyncio.Event()

    # ---- public API (phases 4/5)
    def get_market(self, mint: str) -> MarketData | None:
        t = self.tracked.get(mint)
        return t.market if t else self.db.get_market(mint)

    def get_candles(self, mint: str, minutes: int = 60, source: str = "local", include_partial: bool = True) -> list[Candle]:
        since = int((self.clock() - minutes * 60) // 60) * 60
        out = self.db.get_candles(mint, since, source)
        t = self.tracked.get(mint)
        if include_partial and source == "local" and t and t.builder.partial():
            cur = t.builder.partial()
            if not out or cur.ts > out[-1].ts:
                out.append(replace(cur))
        return out

    def candles_df(self, mint: str, minutes: int = 60, source: str = "local"):
        import pandas as pd

        rows = [c.__dict__ for c in self.get_candles(mint, minutes, source)]
        df = pd.DataFrame(rows, columns=list(Candle.__dataclass_fields__))
        if not df.empty:
            df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True)
            df = df.set_index("time")
        return df

    def tracked_mints(self) -> list[str]:
        return list(self.tracked)

    def snapshot(self) -> dict:
        return {"pending": len(self.pending), "tracked": len(self.tracked), "deferred": len(self.deferred),
                **dict(self.stats),
                "drops": dict(self.drops), "last_errors": list(self.errors)[-5:]}

    # ---- intake
    def submit(self, ev: TokenEvent) -> None:
        now = self.clock()
        self.stats["received"] += 1
        if ev.mint in self.tracked:
            if ev.kind == MIGRATION:
                self.tracked[ev.mint].market.migration_status = MIGRATED
                self.stats["migrations_tracked"] += 1
            return
        if ev.mint in self.deferred and ev.kind == MIGRATION:
            self.deferred[ev.mint].migration_status = MIGRATED
        if ev.mint in self.pending:
            if ev.kind == MIGRATION:
                self.pending[ev.mint].ev.status = MIGRATED
            return
        if ev.launch_ts and (now - ev.launch_ts) / 60 > self.s.max_token_age_min:
            self._drop(ev.mint, "too_old", f"launch {((now - ev.launch_ts) / 60):.0f} min ago ({ev.source})")
            return
        self.pending[ev.mint] = Pending(ev, now)

    def _drop(self, mint: str, reason: str, detail: str = "") -> None:
        self.pending.pop(mint, None)
        self.drops[reason] += 1
        self.db.add_drop(mint, reason, detail)

    def _err(self, text: str) -> None:
        self.stats["errors"] += 1
        self.errors.append(f"{time.strftime('%H:%M:%S')} {text}")
        log.warning(text)

    async def _ds_fetch(self, mints: list[str]) -> list | None:
        try:
            data = await fetch_json(self._client, self.limiters["dexscreener"], DS_TOKENS + ",".join(mints))
            self.stats["ds_calls"] += 1
            return data if isinstance(data, list) else []
        except asyncio.CancelledError:
            raise
        except RateLimitError:
            self._err("dexscreener 429 - queue paused")
        except Exception as e:
            self._err(f"dexscreener failed: {type(e).__name__}: {str(e)[:100]}")
        return None

    @staticmethod
    def _hint(ev: TokenEvent | None, mint: str) -> bool:
        return bool(ev and (ev.launchpad or ev.status in (BONDING, MIGRATED))) or mint.endswith("pump")

    def _emit(self, md: MarketData) -> None:
        if self.updates.full():
            self.updates.get_nowait()
        self.updates.put_nowait(md)

    # ---- enrichment
    async def enrich_once(self) -> int:
        now = self.clock()
        self.requeue_deferred(now)
        due = [m for m, p in self.pending.items() if p.next_ts <= now][:DS_BATCH]
        if not due:
            return 0
        pairs = await self._ds_fetch(due)
        if pairs is None:
            for m in due:
                if m in self.pending:
                    self.pending[m].next_ts = now + 2 * self.s.enrich_sec
            return 0
        grouped = group_pairs(pairs, due)
        done = 0
        for m in due:
            p = self.pending.get(m)
            if p is None:
                continue
            ps = grouped.get(m, [])
            good = [x for x in ps if standard_quote(x)]
            if ps and not good:
                quotes = sorted({(x.get("quoteToken") or {}).get("symbol", "?") for x in ps})
                self._drop(m, "not_meme", f"no SOL/USDC/USDT pair (quotes: {', '.join(quotes)[:80]})")
                continue
            if not good:
                p.attempts += 1
                if now - p.first_ts >= self.s.no_pair_ttl_sec:
                    self._drop(m, "no_pair", f"no DexScreener pair after {p.attempts} tries ({p.ev.source})")
                else:
                    p.next_ts = now + min(60, 10 * p.attempts)
                continue
            md = market_from_pairs(m, good, now, p.ev.launch_ts, self._hint(p.ev, m))
            md.symbol, md.name = md.symbol or p.ev.symbol, md.name or p.ev.name
            if p.ev.status == MIGRATED and md.migration_status in (AMM, BONDING, UNKNOWN):
                md.migration_status = MIGRATED if md.migration_status == AMM else md.migration_status
            age = md.age_min(now)
            if age is not None and age > self.s.max_token_age_min:
                self._drop(m, "too_old", f"real age {age:.0f} min (oldest pair)")
                continue
            if not p.enriched:
                p.enriched = True
                self.stats["enriched"] += 1
                done += 1
            if md.txns_h24 < self.s.min_track_txns:
                p.attempts += 1
                if now - p.first_ts >= self.s.no_pair_ttl_sec:
                    self._drop(m, "inactive", f"only {md.txns_h24} trades after {(now - p.first_ts):.0f}s")
                    self.db.upsert_market(md, tracking=False)
                else:
                    self.stats["quiet_rechecks"] += 1
                    p.next_ts = now + 30
                continue
            self.pending.pop(m, None)
            tracking = self._start_tracking(md, now)
            self.db.upsert_market(md, tracking=tracking)
            if md.migration_status == MIGRATED and p.ev.status != MIGRATED:
                self._record_migration(md)
            self._emit(md)
            if self.on_enriched:
                self.on_enriched(md, tracking)
        return done

    def _record_migration(self, md: MarketData) -> None:
        self.stats["migrations_derived"] += 1
        self.db.mark_migrated(TokenEvent(MIGRATION, md.mint, "dexscreener", status=MIGRATED, pool=md.dex_id,
                                         pool_address=md.pair_address))

    def _start_tracking(self, md: MarketData, now: float) -> bool:
        if len(self.tracked) >= self.s.max_tracked:
            old_enough = [t for t in self.tracked.values() if now - t.started_ts >= self.s.min_hold_sec]
            weakest = min(old_enough, key=lambda t: t.market.vol_m5) if old_enough else None
            if weakest is not None and md.vol_m5 > weakest.market.vol_m5:
                self.stop_tracking(weakest.market.mint, "evicted")
            else:
                self.drops["tracking_full"] += 1
                self.db.add_drop(md.mint, "tracking_full", f"vol_m5 {md.vol_m5:.0f} (deferred, re-checked later)")
                self.deferred[md.mint] = md
                while len(self.deferred) > DEFERRED_MAX:
                    self.deferred.pop(next(iter(self.deferred)))
                return False
        self.deferred.pop(md.mint, None)
        b = CandleBuilder(md.mint)
        b.add(now, md.price_usd, md.vol_h24, md.buys_h24, md.sells_h24)
        self.tracked[md.mint] = Tracked(md, b, last_active_ts=now, last_txns=md.txns_h24, started_ts=now)
        self.stats["tracked_total"] += 1
        self.stats["tracked_max"] = max(self.stats["tracked_max"], len(self.tracked))
        self.stats["snapshots"] += 1
        self.db.add_snapshots([self._snap_row(md, now, "dexscreener")])
        return True

    def requeue_deferred(self, now: float | None = None) -> int:
        """Slots free again: send the most active deferred (tracking_full) tokens back to enrichment,
        as long as they are still younger than MAX_TOKEN_AGE_MIN."""
        if not self.deferred:
            return 0
        now = now or self.clock()
        for m, md in list(self.deferred.items()):
            age = md.age_min(now)
            if age is not None and age > self.s.max_token_age_min:
                self.deferred.pop(m)
                self.stats["deferred_expired"] += 1
        free = self.s.max_tracked - len(self.tracked) - sum(1 for p in self.pending.values() if p.enriched)
        if free <= 0:
            return 0
        best = sorted(self.deferred.values(), key=lambda md: md.vol_m5, reverse=True)[:free]
        for md in best:
            self.deferred.pop(md.mint, None)
            if md.mint in self.pending or md.mint in self.tracked:
                continue
            ev = TokenEvent(NEW_TOKEN, md.mint, "requeue", name=md.name, symbol=md.symbol, launch_ts=md.launch_ts,
                            status=md.migration_status)
            self.pending[md.mint] = Pending(ev, now, enriched=True)
        self.stats["requeued_from_full"] += len(best)
        return len(best)

    @staticmethod
    def _snap_row(md: MarketData, now: float, source: str) -> tuple:
        return (md.mint, now, md.price_usd, md.market_cap, md.liquidity_usd, md.vol_m5, md.vol_h24, md.buys_h24,
                md.sells_h24, source)

    def stop_tracking(self, mint: str, reason: str) -> None:
        t = self.tracked.pop(mint, None)
        if not t:
            return
        cur = t.builder.partial()
        if cur:
            self.db.upsert_candles([cur])
        self.db.set_tracking(mint, False, reason)
        self.stats[f"stopped_{reason}"] += 1
        if self.on_stop:
            self.on_stop(t.market, reason)

    # ---- tracking
    async def track_once(self) -> int:
        if not self.tracked:
            return 0
        now = self.clock()
        rows, finished, missing, mds = [], [], [], []
        for chunk in chunks(list(self.tracked), DS_BATCH):
            pairs = await self._ds_fetch(chunk)
            if pairs is None:
                continue
            grouped = group_pairs(pairs, chunk)
            for m in chunk:
                t = self.tracked.get(m)
                if t is None:
                    continue
                good = [x for x in grouped.get(m, []) if standard_quote(x)]
                if not good:
                    t.misses += 1
                    missing.append(m)
                    continue
                old = t.market
                md = market_from_pairs(m, good, now, old.launch_ts, True if old.migration_status != AMM else False)
                md.symbol, md.name = md.symbol or old.symbol, md.name or old.name
                if md.liquidity_usd is None and old.liquidity_source == "jupiter":
                    md.liquidity_usd, md.liquidity_source = old.liquidity_usd, "jupiter"
                if old.migration_status == MIGRATED and md.migration_status != MIGRATED:
                    md.migration_status = MIGRATED
                if old.migration_status == BONDING and md.migration_status == MIGRATED:
                    self._record_migration(md)
                t.market, t.misses = md, 0
                if t.last_txns is None or md.txns_h24 > t.last_txns:
                    t.last_active_ts = now
                t.last_txns = md.txns_h24
                rows.append(self._snap_row(md, now, "dexscreener"))
                finished += t.builder.add(now, md.price_usd, md.vol_h24, md.buys_h24, md.sells_h24)
                mds.append(md)
                self._emit(md)
        self.db.upsert_markets(mds, tracking=True)
        self.db.add_snapshots(rows)
        self.db.upsert_candles(finished)
        self.stats["snapshots"] += len(rows)
        self.stats["candles_local"] += sum(1 for c in finished if c.n_snapshots)
        self.stats["candles_gapfill"] += sum(1 for c in finished if not c.n_snapshots)
        if missing:
            await self._jup_fill(missing, price_fallback=True)
        self.check_stops(now)
        return len(rows)

    def check_stops(self, now: float | None = None) -> None:
        now = now or self.clock()
        for m, t in list(self.tracked.items()):
            age = t.market.age_min(now)
            if age is not None and age > self.s.max_token_age_min:
                self.stop_tracking(m, "aged_out")
            elif now - t.last_active_ts > self.s.dead_after_min * 60:
                self.stop_tracking(m, "dead")
            elif t.misses >= 4:
                self.stop_tracking(m, "lost")

    # ---- Jupiter (liquidity fill-in + price fallback)
    async def jup_once(self) -> int:
        need = [m for m, t in self.tracked.items() if t.market.liquidity_source != "dexscreener"]
        return await self._jup_fill(need, price_fallback=False)

    async def _jup_fill(self, mints: list[str], price_fallback: bool) -> int:
        n = 0
        headers = {"x-api-key": self.s.jupiter_api_key} if self.s.jupiter_api_key else None
        for chunk in chunks(mints, JUP_BATCH):
            try:
                data = await fetch_json(self._client, self.limiters["jupiter"], JUP_PRICE,
                                        params={"ids": ",".join(chunk)}, headers=headers)
                self.stats["jup_calls"] += 1
            except asyncio.CancelledError:
                raise
            except RateLimitError:
                self._err("jupiter 429 - queue paused")
                continue
            except Exception as e:
                self._err(f"jupiter failed: {type(e).__name__}: {str(e)[:100]}")
                continue
            now = self.clock()
            rows = []
            for m in chunk:
                info, t = (data or {}).get(m) or {}, self.tracked.get(m)
                if not t or not info:
                    continue
                liq, price = _f(info.get("liquidity")), _f(info.get("usdPrice"))
                if liq is not None and t.market.liquidity_source != "dexscreener":
                    t.market.liquidity_usd, t.market.liquidity_source = liq, "jupiter"
                    self.db.upsert_market(t.market, tracking=True)
                    n += 1
                if price_fallback and price:
                    rows.append((m, now, price, None, liq, None, None, None, None, "jupiter"))
                    self.db.upsert_candles(t.builder.add(now, price))
            self.db.add_snapshots(rows)
            self.stats["snapshots_jupiter"] += len(rows)
        return n

    # ---- GeckoTerminal OHLCV for the hot set
    def hot_mints(self) -> list[str]:
        cands = [t.market for t in self.tracked.values() if t.market.pair_address and t.market.vol_m5 > 0]
        return [m.mint for m in sorted(cands, key=lambda m: m.vol_m5, reverse=True)[: self.s.hot_size]]

    async def hot_once(self) -> int:
        n = 0
        for m in self.hot_mints():
            t = self.tracked.get(m)
            if not t:
                continue
            try:
                data = await fetch_json(self._client, self.limiters["geckoterminal"],
                                        GT_OHLCV.format(pool=t.market.pair_address),
                                        params={"aggregate": 1, "limit": 60, "currency": "usd"})
                self.stats["gt_calls"] += 1
            except asyncio.CancelledError:
                raise
            except RateLimitError:
                self._err("geckoterminal 429 - queue paused")
                break
            except Exception as e:
                self._err(f"geckoterminal ohlcv failed: {type(e).__name__}: {str(e)[:100]}")
                continue
            cs = parse_gt_ohlcv(m, data)
            self.db.upsert_candles(cs)
            self.stats["candles_gt"] += len(cs)
            n += len(cs)
        return n

    # ---- maintenance
    def cleanup(self) -> dict:
        out = self.db.cleanup(self.s.snapshot_retention_h, self.s.candle_retention_h, self.s.market_retention_d,
                              self.s.seen_retention_d)
        self.stats["cleanup_runs"] += 1
        return out

    def restore(self) -> int:
        """Resume tracking tokens that were tracked before a restart (still young enough)."""
        now, n = self.clock(), 0
        for m in self.db.tracked_mints():
            md = self.db.get_market(m)
            age = md.age_min(now) if md else None
            if md and (age is None or age <= self.s.max_token_age_min) and len(self.tracked) < self.s.max_tracked:
                self.tracked[m] = Tracked(md, CandleBuilder(m), last_active_ts=now, last_txns=None, started_ts=now)
                n += 1
            else:
                self.db.set_tracking(m, False, "aged_out")
        self.stats["restored"] = n
        return n

    # ---- loops
    async def _every(self, seconds: float, fn, initial_delay: float = 0.0) -> None:
        if initial_delay:
            await self._sleep(initial_delay)
        while not self._stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one bad response kill the collector
                self._err(f"{fn.__name__}: {type(e).__name__}: {str(e)[:120]}")
            await self._sleep(seconds)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _intake(self, stream) -> None:
        async for ev in stream:
            self.submit(ev)
            if self._stop.is_set():
                break

    async def _cleanup_async(self):
        self.cleanup()

    async def run(self, stream=None) -> None:
        own = self._client is None
        if own:
            import httpx
            self._client = httpx.AsyncClient(timeout=self.s.http_timeout_sec, follow_redirects=True,
                                             headers={"User-Agent": UA, "Accept": "application/json"})
        self.restore()
        tasks = [self._every(self.s.enrich_sec, self.enrich_once),
                 self._every(self.s.snapshot_sec, self.track_once, self.s.snapshot_sec),
                 self._every(self.s.jup_refresh_sec, self.jup_once, 10),
                 self._every(self.s.hot_refresh_sec, self.hot_once, 30),
                 self._every(600, self._cleanup_async, 60)]
        if stream is not None:
            tasks.append(self._intake(stream))
        try:
            await asyncio.gather(*tasks)
        finally:
            if own:
                await self._client.aclose()

    async def stop(self) -> None:
        self._stop.set()
