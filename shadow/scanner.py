"""PHASE 2 - Token scanner.

One PumpPortal websocket (subscribeNewToken + subscribeMigration) is the primary source. It has
reconnect with exponential backoff + jitter, websocket pings, and an idle watchdog that reconnects
when no message arrives for `idle_timeout` seconds.

GeckoTerminal new_pools (Solana) and RugCheck new_tokens are polled through rate-limit queues:
- poll_always=True (default, SCANNER_POLL_ALWAYS): all the time, because a live comparison on
  2026-11-13 showed the free PumpPortal stream delivered only ~20-35% of new pump.fun launches;
- poll_always=False: only as a fallback once the websocket has been down for `fallback_after`
  seconds, stopping as soon as the websocket delivers messages again.

Every token is normalized to a `TokenEvent` and de-duplicated by mint in SQLite (`seen_tokens`), so
a restart never re-announces old tokens. Consumers (phase 3) read events with:

    async for ev in scanner.stream():   # or: await scanner.queue.get()
        ...
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import Counter, deque
from datetime import datetime

from .config import Settings
from .db import Database
from .models import AMM, BONDING, MIGRATED, MIGRATION, NEW_TOKEN, UNKNOWN, TokenEvent
from .net import UA, RateLimitError, fetch_json, make_limiters

PHASE = 2
IMPLEMENTED = True
log = logging.getLogger("shadow.scanner")

WSOL = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {WSOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}       # USDT
BONDING_DEXES = {"pump-fun", "meteora-dbc", "raydium-launchlab", "letsbonk", "moonshot", "boop"}
GT_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
RC_NEW_TOKENS = "https://api.rugcheck.xyz/v1/stats/new_tokens"


# ---------------------------------------------------------------- pure helpers (unit-tested)
def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0, rand=random.random) -> float:
    """1s, 2s, 4s ... capped, with +-20% jitter so many phones don't reconnect in lockstep."""
    d = min(cap, base * (2 ** max(0, attempt)))
    return round(d * (0.8 + 0.4 * rand()), 3)


def _f(x):
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def _iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        if "." in s:  # RugCheck sends nanoseconds; Python wants <= 6 digits
            head, _, rest = s.partition(".")
            frac = rest[:len(rest) - len(rest.lstrip("0123456789"))]
            tz = rest[len(frac):]
            s = f"{head}.{frac[:6]}{tz}"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def parse_pumpportal(msg: dict, now: float | None = None) -> TokenEvent | None:
    """PumpPortal data message -> TokenEvent. Acks / unknown messages -> None."""
    now = now or time.time()
    tx = msg.get("txType")
    mint = msg.get("mint")
    if not mint or tx not in ("create", "migrate"):
        return None
    if tx == "create":
        return TokenEvent(
            kind=NEW_TOKEN, mint=mint, source="pumpportal", name=(msg.get("name") or "").strip(),
            symbol=(msg.get("symbol") or "").strip(), launch_ts=now, seen_ts=now,
            creator=msg.get("traderPublicKey") or "", dev_buy_tokens=_f(msg.get("initialBuy")),
            dev_buy_sol=_f(msg.get("solAmount")), initial_mcap_sol=_f(msg.get("marketCapSol")),
            status=BONDING, launchpad=msg.get("pool") or "pump", bonding_curve=msg.get("bondingCurveKey") or "",
            signature=msg.get("signature") or "", uri=msg.get("uri") or "", raw=msg)
    return TokenEvent(kind=MIGRATION, mint=mint, source="pumpportal", seen_ts=now, status=MIGRATED,
                      pool=msg.get("pool") or "", signature=msg.get("signature") or "", raw=msg)


def parse_gt_new_pools(payload: dict, now: float | None = None) -> list[TokenEvent]:
    """GeckoTerminal /new_pools?include=base_token,quote_token,dex -> events (one per pool)."""
    now = now or time.time()
    tokens = {i["id"]: i.get("attributes", {}) for i in payload.get("included", []) if i.get("type") == "token"}
    out = []
    for p in payload.get("data", []):
        a, rel = p.get("attributes", {}), p.get("relationships", {})
        base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
        quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id", "")
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
        bmint, qmint = base.removeprefix("solana_"), quote.removeprefix("solana_")
        if bmint in QUOTE_MINTS and qmint and qmint not in QUOTE_MINTS:
            bmint, base = qmint, quote
        if not bmint or bmint in QUOTE_MINTS:
            continue
        t = tokens.get(base, {})
        bonding = dex in BONDING_DEXES
        out.append(TokenEvent(
            kind=NEW_TOKEN, mint=bmint, source="geckoterminal", name=t.get("name") or (a.get("name") or "").split(" / ")[0],
            symbol=t.get("symbol") or "", launch_ts=_iso_ts(a.get("pool_created_at")), seen_ts=now,
            initial_mcap_usd=_f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
            status=BONDING if bonding else AMM, launchpad=dex if bonding else "",
            pool="" if bonding else dex, pool_address=a.get("address") or "", raw=p))
    return out


def parse_rugcheck_new(items: list, now: float | None = None) -> list[TokenEvent]:
    now = now or time.time()
    out = []
    for it in items or []:
        mint = it.get("mint")
        if not mint:
            continue
        out.append(TokenEvent(kind=NEW_TOKEN, mint=mint, source="rugcheck", symbol=(it.get("symbol") or "").strip(),
                              creator=it.get("creator") or "", launch_ts=_iso_ts(it.get("createAt")), seen_ts=now,
                              status=UNKNOWN, raw=it))
    return out


# ---------------------------------------------------------------- scanner
class Scanner:
    def __init__(self, settings: Settings, db: Database, *, connect=None, client=None, limiters=None,
                 backoff_base: float = 1.0, backoff_max: float = 60.0, stable_after: float = 60.0,
                 idle_timeout: float | None = None, fallback_after: float | None = None,
                 gt_interval: float | None = None, rc_interval: float | None = None, tick: float = 1.0,
                 force_fallback: bool = False, poll_always: bool | None = None,
                 queue_size: int = 2000, clock=time.monotonic, rand=random.random):
        pick = lambda v, d: d if v is None else v  # noqa: E731
        idle_timeout = pick(idle_timeout, settings.ws_idle_timeout_sec)
        fallback_after = pick(fallback_after, settings.fallback_after_sec)
        gt_interval = pick(gt_interval, settings.gt_poll_sec)
        rc_interval = pick(rc_interval, settings.rc_poll_sec)
        self.poll_always = pick(poll_always, settings.scanner_poll_always)
        self.s, self.db = settings, db
        self._connect = connect
        self._client = client
        self.limiters = limiters or make_limiters()
        self.backoff_base, self.backoff_max, self.stable_after = backoff_base, backoff_max, stable_after
        self.idle_timeout, self.fallback_after = idle_timeout, fallback_after
        self.gt_interval, self.rc_interval, self.tick = gt_interval, rc_interval, tick
        self.force_fallback = force_fallback
        self.clock, self.rand = clock, rand
        self.queue: asyncio.Queue[TokenEvent] = asyncio.Queue(maxsize=queue_size)
        self.stats: Counter = Counter()
        self.errors: deque = deque(maxlen=50)
        self.backoffs: list[float] = []
        self.ws_up = False
        self._down_since = clock()
        self._last_msg = 0.0
        self._fallback_on = False
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.started_at = time.time()

    # ---- public API
    async def run(self) -> None:
        own_client = self._client is None
        if own_client:
            import httpx
            self._client = httpx.AsyncClient(timeout=self.s.http_timeout_sec, follow_redirects=True,
                                             headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            await asyncio.gather(self._ws_loop(), self._fallback_loop())
        finally:
            if own_client:
                await self._client.aclose()

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="shadow-scanner")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def stream(self):
        """Async generator of new TokenEvents (new tokens + migrations). Starts the scanner if needed."""
        self.start()
        while not self._stop.is_set():
            yield await self.queue.get()

    @property
    def mode(self) -> str:
        if self.force_fallback:
            return "fallback(forced)"
        if self.ws_up:
            return "websocket+polling" if self.poll_always else "websocket"
        return "fallback" if self._fallback_on else "reconnecting"

    def snapshot(self) -> dict:
        return {"mode": self.mode, "uptime_s": int(time.time() - self.started_at), **dict(self.stats),
                "seen_in_db": self.db.count_seen(), "last_errors": list(self.errors)[-5:]}

    # ---- event handling (sync, testable)
    def handle(self, ev: TokenEvent) -> TokenEvent | None:
        """De-dup + persist. Returns the event to emit, or None for duplicates."""
        if ev.kind == NEW_TOKEN and ev.status == AMM and self.db.status_of(ev.mint) == BONDING:
            ev.kind, ev.status = MIGRATION, MIGRATED  # bonding token showed up on an AMM pool = graduated
        if ev.kind == MIGRATION:
            if self.db.mark_migrated(ev):
                self.stats["migrations"] += 1
                self.stats[f"migrations_{ev.source}"] += 1
                return self._emit(ev)
            self.stats["duplicate_migrations"] += 1
            return None
        if self.db.mark_new(ev):
            self.stats["new_tokens"] += 1
            self.stats[f"new_{ev.source}"] += 1
            return self._emit(ev)
        self.stats["duplicates"] += 1
        self.db.fill_missing(ev)
        return None

    def handle_ws_raw(self, raw) -> TokenEvent | None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            self.stats["ws_bad_json"] += 1
            return None
        if not isinstance(msg, dict):
            self.stats["ws_unknown"] += 1
            return None
        if "message" in msg and "mint" not in msg:
            self.stats["ws_acks"] += 1
            return None
        if "errors" in msg or "error" in msg:
            self.stats["ws_server_errors"] += 1
            self._err(f"pumpportal: {str(msg)[:150]}")
            return None
        ev = parse_pumpportal(msg)
        if ev is None:
            self.stats["ws_unknown"] += 1
            return None
        return self.handle(ev)

    def _emit(self, ev: TokenEvent) -> TokenEvent:
        if self.queue.full():
            self.queue.get_nowait()
            self.stats["queue_dropped"] += 1
        self.queue.put_nowait(ev)
        return ev

    def _err(self, text: str) -> None:
        self.stats["errors"] += 1
        self.errors.append(f"{time.strftime('%H:%M:%S')} {text}")
        log.warning(text)

    # ---- websocket
    def _default_connect(self):
        import websockets
        return websockets.connect(self.s.pumpportal_ws, open_timeout=self.s.http_timeout_sec, ping_interval=20,
                                  ping_timeout=20, close_timeout=5, max_size=2 ** 20, user_agent_header=UA)

    async def _ws_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            connected_at = None
            reason = ""
            try:
                cm = self._connect() if self._connect else self._default_connect()
                async with cm as ws:
                    connected_at = self.clock()
                    self.stats["ws_connects"] += 1
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout)
                        if not self.ws_up:
                            self.ws_up = True
                            log.info("PumpPortal websocket live")
                        self._last_msg = self.clock()
                        self.handle_ws_raw(raw)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self.stats["ws_idle_timeouts"] += 1
                reason = f"no message for {self.idle_timeout:.0f}s (watchdog)"
            except Exception as e:  # network drop, DNS failure, server close, ban...
                self.stats["ws_errors"] += 1
                reason = f"{type(e).__name__}: {str(e)[:120]}"
            if self.ws_up or connected_at is not None:
                self.stats["ws_disconnects"] += 1
            if self.ws_up:
                self._down_since = self.clock()
            self.ws_up = False
            if self._stop.is_set():
                break
            if connected_at is not None and self.clock() - connected_at >= self.stable_after:
                attempt = 0  # it was a healthy session; start backoff from scratch
            delay = backoff_delay(attempt, self.backoff_base, self.backoff_max, self.rand)
            self.backoffs.append(delay)
            attempt += 1
            self._err(f"websocket down ({reason}); reconnect #{attempt} in {delay:.1f}s")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # ---- fallback polling
    def fallback_needed(self) -> bool:
        if self.force_fallback:
            return True
        return (not self.ws_up) and (self.clock() - self._down_since >= self.fallback_after)

    def polling_needed(self) -> bool:
        return self.poll_always or self.fallback_needed()

    async def _fallback_loop(self) -> None:
        """Polls GeckoTerminal + RugCheck. Always in poll_always mode; otherwise only as websocket fallback."""
        next_gt = next_rc = 0.0
        while not self._stop.is_set():
            down = self.fallback_needed()
            if down != self._fallback_on:
                self._fallback_on = down
                self.stats["fallback_on" if down else "fallback_off"] += 1
                log.info("fallback %s", "STARTED (websocket down)" if down else "ended (websocket back)")
            if self.polling_needed():
                now = self.clock()
                if now >= next_gt:
                    next_gt = now + self.gt_interval
                    await self._poll("geckoterminal", GT_NEW_POOLS,
                                     {"page": 1, "include": "base_token,quote_token,dex"}, parse_gt_new_pools)
                if now >= next_rc and self.polling_needed():
                    next_rc = now + self.rc_interval
                    await self._poll("rugcheck", RC_NEW_TOKENS, None, parse_rugcheck_new)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass

    async def _poll(self, name, url, params, parser) -> int:
        self.stats[f"poll_{name}"] += 1
        try:
            data = await fetch_json(self._client, self.limiters[name], url, params=params)
            events = parser(data)
        except asyncio.CancelledError:
            raise
        except RateLimitError:
            self.stats[f"poll_{name}_429"] += 1
            self._err(f"{name} rate limited (429), queue paused")
            return 0
        except Exception as e:
            self._err(f"{name} poll failed: {type(e).__name__}: {str(e)[:120]}")
            return 0
        emitted = 0
        for ev in sorted(events, key=lambda e: e.launch_ts or 0):
            if self.handle(ev):
                emitted += 1
        return emitted
