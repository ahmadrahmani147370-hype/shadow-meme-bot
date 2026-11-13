"""PHASE 5 - MCAP and TOP VOLUME universes (established Solana meme coins, no age limit).

Discovery (every UNIVERSE_DISCOVER_SEC, default 5 min), all free and keyless:
    GeckoTerminal networks/solana/trending_pools, pools?sort=h24_volume_usd_desc, pools?sort=h24_tx_count_desc
    DexScreener token-boosts/top/v1
Candidates are validated with DexScreener tokens/v1 (30 mints per call) and pass a meme filter
(no SOL / stablecoins / LSTs / wrapped BTC-ETH / tokenized stocks / majors unless allowed).

    MCAP   = market cap >= UNIVERSE_MCAP_MIN ($1M) and liquidity >= UNIVERSE_MCAP_MIN_LIQ;
             the UNIVERSE_MCAP_MAX most traded (24h volume) are tracked, rank = by market cap.
    VOLUME = top UNIVERSE_VOLUME_TOP_N by 24h volume plus top N by 1h volume (min liquidity/mcap),
             at most UNIVERSE_VOLUME_MAX; ranks by 24h and 1h volume are both stored.

Tracked coins get a snapshot every UNIVERSE_REFRESH_SEC and local 1-minute candles. DexScreener's h24
counters are rolling windows for coins older than 24 h, so candle volume / buys / sells here are
ESTIMATED by integrating the m5 rolling window (vol_m5 * dt / 300); prices are exact. Coins tracked by
the new-launch collector are skipped (no double polling) and keep their universe in token_market.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace

from .candles import CandleBuilder, parse_gt_ohlcv
from .collector import DS_BATCH, DS_TOKENS, chunks, group_pairs, market_from_pairs, standard_quote
from .config import Settings
from .db import Database
from .models import U_MCAP, U_NEW, U_VOLUME, Candle, MarketData
from .net import UA, RateLimitError, fetch_json, make_limiters

PHASE = 5
IMPLEMENTED = True
log = logging.getLogger("shadow.universes")

GT_BASE = "https://api.geckoterminal.com/api/v2/networks/solana/"
GT_LISTS = (("trending_pools", {}), ("pools", {"sort": "h24_volume_usd_desc"}), ("pools", {"sort": "h24_tx_count_desc"}),
            # v1.2: more >= $1M discovery - trending over 1h and 6h as well
            ("trending_pools", {"duration": "1h"}), ("trending_pools", {"duration": "6h"}))
# v1.2: DexScreener latest boosts + latest profiles every run (were only used while behind the coin pace)
DS_ALWAYS = ("https://api.dexscreener.com/token-boosts/latest/v1", "https://api.dexscreener.com/token-profiles/latest/v1")
DS_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
# v1.1.1 widened discovery (only while the daily coin minimum is behind pace)
GT_WIDE_LISTS = (("new_pools", {}),)
DS_WIDE = ("https://api.dexscreener.com/token-boosts/latest/v1", "https://api.dexscreener.com/token-profiles/latest/v1")
WIDE_EXTRA_PAGES = 0      # v1.2: 3 pages per list by default already (GeckoTerminal 6 calls/min budget)

# Not memes: SOL, stables, LSTs, wrapped assets, and "majors" (allowed with UNIVERSE_ALLOW_MAJORS=true)
NON_MEME_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": "PYUSD",
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": "USDG",
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": "cbBTC",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "JitoSOL",
    "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4": "JLP",
}
MAJOR_MINTS = {
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "RAY",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "WIF",
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": "PUMP",
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": "JTO",
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3": "PYTH",
    "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE": "ORCA",
}
NON_MEME_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "USDG", "PYUSD", "USD1", "USDS", "USDE", "EURC", "FDUSD", "DAI",
                    "WBTC", "CBBTC", "ZBTC", "TBTC", "BTC", "WETH", "ETH", "JLP", "MSOL", "JITOSOL", "BSOL", "JUPSOL",
                    "INF", "BNSOL", "HSOL", "DSOL", "STSOL"}
MAJOR_SYMBOLS = {"JUP", "RAY", "BONK", "WIF", "PUMP", "JTO", "PYTH", "ORCA", "W", "DRIFT", "KMNO", "TNSR", "HNT",
                 "RENDER", "TRUMP", "MELANIA"}
NON_MEME_NAME_WORDS = ("xstock", "securities", "tokenized", "wrapped", "bridged", "staked sol", "liquid staking",
                       "stablecoin", "global dollar", "usd coin", "tether", "ondo")


def meme_filter(mint: str, symbol: str, name: str, price: float | None, allow_majors: bool = False,
                extra_exclude: set | None = None) -> str | None:
    """Returns the reason a coin is NOT a tradable meme, or None if it passes."""
    sym, nm = (symbol or "").strip().upper(), (name or "").strip().lower()
    if extra_exclude and mint in extra_exclude:
        return "excluded_by_config"
    if mint in NON_MEME_MINTS or sym in NON_MEME_SYMBOLS:
        return "not_meme_asset"
    if not allow_majors and (mint in MAJOR_MINTS or sym in MAJOR_SYMBOLS):
        return "major"
    if any(w in nm for w in NON_MEME_NAME_WORDS):
        return "not_meme_asset"
    if ("USD" in sym or "EUR" in sym) and price and 0.9 <= price <= 1.1:
        return "stablecoin"
    if sym.endswith("X") and len(sym) <= 6 and symbol[-1:] == "x" and price and price > 5:
        return "tokenized_stock"           # NVDAx, TSLAx ...
    if sym.endswith("SOL") and price and price > 20:
        return "not_meme_asset"            # liquid staking tokens trade near the SOL price
    return None


def parse_gt_pools(payload: dict) -> list[dict]:
    """GeckoTerminal pool list -> [{mint, symbol, name, mcap, vol_h24, vol_h1, liquidity, buyers_h1, sellers_h1}]."""
    toks = {t.get("id"): (t.get("attributes") or {}) for t in (payload or {}).get("included") or [] if t.get("type") == "token"}
    out = []
    for p in (payload or {}).get("data") or []:
        a = p.get("attributes") or {}
        rid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
        if not rid.startswith("solana_"):
            continue
        mint = rid.split("_", 1)[1]
        t = toks.get(rid, {})
        f = lambda x: float(x) if x not in (None, "") else None  # noqa: E731
        vol, tx = a.get("volume_usd") or {}, (a.get("transactions") or {}).get("h1") or {}
        out.append({"mint": mint, "symbol": t.get("symbol") or (a.get("name") or "").split(" / ")[0],
                    "name": t.get("name") or "", "mcap": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")),
                    "vol_h24": f(vol.get("h24")), "vol_h1": f(vol.get("h1")), "liquidity": f(a.get("reserve_in_usd")),
                    "buyers_h1": tx.get("buyers"), "sellers_h1": tx.get("sellers"),
                    "price": f(a.get("base_token_price_usd"))})
    return out


def parse_ds_boosts(payload) -> list[str]:
    return [b["tokenAddress"] for b in payload or [] if b.get("chainId") == "solana" and b.get("tokenAddress")]


def mcap_of(md: MarketData) -> float:
    return md.market_cap or md.fdv or 0.0


def select_universes(s: Settings, markets: list[MarketData]) -> tuple[list[MarketData], list[MarketData], dict]:
    """Pure selection: (mcap members ranked by mcap, volume members ranked by 24h volume, rank_h1 map)."""
    mcap = [m for m in markets if mcap_of(m) >= s.universe_mcap_min and (m.liquidity_usd or 0) >= s.universe_mcap_min_liq]
    mcap = sorted(mcap, key=lambda m: m.vol_h24, reverse=True)[: s.universe_mcap_max]
    mcap.sort(key=mcap_of, reverse=True)
    vol_ok = [m for m in markets if (m.liquidity_usd or 0) >= s.universe_volume_min_liq
              and mcap_of(m) >= s.universe_volume_min_mcap]
    by24 = sorted((m for m in vol_ok if m.vol_h24 >= s.universe_volume_min_vol_h24), key=lambda m: m.vol_h24, reverse=True)
    by1 = sorted((m for m in vol_ok if m.vol_h1 >= s.universe_volume_min_vol_h1), key=lambda m: m.vol_h1, reverse=True)
    rank_h1 = {m.mint: i + 1 for i, m in enumerate(sorted(vol_ok, key=lambda m: m.vol_h1, reverse=True))}
    chosen, seen = [], set()
    for m in by24[: s.universe_volume_top_n] + by1[: s.universe_volume_top_n]:
        if m.mint not in seen:
            seen.add(m.mint)
            chosen.append(m)
    chosen = sorted(chosen, key=lambda m: m.vol_h24, reverse=True)[: s.universe_volume_max]
    return mcap, chosen, rank_h1


@dataclass
class UTracked:
    market: MarketData
    builder: CandleBuilder
    last_ts: float
    cum: list = field(default_factory=lambda: [0.0, 0.0, 0.0])   # synthetic cumulative vol / buys / sells
    start_ts: float = 0.0
    backfill_tried: float = 0.0


class UniverseCollector:
    def __init__(self, settings: Settings, db: Database, *, client=None, limiters=None, clock=time.time,
                 skip_fn=None):
        self.s, self.db = settings, db
        self._client = client
        self.limiters = limiters if limiters is not None else make_limiters()
        self.clock = clock
        self.skip_fn = skip_fn or (lambda mint: False)   # e.g. "tracked by the new-launch collector"
        self.extra_exclude = {m.strip() for m in settings.universe_exclude_mints.split(",") if m.strip()}
        self.candidates: dict[str, float] = {}             # mint -> last seen in a discovery list
        self.tracked: dict[str, UTracked] = {}
        self.members: dict[str, set] = {U_MCAP: set(), U_VOLUME: set()}
        self.stats: Counter = Counter()
        self.filtered: Counter = Counter()
        self.errors: deque = deque(maxlen=50)
        self._stop = asyncio.Event()

    # ---- public API (same shape as Collector)
    def tracked_mints(self) -> list[str]:
        return list(self.tracked)

    def get_market(self, mint: str) -> MarketData | None:
        t = self.tracked.get(mint)
        return t.market if t else None

    def get_candles(self, mint: str, minutes: int = 60, include_partial: bool = True) -> list[Candle]:
        since = int((self.clock() - minutes * 60) // 60) * 60
        out = self.db.get_candles(mint, since, "local")
        t = self.tracked.get(mint)
        if include_partial and t and t.builder.partial():
            cur = t.builder.partial()
            if not out or cur.ts > out[-1].ts:
                out.append(replace(cur))
        return out

    def snapshot(self) -> dict:
        return {"candidates": len(self.candidates), "tracked": len(self.tracked),
                "mcap": len(self.members[U_MCAP]), "volume": len(self.members[U_VOLUME]),
                **dict(self.stats), "filtered": dict(self.filtered), "last_errors": list(self.errors)[-5:]}

    def _err(self, text: str) -> None:
        self.stats["errors"] += 1
        self.errors.append(f"{time.strftime('%H:%M:%S')} {text}")
        log.warning(text)

    async def _get(self, limiter: str, url: str, params=None):
        try:
            data = await fetch_json(self._client, self.limiters[limiter], url, params=params)
            self.stats[f"{limiter}_calls"] += 1
            return data
        except asyncio.CancelledError:
            raise
        except RateLimitError:
            self._err(f"{limiter} 429 - queue paused")
        except Exception as e:
            self._err(f"{limiter} failed: {type(e).__name__}: {str(e)[:100]}")
        return None

    # ---- discovery
    async def discover_once(self) -> int:
        now = self.clock()
        found: list[str] = []
        wide = bool(getattr(self, "widen", False))
        lists = GT_LISTS + (GT_WIDE_LISTS if wide else ())
        pages = self.s.universe_gt_pages + (WIDE_EXTRA_PAGES if wide else 0)
        if wide:
            self.stats["discover_wide"] += 1
        if not (self.members.get(U_MCAP) or self.members.get(U_VOLUME)):
            # v1.2: after a restart re-rank the known candidates at once (recent members + new launches >= the MCAP
            # floor) instead of leaving the MCAP / TOP VOLUME universes empty until the slow GeckoTerminal pass ends
            for (m,) in self.db.con.execute("SELECT mint FROM universe_members WHERE universe IN (?,?) AND last_ts>=?",
                                            (U_MCAP, U_VOLUME, now - 6 * 3600)):
                self.candidates.setdefault(m, now)
            await self.validate_once()
        # DexScreener lists first (own rate limit), then GeckoTerminal
        found += parse_ds_boosts(await self._get("dexscreener_profiles", DS_BOOSTS_TOP))
        for url in DS_ALWAYS + tuple(u for u in (DS_WIDE if wide else ()) if u not in DS_ALWAYS):
            found += parse_ds_boosts(await self._get("dexscreener_profiles", url))
        # page 1 of every list first (then validate), deeper pages after - members appear within ~1 minute
        order = [(page, path, extra) for page in range(1, pages + 1) for path, extra in lists]
        for k, (page, path, extra) in enumerate(order):
            if page == 2 and order[k - 1][0] == 1:
                for m in found:
                    self.candidates[m] = now
                await self.validate_once()
            data = await self._get("geckoterminal", GT_BASE + path, {"page": page, "include": "base_token", **extra})
            for p in parse_gt_pools(data):
                reason = meme_filter(p["mint"], p["symbol"], p["name"], p["price"], self.s.universe_allow_majors,
                                     self.extra_exclude)
                if reason:
                    self.filtered[reason] += 1
                else:
                    found.append(p["mint"])
        for m in found:
            self.candidates[m] = now
        # keep the candidate pool bounded: members first, then most recently seen
        if len(self.candidates) > self.s.universe_max_candidates:
            keep = set(self.tracked)
            rest = sorted((m for m in self.candidates if m not in keep), key=self.candidates.get, reverse=True)
            for m in rest[self.s.universe_max_candidates - len(keep):]:
                self.candidates.pop(m, None)
        self.stats["discover_runs"] += 1
        await self.validate_once()
        return len(found)

    async def _fetch_markets(self, mints: list[str]) -> dict[str, MarketData]:
        now, out = self.clock(), {}
        for chunk in chunks(mints, DS_BATCH):
            pairs = await self._get("dexscreener", DS_TOKENS + ",".join(chunk))
            if pairs is None:
                continue
            for m, ps in group_pairs(pairs, chunk).items():
                good = [x for x in ps if standard_quote(x)]
                if good:
                    out[m] = market_from_pairs(m, good, now, launchpad_hint=m.endswith("pump"))
        return out

    async def validate_once(self) -> dict:
        """Re-rank every candidate (DexScreener), choose members, start/stop tracking."""
        # new launches that grew past the MCAP floor (seen by the new-launch collector) are candidates too
        for (m,) in self.db.con.execute(
                "SELECT mint FROM token_market WHERE COALESCE(market_cap, fdv, 0) >= ? AND COALESCE(liquidity_usd, 0) >= ?"
                " AND updated_ts >= ? ORDER BY updated_ts DESC LIMIT 100",
                (self.s.universe_mcap_min, self.s.universe_mcap_min_liq, self.clock() - 86400)):
            self.candidates.setdefault(m, self.clock())
        mints = [m for m in self.candidates if not self.skip_fn(m)]
        markets = await self._fetch_markets(mints)
        ok = []
        for md in markets.values():
            reason = meme_filter(md.mint, md.symbol, md.name, md.price_usd, self.s.universe_allow_majors,
                                 self.extra_exclude)
            if reason:
                self.filtered[reason] += 1
                self.candidates.pop(md.mint, None)
            else:
                ok.append(md)
        mcap, vol, rank_h1 = select_universes(self.s, ok)
        now = self.clock()
        self.members = {U_MCAP: {m.mint for m in mcap}, U_VOLUME: {m.mint for m in vol}}
        for i, md in enumerate(mcap):
            self.db.upsert_universe_member(md.mint, U_MCAP, rank=i + 1, metric=mcap_of(md), source="dexscreener",
                                           now=now, rank_h1=rank_h1.get(md.mint), mcap=mcap_of(md),
                                           vol_h24=md.vol_h24, vol_h1=md.vol_h1, liquidity_usd=md.liquidity_usd)
        for i, md in enumerate(vol):
            self.db.upsert_universe_member(md.mint, U_VOLUME, rank=i + 1, metric=md.vol_h24, source="dexscreener",
                                           now=now, rank_h1=rank_h1.get(md.mint), mcap=mcap_of(md),
                                           vol_h24=md.vol_h24, vol_h1=md.vol_h1, liquidity_usd=md.liquidity_usd)
        for u in (U_MCAP, U_VOLUME):
            self.db.deactivate_universe_members(u, self.members[u])
        wanted = self.members[U_MCAP] | self.members[U_VOLUME]
        for m in list(self.tracked):
            if m not in wanted:
                t = self.tracked.pop(m)
                cur = t.builder.partial()
                if cur:
                    self.db.upsert_candles([cur])
                self.stats["stopped"] += 1
        for md in mcap + vol:
            if md.mint not in self.tracked:
                self._start(md, now)
        self.db.add_universe_run({"ts": now, "candidates": len(self.candidates), "mcap_members": len(mcap),
                                  "volume_members": len(vol), "filtered": dict(self.filtered),
                                  "calls": self.stats["dexscreener_calls"] + self.stats["geckoterminal_calls"]})
        return {"candidates": len(mints), "markets": len(markets), "mcap": len(mcap), "volume": len(vol)}

    def _universe_of(self, mint: str) -> str:
        # a $1M+ coin is judged with the MCAP parameter set even when it is also a top-volume coin;
        # the VOLUME set is for high-volume coins below the MCAP floor
        return U_MCAP if mint in self.members[U_MCAP] else U_VOLUME

    def _start(self, md: MarketData, now: float) -> None:
        md.universe = self._universe_of(md.mint)
        b = CandleBuilder(md.mint)
        t = UTracked(md, b, now, start_ts=now)
        b.add(now, md.price_usd, 0.0, 0.0, 0.0)
        self.tracked[md.mint] = t
        self.db.upsert_markets([md])
        self.stats["tracked_total"] += 1

    # ---- refresh
    async def refresh_once(self) -> int:
        mints = [m for m in self.tracked if not self.skip_fn(m)]
        for m in [m for m in self.tracked if self.skip_fn(m)]:
            self.tracked.pop(m, None)                    # the new-launch collector owns it now
        if not mints:
            return 0
        markets = await self._fetch_markets(mints)
        now, rows, mds, finished = self.clock(), [], [], []
        for m, md in markets.items():
            t = self.tracked.get(m)
            if not t:
                continue
            md.universe = self._universe_of(m)
            dt = min(max(now - t.last_ts, 0.0), 120.0)
            t.cum[0] += md.vol_m5 * dt / 300.0
            t.cum[1] += md.buys_m5 * dt / 300.0
            t.cum[2] += md.sells_m5 * dt / 300.0
            t.market, t.last_ts = md, now
            finished += t.builder.add(now, md.price_usd, t.cum[0], t.cum[1], t.cum[2])
            rows.append((m, now, md.price_usd, md.market_cap, md.liquidity_usd, md.vol_m5, md.vol_h24, md.buys_h24,
                         md.sells_h24, "dexscreener_u"))
            mds.append(md)
        for c in finished:                                # estimated counters -> round trade counts
            c.buys, c.sells = int(round(c.buys)), int(round(c.sells))
        self.db.upsert_markets(mds)
        self.db.add_snapshots(rows)
        self.db.upsert_candles(finished)
        self.stats["snapshots"] += len(rows)
        return len(rows)

    # ---- GeckoTerminal backfill: members start with history instead of waiting 30+ minutes
    def backfill_due(self) -> str | None:
        now = self.clock()
        best = None
        for m, t in self.tracked.items():
            if not t.market.pair_address or now - t.backfill_tried < 1800 or self.skip_fn(m):
                continue
            since = int((now - self.s.universe_backfill_minutes * 60) // 60) * 60
            have = self.db.con.execute("SELECT COUNT(*) FROM candles_1m WHERE mint=? AND ts>=? AND source='local'",
                                       (m, since)).fetchone()[0]
            if have >= self.s.universe_backfill_minutes * 0.6:
                continue
            key = (m in self.members[U_MCAP], t.market.vol_h1)      # MCAP first, then the busiest
            if best is None or key > best[0]:
                best = (key, m)
        return best[1] if best else None

    async def backfill_once(self) -> int:
        m = self.backfill_due()
        if m is None:
            return 0
        t = self.tracked[m]
        t.backfill_tried = self.clock()
        data = await self._get("geckoterminal", GT_BASE + f"pools/{t.market.pair_address}/ohlcv/minute",
                               {"aggregate": 1, "limit": self.s.universe_backfill_minutes, "currency": "usd",
                                "token": m})
        if not data:
            return 0
        start_min = int(t.start_ts // 60) * 60
        cs = [replace(c, source="local", n_snapshots=0, buys=0, sells=0)
              for c in parse_gt_ohlcv(m, data) if c.ts < start_min]      # never overwrite live candles
        if cs:
            self.db.insert_candles_ignore(cs)
        self.stats["backfilled_coins"] += 1
        self.stats["backfilled_candles"] += len(cs)
        return len(cs)

    # ---- loops
    async def _every(self, seconds: float, fn, initial_delay: float = 0.0) -> None:
        if initial_delay:
            await self._sleep(initial_delay)
        while not self._stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._err(f"{fn.__name__}: {type(e).__name__}: {str(e)[:120]}")
            await self._sleep(seconds)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        own = self._client is None
        if own:
            import httpx
            self._client = httpx.AsyncClient(timeout=self.s.http_timeout_sec, follow_redirects=True,
                                             headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            await asyncio.gather(self._every(self.s.universe_discover_sec, self.discover_once, 5),
                                 self._every(self.s.universe_refresh_sec, self.refresh_once, 40),
                                 self._every(self.s.universe_backfill_sec, self.backfill_once, 60))
        finally:
            if own:
                await self._client.aclose()

    async def stop(self) -> None:
        self._stop.set()


__all__ = ["UniverseCollector", "meme_filter", "parse_gt_pools", "parse_ds_boosts", "select_universes",
           "NON_MEME_MINTS", "MAJOR_MINTS"]
