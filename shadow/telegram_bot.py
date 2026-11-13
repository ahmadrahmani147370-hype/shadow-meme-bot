"""PHASE 8 - Telegram control + alerts (paper only).

Only TELEGRAM_CHAT_ID (default 7922990181) is obeyed; every other chat is ignored and counted.

Commands: /start /status /scanner /newtokens /signals /paper /positions /history /report /strategies
          /pause /resume /export /help
Alerts:   BUY WATCH (+ judge verdict and reasons), SELL / HOLD (per-coin cooldowns), REJECT (digest only),
          paper trade OPEN / CLOSE, strategy_update (every self-learning change, sent until delivered),
          daily report at 00:00 PKT (TIMEZONE).

Design: everything outgoing goes through a bounded queue drained by its own task. If Telegram is
unreachable (ConnectTimeout etc.) the sender backs off and retries; the scanner, signals and paper
trading never wait for Telegram. Strategy updates are marked sent only after Telegram accepted them.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from dataclasses import dataclass, field

from . import reports
from .config import Settings
from .db import Database
from .telegram_api import TelegramAPI, TelegramError
from .timeutil import day_key, day_start, fmt, tz_name

PHASE = 8
IMPLEMENTED = True
log = logging.getLogger("shadow.telegram")

COMMANDS = {
    "start": "bot shuru / welcome",
    "status": "bot ka haal: scanner, signals, paper, Telegram",
    "scanner": "scanner stats (naye coins, sources)",
    "newtokens": "aakhri 10 naye tokens",
    "signals": "aakhri 10 signals (BUY WATCH / SELL) + judge",
    "paper": "paper account: balance, equity, P&L",
    "positions": "khuli paper positions",
    "history": "band hui aakhri 10 paper trades",
    "report": "aaj ki report (PKT)",
    "strategies": "strategies, versions, self-learning changes",
    "pause": "naye paper trades + signal alerts band (scanning chalti rahe gi)",
    "resume": "dobara shuru",
    "export": "CSV files bhejo (trades, daily, signals, judge, updates, equity)",
    "help": "commands ki list",
}
ICON = {"BUY_WATCH": "🟢", "SELL": "🔴", "HOLD": "⏸️", "REJECT": "⛔"}
PAPER_TAG = "📄 PAPER ONLY - asli paisa nahi"


def _usd(x) -> str:
    return "-" if x is None else (f"${x:,.2f}" if abs(x) >= 0.01 or x == 0 else f"${x:.6g}")


def _px(x) -> str:
    return "-" if x is None else f"{x:.6g}"


def _short(m: str) -> str:
    return f"{m[:4]}…{m[-4:]}" if m and len(m) > 10 else (m or "?")


@dataclass
class Out:
    kind: str                     # text | doc
    text: str = ""
    filename: str = ""
    content: bytes = b""
    tag: str = ""                 # alert type / reply / update
    on_sent: object = None        # callback after Telegram accepted it
    tries: int = 0
    ts: float = field(default_factory=time.time)


# ====================================================================== alert formatting
def buy_watch_text(row: dict, verdict=None) -> str:
    dets = list(json.loads(row.get("detectors_json") or "{}"))
    lines = [f"🟢 BUY WATCH - {row.get('symbol') or '?'} [{row.get('universe')}]",
             f"Strategy {row.get('strategy')} v{row.get('params_version') or '?'} | score {row.get('score') or 0:.0f} "
             f"{row.get('class') or ''}",
             f"Price {_px(row.get('price'))} | MCAP {_usd(row.get('mcap'))} | Liq {_usd(row.get('liquidity'))}"]
    e, sl, tp = row.get("entry"), row.get("sl"), row.get("tp")
    if e:
        lines.append(f"Entry {_px(e)} | SL {_px(sl)} ({(sl / e - 1) * 100:+.1f}%) | TP {_px(tp)} ({(tp / e - 1) * 100:+.1f}%)"
                     f" | time stop {row.get('time_stop_min') or '-'} min" if sl and tp else f"Entry {_px(e)}")
    if row.get("safety_verdict"):
        lines.append(f"Safety {row['safety_verdict']} {row.get('safety_score') or ''}/100")
    if dets:
        lines.append("Detectors: " + ", ".join(dets))
    if verdict is not None:
        mark = "✅ APPROVE" if verdict.verdict == "APPROVE" else f"🚫 {verdict.verdict}"
        lines.append(f"Judge: {mark} (confidence {verdict.confidence:.2f})")
        rs = [r for r in verdict.reasons if r.get("kind") in ("hard", "soft")] or \
             [r for r in verdict.reasons if r.get("kind") == "info"][:3]
        for r in rs[:6]:
            lines.append(f" • [{r.get('kind')}] {r.get('code')}: {r.get('detail')}")
        if not rs:
            lines.append(" • koi aitraz nahi (no objections)")
        lines.append("Paper trade: " + ("khulega agar rules allow karein" if verdict.verdict == "APPROVE"
                                        else "NAHI (judge veto, sirf track ho ga)"))
    lines += [f"Mint: {row.get('mint')}", f"https://dexscreener.com/solana/{row.get('mint')}", PAPER_TAG]
    return "\n".join(lines)


def signal_text(row: dict) -> str:
    a = row.get("alert_type")
    return (f"{ICON.get(a, '-')} {a} - {row.get('symbol') or '?'} [{row.get('universe')}] {row.get('strategy') or ''}\n"
            f"{row.get('reason') or ''}\nPrice {_px(row.get('price'))} | Mint {_short(row.get('mint'))}\n{PAPER_TAG}")


def trade_text(kind: str, p: dict) -> str:
    if kind == "OPEN":
        return (f"💰 PAPER OPEN #{p['id']} - {p.get('symbol') or _short(p['mint'])} [{p.get('universe')}]\n"
                f"Strategy {p.get('strategy')} | size {_usd(p.get('size_usd'))} @ {_px(p.get('entry_price'))} "
                f"(market {_px(p.get('market_price'))}, slippage {p.get('entry_slippage_pct') or 0:.2f}%, fee "
                f"{_usd(p.get('buy_fee'))})\nSL {_px(p.get('sl'))} | TP {_px(p.get('tp'))}\n{PAPER_TAG}")
    pnl = p.get("pnl_usd") or 0
    return (f"{'✅' if pnl > 0 else '❌'} PAPER CLOSE #{p['id']} - {p.get('symbol') or _short(p['mint'])} "
            f"[{p.get('universe')}]\nWhy: {p.get('exit_reason')} | exit {_px(p.get('exit_price'))} (market "
            f"{_px(p.get('exit_market_price'))}, slippage {p.get('exit_slippage_pct') or 0:.2f}%)\n"
            f"P&L after fees {_usd(pnl)} ({p.get('pnl_pct') or 0:+.1f}%)\n{PAPER_TAG}")


# ====================================================================== rate limiting
class AlertLimiter:
    """BUY WATCH always (the engine has its own cooldown) but under a global per-minute cap; SELL / HOLD per
    coin cooldowns; REJECT only counted for a digest."""

    def __init__(self, s: Settings, clock=time.time):
        self.s, self.clock = s, clock
        self.last: dict[tuple, float] = {}
        self.window: collections.deque = collections.deque()
        self.suppressed = collections.Counter()
        self.rejects: list[dict] = []
        self.last_digest = clock()

    def allow(self, row: dict) -> bool:
        a, now = row.get("alert_type"), self.clock()
        if a == "REJECT":
            self.rejects.append({"mint": row.get("mint"), "symbol": row.get("symbol"), "reason": row.get("reason") or "",
                                 "universe": row.get("universe")})
            del self.rejects[:-2000]
            return False
        if a == "HOLD" and not self.s.tg_hold_alerts:
            self.suppressed[a] += 1
            return False
        if a in ("SELL", "HOLD"):
            key = (a, row.get("mint"), row.get("strategy") if a == "SELL" else None)
            cd = (self.s.tg_sell_cooldown_min if a == "SELL" else self.s.tg_hold_cooldown_min) * 60
            if now - self.last.get(key, -1e18) < cd:
                self.suppressed[a] += 1
                return False
            self.last[key] = now
        while self.window and now - self.window[0] > 60:
            self.window.popleft()
        if len(self.window) >= self.s.tg_max_alerts_per_min:
            self.suppressed[f"{a}_flood"] += 1
            return False
        self.window.append(now)
        if len(self.last) > 5000:
            cut = now - 86400
            self.last = {k: v for k, v in self.last.items() if v > cut}
        return True

    def digest(self, force: bool = False) -> str | None:
        """REJECT digest + count of rate-limited alerts, at most once per TELEGRAM_REJECT_DIGEST_MIN."""
        every = self.s.tg_reject_digest_min * 60
        now = self.clock()
        if not every or (not force and now - self.last_digest < every):
            return None
        rj, sup = self.rejects, dict(self.suppressed)
        self.rejects, self.suppressed, self.last_digest = [], collections.Counter(), now
        if not rj and not sup:
            return None
        mins = max(1, round(every / 60))
        lines = [f"⛔ REJECT digest - pichle {mins} min: {len(rj)} reject"]
        why = collections.Counter(r["reason"].split(":")[0].split(" (")[0][:60] for r in rj)
        for w, n in why.most_common(5):
            lines.append(f" • {n}× {w}")
        coins = collections.Counter(r["symbol"] or _short(r["mint"]) for r in rj)
        if coins:
            lines.append("Coins: " + ", ".join(f"{c} {n}" for c, n in coins.most_common(8)))
        if sup:
            lines.append("Rate-limited (nahi bheje): " + ", ".join(f"{k} {v}" for k, v in sorted(sup.items())))
        return "\n".join(lines)


# ====================================================================== bot
class TelegramBot:
    def __init__(self, s: Settings, db: Database, api: TelegramAPI | None, app=None, *, clock=time.time,
                 sleep=asyncio.sleep):
        self.s, self.db, self.api, self.app = s, db, api, app
        self.clock, self._sleep = clock, sleep
        self.chat_id = str(s.telegram_chat_id or "7922990181").strip()
        self.dry_run = s.tg_alerts_dry_run
        self.limiter = AlertLimiter(s, clock)
        self.queue: collections.deque[Out] = collections.deque()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self.offset: int | None = None
        self.inflight_updates: set[int] = set()
        self.dry_log: collections.deque = collections.deque(maxlen=500)
        self.sent_log: collections.deque = collections.deque(maxlen=200)
        self.stats = collections.Counter()
        self.conflict = False
        self.started_at = clock()

    # ------------------------------------------------------------ outgoing
    def enqueue(self, item: Out) -> None:
        if len(self.queue) >= self.s.tg_queue_max:
            # drop the oldest non-essential message first
            for i, q in enumerate(self.queue):
                if q.tag not in ("update", "daily", "reply"):
                    del self.queue[i]
                    break
            else:
                self.queue.popleft()
            self.stats["dropped"] += 1
        self.queue.append(item)
        self._wake.set()

    def send_text(self, text: str, tag: str = "reply", on_sent=None) -> None:
        self.enqueue(Out("text", text=text, tag=tag, on_sent=on_sent))

    def alert(self, text: str, tag: str) -> None:
        """Alerts honour TELEGRAM_ALERTS_DRYRUN (logged only)."""
        if self.dry_run:
            self.dry_log.append((self.clock(), tag, text))
            self.stats[f"dry_{tag}"] += 1
            log.info("[dry-run alert %s] %s", tag, text.splitlines()[0] if text else "")
            return
        self.send_text(text, tag)

    async def _deliver(self, item: Out) -> None:
        if item.kind == "doc":
            await self.api.send_document(self.chat_id, item.filename, item.content, item.text)
        else:
            await self.api.send_message(self.chat_id, item.text, retries=2)

    async def sender_loop(self) -> None:
        streak = 0
        while not self._stop.is_set():
            if not self.queue:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            item = self.queue[0]
            try:
                await self._deliver(item)
                self.queue.popleft()
                streak = 0
                self.stats["sent"] += 1
                self.stats[f"sent_{item.tag}"] += 1
                self.sent_log.append((self.clock(), item.tag, (item.text or item.filename).splitlines()[0][:80]))
                if item.on_sent:
                    try:
                        item.on_sent()
                    except Exception as e:  # noqa: BLE001
                        log.warning("on_sent callback failed: %s", e)
                await self._sleep(self.s.tg_send_interval_sec)
            except TelegramError as e:
                if e.code and 400 <= e.code < 500 and e.code != 429:
                    self.queue.popleft()               # bad request: retrying will not help
                    self.stats["send_rejected"] += 1
                    log.warning("telegram refused a message (%s): dropped", e)
                    continue
                streak += 1
                self.stats["send_retry"] += 1
                await self._sleep(self.api.backoff(min(streak, 8)) if self.api else 5)
            except Exception as e:  # noqa: BLE001  never let the sender die
                streak += 1
                log.warning("telegram sender error: %s", self.api.redact(str(e)) if self.api else e)
                await self._sleep(5)

    # ------------------------------------------------------------ hooks from the engine
    @property
    def paused(self) -> bool:
        return bool(self.app and getattr(self.app, "paused", False))

    def on_signal(self, row: dict, verdict=None) -> None:
        try:
            a = row.get("alert_type")
            if self.paused and a != "REJECT":
                self.stats["paused_skip"] += 1
                return
            if not self.limiter.allow(row):
                return
            self.alert(buy_watch_text(row, verdict) if a == "BUY_WATCH" else signal_text(row), a)
        except Exception as e:  # noqa: BLE001  an alert bug must never break the signal engine
            log.warning("alert formatting failed: %s", e)

    def on_trade(self, kind: str, pos: dict) -> None:
        try:
            self.alert(trade_text(kind, pos), f"paper_{kind.lower()}")
        except Exception as e:  # noqa: BLE001
            log.warning("trade alert failed: %s", e)

    # ------------------------------------------------------------ periodic jobs
    def queue_strategy_updates(self) -> int:
        n = 0
        for uid, text in reports.pending_strategy_updates(self.db):
            if uid in self.inflight_updates:
                continue
            if self.dry_run:
                self.inflight_updates.add(uid)      # logged once; stays unsent in the DB for the real bot
                self.alert("🧠 STRATEGY UPDATE\n" + text, "update")
                continue
            self.inflight_updates.add(uid)
            self.send_text("🧠 STRATEGY UPDATE\n" + text, "update", on_sent=self._mark_sent(uid))
            n += 1
        return n

    def _mark_sent(self, uid: int):
        def f():
            reports.mark_strategy_updates_sent(self.db, [uid])
            self.inflight_updates.discard(uid)
        return f

    def check_daily_report(self, now: float | None = None) -> bool:
        """At/after 00:00 local time send the report for the day that just ended (once; survives restarts)."""
        now = now or self.clock()
        yday = day_key(day_start(now) - 1)
        last = self.db.meta_get("tg_daily_report_day")
        if last is None:
            self.db.meta_set("tg_daily_report_day", yday)   # first run: no report for days before the bot existed
            return False
        if last >= yday or getattr(self, "_dry_daily", None) == yday:
            return False
        text = reports.report_text(self.db, self.s, now=day_start(now) - 1)
        if self.dry_run:
            self._dry_daily = yday
            self.alert(text, "daily")
            return True
        self.send_text(text, "daily", on_sent=lambda: self.db.meta_set("tg_daily_report_day", yday))
        self._dry_daily = yday            # do not queue twice while it is waiting to be delivered
        return True

    def check_digest(self) -> None:
        d = self.limiter.digest()
        if d:
            self.alert(d, "digest")

    async def jobs_loop(self) -> None:
        while not self._stop.is_set():
            for job in (self.queue_strategy_updates, self.check_daily_report, self.check_digest):
                try:
                    job()
                except Exception as e:  # noqa: BLE001
                    log.warning("telegram job %s failed: %s", job.__name__, e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------ incoming
    def allowed(self, msg: dict) -> bool:
        return str((msg.get("chat") or {}).get("id")) == self.chat_id

    async def handle_update(self, upd: dict) -> str | None:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return None
        if not self.allowed(msg):
            self.stats["ignored_foreign"] += 1
            log.info("ignored message from chat %s (not TELEGRAM_CHAT_ID)", (msg.get("chat") or {}).get("id"))
            return None
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            self.send_text("Commands ke liye /help likhein.")
            return "help-hint"
        cmd = text.split()[0][1:].split("@")[0].lower()
        self.stats["commands"] += 1
        self.stats[f"cmd_{cmd}"] += 1
        await self.run_command(cmd, text)
        return cmd

    async def run_command(self, cmd: str, text: str = "") -> None:
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            self.send_text(f"'/{cmd}' maloom nahi. /help dekhein.")
            return
        try:
            out = fn()
            if asyncio.iscoroutine(out):
                out = await out
        except Exception as e:  # noqa: BLE001
            log.exception("command /%s failed", cmd)
            out = f"⚠️ /{cmd} mein error: {type(e).__name__}: {str(e)[:200]}"
        if isinstance(out, str):
            self.send_text(out)

    async def skip_backlog(self) -> None:
        """Do not answer commands that were sent while the bot was off (start fresh)."""
        try:
            ups = await self.api.get_updates(offset=-1, timeout=0)
            if ups:
                self.offset = ups[-1]["update_id"] + 1
                self.stats["backlog_skipped"] += 1
        except TelegramError as e:
            log.warning("could not read Telegram backlog: %s", e)

    async def poll_loop(self) -> None:
        await self.skip_backlog()
        streak = 0
        while not self._stop.is_set():
            try:
                ups = await self.api.get_updates(self.offset, timeout=self.s.tg_poll_timeout_sec)
                streak = 0
                if self.conflict:
                    log.info("Telegram polling conflict cleared")
                self.conflict = False
                for u in ups or []:
                    self.offset = u["update_id"] + 1
                    try:
                        await self.handle_update(u)
                    except Exception as e:  # noqa: BLE001
                        log.warning("update handling failed: %s", e)
                self.stats["polls"] += 1
                await asyncio.sleep(0.01 if ups else 0.2)    # always yield (a mirror might answer instantly)
            except TelegramError as e:
                streak += 1
                if e.code == 409:
                    if not self.conflict:
                        log.error("Telegram 409: another program is polling this bot token (stop the old bot). "
                                  "Alerts still go out; commands wait.")
                    self.conflict = True
                    self.stats["conflict_409"] += 1
                    await self._sleep(30)
                elif e.code == 401:
                    log.error("Telegram 401: TELEGRAM_BOT_TOKEN is wrong/revoked. Commands disabled.")
                    self.stats["unauthorized"] += 1
                    await self._sleep(300)
                else:
                    self.stats["poll_errors"] += 1
                    await self._sleep(self.api.backoff(min(streak, 8)))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                streak += 1
                log.warning("poll error: %s", self.api.redact(str(e)))
                await self._sleep(min(60, 2 * streak))

    # ------------------------------------------------------------ lifecycle
    def startup_text(self) -> str:
        from . import __version__
        return (f"🟢 SHADOW MEME bot online (paper-only)\nVersion {__version__} | PAPER_ONLY=true, koi wallet/private "
                f"key nahi\nTime {fmt(self.clock(), '%d %b %Y %H:%M')} {tz_name()}\n"
                f"Status: {'⏸️ PAUSED' if self.paused else '▶️ running'} | /help for commands")

    async def run(self) -> None:
        if self.s.tg_startup_message:
            self.send_text(self.startup_text(), "startup")
        tasks = [asyncio.create_task(self.sender_loop(), name="tg-send"),
                 asyncio.create_task(self.poll_loop(), name="tg-poll"),
                 asyncio.create_task(self.jobs_loop(), name="tg-jobs")]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def flush(self, timeout: float = 10) -> None:
        end = time.monotonic() + timeout
        while self.queue and time.monotonic() < end:
            await asyncio.sleep(0.2)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ================================================================ commands
    def cmd_start(self) -> str:
        return ("👋 SHADOW MEME - Solana meme coin scanner + PAPER trading signals.\n"
                "Asli trading NAHI: koi wallet / private key nahi, sab paper (virtual) hai.\n\n" + self.cmd_help())

    def cmd_help(self) -> str:
        return "Commands:\n" + "\n".join(f"/{k} - {v}" for k, v in COMMANDS.items())

    def cmd_status(self) -> str:
        st = self.app.status() if self.app and hasattr(self.app, "status") else {}
        up = int(self.clock() - self.started_at)
        lines = [f"📟 STATUS - {'⏸️ PAUSED' if self.paused else '▶️ running'} | PAPER ONLY",
                 f"Uptime {up // 3600}h {up % 3600 // 60}m | time {fmt(self.clock(), '%d %b %H:%M')} {tz_name()}"]
        if st:
            lines.append(f"Scanner {st.get('scanner_mode')} | naye coins {st.get('new_tokens', 0)} | tracked: new "
                         f"{st.get('tracked_new', 0)}, mcap {st.get('tracked_mcap', 0)}, volume {st.get('tracked_volume', 0)}")
            lines.append(f"Safety checked {st.get('safety_checked', 0)} | signals: BUY WATCH {st.get('buy_watch', 0)} "
                         f"(approve {st.get('approve', 0)} / veto {st.get('veto', 0)}) SELL {st.get('sell', 0)} "
                         f"REJECT {st.get('reject', 0)}")
            if "equity" in st:
                lines.append(f"Paper: equity {_usd(st['equity'])} | cash {_usd(st['cash'])} | open {st['n_open']} | "
                             f"realized {_usd(st['realized_pnl'])}")
            lines.append(f"DB {st.get('db_mb', 0):.1f} MB | strategies " + ", ".join(
                f"{k} v{v}" for k, v in (st.get("strategies") or {}).items()))
        a = self.api
        link = "OK" if a and a.reachable else ("unreachable, retrying" if a and a.reachable is False else "starting")
        lines.append(f"Telegram {link} | queue {len(self.queue)} | sent {self.stats['sent']} | ignored chats "
                     f"{self.stats['ignored_foreign']}" + (" | ⚠️ 409 conflict" if self.conflict else "")
                     + (" | alerts DRY-RUN" if self.dry_run else ""))
        return "\n".join(lines)

    def cmd_scanner(self) -> str:
        st = self.app.scanner_status() if self.app and hasattr(self.app, "scanner_status") else {}
        if not st:
            return "Scanner abhi start nahi hua."
        lines = [f"🛰️ SCANNER - mode {st.get('mode')} | uptime {st.get('uptime_s', 0) // 60} min",
                 f"Naye tokens {st.get('new_tokens', 0)} | migrations {st.get('migrations', 0)} | duplicates "
                 f"{st.get('duplicates', 0)} | DB mein {st.get('seen_in_db', 0)}"]
        for k in sorted(st):
            if k.startswith("new_") and k != "new_tokens":
                lines.append(f"  source {k[4:]}: {st[k]}")
        if st.get("tracked") is not None:
            lines.append(f"Tracked: new {st['tracked']} | mcap {st.get('mcap', 0)} | volume {st.get('volume', 0)}")
        if st.get("last_errors"):
            lines.append("Last errors: " + "; ".join(str(e)[:80] for e in st["last_errors"][-3:]))
        return "\n".join(lines)

    def cmd_newtokens(self) -> str:
        rows = self.db.recent(10)
        if not rows:
            return "Abhi koi naya token nahi mila."
        now = self.clock()
        lines = ["🆕 NEW TOKENS (aakhri 10)"]
        for r in rows:
            age = (now - (r.get("launch_ts") or r.get("first_seen_ts") or now)) / 60
            lines.append(f"• {r.get('symbol') or '?'} ({r.get('name') or ''})"[:60] + f" | {r.get('status')} | "
                         f"{r.get('source')} | {age:.0f} min\n  {r['mint']}")
        return "\n".join(lines)

    def cmd_signals(self) -> str:
        rows = [dict(r) for r in self.db.con.execute(
            "SELECT * FROM signals WHERE alert_type IN ('BUY_WATCH','SELL') ORDER BY ts DESC LIMIT 10")]
        nrej = self.db.con.execute("SELECT COUNT(*) FROM signals WHERE alert_type='REJECT' AND ts>=?",
                                   (day_start(self.clock()),)).fetchone()[0]
        if not rows:
            return f"Abhi koi BUY WATCH / SELL signal nahi. (aaj REJECT: {nrej})"
        lines = ["📡 SIGNALS (aakhri 10)"]
        for r in rows:
            j = f" | judge {r['judge_verdict']} {r['judge_confidence'] or 0:.2f}" if r.get("judge_verdict") else ""
            extra = (f" | score {r['score'] or 0:.0f}{j}" if r["alert_type"] == "BUY_WATCH" else f" | {r.get('reason') or ''}")
            res = f" → {r['exit_reason']} {r['pnl_pct']:+.1f}%" if r.get("pnl_pct") is not None else ""
            lines.append(f"{fmt(r['ts'], '%H:%M')} {ICON.get(r['alert_type'])} {r['alert_type']} {r.get('symbol') or '?'} "
                         f"[{r['universe']}] {r.get('strategy') or ''}{extra}{res}"[:300])
        lines.append(f"Aaj REJECT: {nrej} (digest har {self.s.tg_reject_digest_min:.0f} min)")
        return "\n".join(lines)

    def cmd_paper(self) -> str:
        return reports.paper_text(self.db, self.s, self.clock())

    def cmd_positions(self) -> str:
        return reports.positions_text(self.db, self.clock())

    def cmd_history(self) -> str:
        return reports.history_text(self.db, 10)

    def cmd_report(self) -> str:
        return reports.report_text(self.db, self.s, self.clock())

    def cmd_strategies(self) -> str:
        return reports.strategies_text(self.db, self.s)

    def cmd_pause(self) -> str:
        if self.app is None or not hasattr(self.app, "set_paused"):
            return "Pause abhi available nahi."
        self.app.set_paused(True)
        return ("⏸️ PAUSED: naye paper trades aur signal alerts band. Scanning, data aur khuli positions ka "
                "management (SL/TP) chalta rahe ga. /resume se dobara shuru.")

    def cmd_resume(self) -> str:
        if self.app is None or not hasattr(self.app, "set_paused"):
            return "Resume abhi available nahi."
        self.app.set_paused(False)
        return "▶️ RESUMED: paper trades aur alerts dobara on."

    def cmd_export(self) -> str:
        from . import export
        now = self.clock()
        files = {"trades.csv": export.trades_csv(self.db), "daily_report.csv": export.daily_csv(self.db),
                 "signals.csv": export.signals_csv(self.db), "judge.csv": export.judge_csv(self.db),
                 "strategy_updates.csv": export.strategy_updates_csv(self.db), "equity.csv": export.equity_csv(self.db)}
        stamp = fmt(now, "%Y%m%d-%H%M")
        sent = []
        for name, text in files.items():
            rows = max(0, text.count("\n") - 1)
            fname = f"shadow-{stamp}-{name}"
            self.enqueue(Out("doc", text=f"{name}: {rows} rows (times {tz_name()})", filename=fname,
                             content=text.encode("utf-8"), tag="export"))
            sent.append(f"{name} ({rows})")
        return "📎 EXPORT: " + ", ".join(sent) + " - files aa rahi hain."

    # ------------------------------------------------------------ selftest (live run, no messages sent)
    async def selftest(self) -> dict:
        """Dispatch every command (+ a foreign chat) through the real handlers with the live DB/app state, capturing
        replies instead of sending them. Used in the live test so only the startup message goes out."""
        cap: list[Out] = []
        real_enqueue = self.enqueue
        self.enqueue = lambda item: cap.append(item)      # type: ignore[method-assign]
        res = {}
        paused0 = self.paused
        try:
            for cmd in COMMANDS:
                cap.clear()
                t0 = time.perf_counter()
                await self.handle_update({"update_id": 0, "message": {"chat": {"id": int(self.chat_id)}, "text": f"/{cmd}"}})
                ok = bool(cap) and not any("error" in (o.text or "")[:40] for o in cap if o.kind == "text")
                res[cmd] = {"ok": ok, "ms": round((time.perf_counter() - t0) * 1000, 1), "messages": len(cap),
                            "first_line": (cap[0].text.splitlines()[0] if cap and cap[0].text else "")[:90]}
            cap.clear()
            ign0 = self.stats["ignored_foreign"]
            await self.handle_update({"update_id": 0, "message": {"chat": {"id": 12345}, "text": "/status"}})
            res["foreign_chat_ignored"] = {"ok": not cap and self.stats["ignored_foreign"] == ign0 + 1}
        finally:
            self.enqueue = real_enqueue                   # type: ignore[method-assign]
            if self.app and hasattr(self.app, "set_paused"):
                self.app.set_paused(paused0)
        return res


__all__ = ["TelegramBot", "AlertLimiter", "buy_watch_text", "signal_text", "trade_text", "COMMANDS", "Out"]
