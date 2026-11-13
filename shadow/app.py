"""PHASE 8 - the full bot: scanner + collectors + safety + signals + judge + paper + learning + maintenance + Telegram.

`python main.py` (and start.sh) runs this. No terminal menus: control is Telegram only. Telegram runs in its own
tasks; if it is unreachable the trading pipeline keeps going and messages wait in the outgoing queue.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time

import httpx

from .collector import Collector
from .config import Settings
from .db import Database
from .judge import Judge
from .learning import Learner
from .maintenance import Maintenance, consistency
from .net import UA, make_limiters
from .paper import PaperEngine
from .safety import SafetyChecker
from .scanner import Scanner
from .signals import SignalEngine, Sources
from .telegram_api import TelegramAPI
from .telegram_bot import TelegramBot
from .universes import UniverseCollector

log = logging.getLogger("shadow.app")


class App:
    def __init__(self, s: Settings, *, api: TelegramAPI | None = None, telegram: bool = True):
        self.s = s
        self.db = Database(str(s.path(s.db_path)), busy_timeout_ms=s.db_busy_timeout_ms)
        self.db.acquire_writer_lock()            # one bot per DB (WriterLocked otherwise)
        self.started_at = time.time()
        self.stop_event = asyncio.Event()
        lim = make_limiters()
        self.scanner = Scanner(s, self.db, limiters=lim)
        self.col = Collector(s, self.db, limiters=lim)
        self.uni = (UniverseCollector(s, self.db, limiters=lim, skip_fn=lambda m: m in self.col.tracked)
                    if s.universes_enabled else None)
        self.src = Sources(self.col, self.uni)
        self.chk = SafetyChecker(s, self.db, limiters=lim, market_fn=self.src.get_market)
        self.judge = Judge(s, self.db)
        self.client = httpx.AsyncClient(timeout=s.http_timeout_sec, headers={"User-Agent": UA, "Accept": "application/json"})
        self.paper = PaperEngine(s, self.db, src=self.src, checker=self.chk, limiters=lim, client=self.client,
                                 on_trade=self._on_trade)
        self.learner = Learner(s, self.db, judge=self.judge)
        self.eng = SignalEngine(s, self.db, self.src, checker=self.chk, judge=self.judge, paper=self.paper,
                                on_signal=self._on_signal)
        self.maint = Maintenance(s, self.db)
        self.bot = None
        if telegram and s.telegram_bot_token:
            self.api = api or TelegramAPI(s.telegram_bot_token, s.telegram_api_base)
            self.bot = TelegramBot(s, self.db, self.api, app=self)
        self.paper.paused = self.paused
        self.learn_runs = 0

    # ------------------------------------------------------------ pause state (persisted)
    @property
    def paused(self) -> bool:
        return self.db.meta_get("paused", "0") == "1"

    def set_paused(self, value: bool) -> None:
        self.db.meta_set("paused", "1" if value else "0")
        self.paper.paused = value
        log.info("bot %s via Telegram", "PAUSED" if value else "RESUMED")

    # ------------------------------------------------------------ hooks
    def _on_signal(self, row: dict, verdict) -> None:
        if row["alert_type"] != "REJECT":
            j = f" judge {verdict.verdict} {verdict.confidence:.2f}" if verdict is not None else ""
            log.info("%s %s [%s] %s score %.0f%s | %s", row["alert_type"], row.get("symbol") or row["mint"][:8],
                     row["universe"], row.get("strategy") or "-", row.get("score") or 0, j, row.get("reason") or "")
        if self.bot:
            self.bot.on_signal(row, verdict)

    def _on_trade(self, kind: str, pos: dict) -> None:
        log.info("PAPER %s #%s %s %s", kind, pos.get("id"), pos.get("symbol") or pos.get("mint", "")[:8],
                 f"pnl ${pos.get('pnl_usd'):+.2f}" if kind == "CLOSE" and pos.get("pnl_usd") is not None else "")
        if self.bot:
            self.bot.on_trade(kind, pos)

    # ------------------------------------------------------------ status for /status and /scanner
    def status(self) -> dict:
        st, e = self.eng.stats, self.paper.equity()
        try:
            db_mb = sum(os.path.getsize(p) for p in (self.db.path, self.db.path + "-wal") if os.path.exists(p)) / 1e6
        except (OSError, AttributeError):
            db_mb = 0.0
        return {"scanner_mode": self.scanner.mode, "new_tokens": self.scanner.stats.get("new_tokens", 0),
                "tracked_new": len(self.col.tracked), "tracked_mcap": len(self.uni.members["mcap"]) if self.uni else 0,
                "tracked_volume": len(self.uni.members["volume"]) if self.uni else 0,
                "safety_checked": self.chk.stats.get("checked_unique", 0), "buy_watch": st["alert_BUY_WATCH"],
                "approve": st["judge_APPROVE"], "veto": st["judge_VETO"], "sell": st["alert_SELL"],
                "reject": st["alert_REJECT"], "equity": e["equity"], "cash": e["cash"], "n_open": e["n_open"],
                "realized_pnl": e["realized_pnl"], "db_mb": db_mb,
                "strategies": {k: v.version for k, v in self.eng.strategies.items()}}

    def scanner_status(self) -> dict:
        d = self.scanner.snapshot()
        d["tracked"] = len(self.col.tracked)
        if self.uni:
            d["mcap"], d["volume"] = len(self.uni.members["mcap"]), len(self.uni.members["volume"])
        return d

    # ------------------------------------------------------------ run
    async def _learn_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.learn_every_min * 60)
            try:
                res = self._learn_once()   # same thread: sqlite connection is not shared across threads
                acts = [r for r in res if r.get("action") in ("activate", "rollback")]
                if acts:
                    log.info("learning: %s", "; ".join(f"{r['target']} {r['action']}: {r.get('reason')}" for r in acts))
            except Exception as e:  # noqa: BLE001
                log.warning("learning run failed: %s", e)

    def _learn_once(self):
        self.learn_runs += 1
        return self.learner.run_once()

    async def _selftest(self, after: float) -> None:
        await asyncio.sleep(after)
        res = await self.bot.selftest()
        ok = sum(1 for v in res.values() if v.get("ok"))
        log.info("TELEGRAM SELFTEST %d/%d ok: %s", ok, len(res), json.dumps(res, ensure_ascii=False))

    def request_stop(self) -> None:
        self.stop_event.set()

    async def run(self, seconds: float | None = None) -> int:
        cons = consistency(self.db)
        if not cons["ok"]:
            log.warning("database consistency problems at start: %s", cons["problems"])
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.request_stop)
        maint_stop = asyncio.Event()
        tasks = [asyncio.create_task(self.col.run(self.scanner.stream()), name="collector"),
                 asyncio.create_task(self.chk.run(self.src), name="safety"),
                 asyncio.create_task(self.eng.run(), name="signals"),
                 asyncio.create_task(self._learn_loop(), name="learning"),
                 asyncio.create_task(self.maint.run(maint_stop, initial_delay=min(60.0, self.s.maintenance_sec)),
                                     name="maintenance")]
        if self.uni:
            tasks.append(asyncio.create_task(self.uni.run(), name="universes"))
        if self.bot:
            tasks.append(asyncio.create_task(self.bot.run(), name="telegram"))
            st_after = float(os.environ.get("TELEGRAM_SELFTEST_AFTER_SEC") or 0)
            if st_after > 0:
                tasks.append(asyncio.create_task(self._selftest(st_after), name="tg-selftest"))
        log.info("SHADOW MEME running (PAPER ONLY)%s | Telegram %s | paused=%s", f" for {seconds:.0f}s" if seconds else "",
                 f"chat {self.bot.chat_id}" + (" (alerts dry-run)" if self.bot.dry_run else "") if self.bot else "OFF",
                 self.paused)
        try:
            if seconds:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            else:
                await self.stop_event.wait()
        finally:
            log.info("stopping...")
            if self.bot:
                await self.bot.flush(5)
                self.bot.stop()
                a = self.api
                log.info("telegram stats: %s | api calls %d errors %d retries %d 429s %d reachable %s | queue left %d",
                         json.dumps(dict(self.bot.stats), sort_keys=True), a.calls, a.errors, a.retries, a.rate_limited,
                         a.reachable, len(self.bot.queue))
            maint_stop.set()
            for x in (self.eng, self.chk, self.col, self.scanner) + ((self.uni,) if self.uni else ()):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(x.stop(), timeout=10)
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            await self.client.aclose()
            if self.bot:
                await self.api.close()
            with contextlib.suppress(Exception):
                self.db.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            log.info("stopped cleanly")
        return 0
