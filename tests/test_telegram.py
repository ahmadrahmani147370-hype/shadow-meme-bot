"""Phase 8 Telegram: mocked Bot API (httpx.MockTransport). No real network."""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

import httpx
import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.judge import JudgeVerdict
from shadow.telegram_api import TelegramAPI, TelegramError, split_text
from shadow.telegram_bot import COMMANDS, AlertLimiter, TelegramBot, buy_watch_text
from shadow import timeutil

TOKEN = "123456789:AAFakeTokenForTestsOnly_xxxxxxxxxxxxxx"
CHAT = "7922990181"
PKT = timezone(timedelta(hours=5))


class FakeTG:
    """Scriptable Telegram server. `fail` = list of exceptions/status codes consumed before success."""

    def __init__(self):
        self.calls = []
        self.fail = []
        self.updates = []
        self.next_id = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = path.rsplit("/", 1)[-1]
        ctype = request.headers.get("content-type", "")
        body = json.loads(request.content or b"{}") if "json" in ctype else {"_multipart": request.content}
        self.calls.append({"method": method, "url": str(request.url), "host": request.url.host, "path": path, "body": body})
        if self.fail:
            f = self.fail.pop(0)
            if isinstance(f, Exception):
                raise f
            if f == 429:
                return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                                 "parameters": {"retry_after": 3}})
            return httpx.Response(f, json={"ok": False, "error_code": f, "description": f"err {f}"})
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "Fathersigna147lBot"}})
        self.next_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})

    def sent(self, method="sendMessage"):
        return [c for c in self.calls if c["method"] == method]


class Sleeps:
    def __init__(self):
        self.d = []

    async def __call__(self, x):
        self.d.append(x)
        await asyncio.sleep(0)


def mk_api(fake, sleeps=None, base="https://api.telegram.org", **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return TelegramAPI(TOKEN, base, client=client, sleep=sleeps or Sleeps(), rand=lambda: 0.5, **kw)


def mk_bot(tmp_path, fake=None, app=None, clock=None, **env):
    s = load_settings(env={"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_SEND_INTERVAL_SEC": "0",
                           **{k.upper(): str(v) for k, v in env.items()}})
    db = Database(str(tmp_path / "t.db"))
    fake = fake or FakeTG()
    api = mk_api(fake)
    bot = TelegramBot(s, db, api, app=app, clock=clock or (lambda: 1_800_000_000.0), sleep=Sleeps())
    return bot, fake, db, s


async def drain(bot, rounds=200):
    """Run the sender until the queue is empty (or rounds exhausted)."""
    t = asyncio.create_task(bot.sender_loop())
    for _ in range(rounds):
        if not bot.queue:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    bot.stop()
    await asyncio.wait_for(t, 2)


def run(c):
    return asyncio.run(c)


class FakeApp:
    def __init__(self):
        self.paused = False

    def set_paused(self, v):
        self.paused = v

    def status(self):
        return {"scanner_mode": "ws", "new_tokens": 5, "tracked_new": 3, "equity": 100.0, "cash": 98.0, "n_open": 1,
                "realized_pnl": 0.0, "db_mb": 1.5, "strategies": {"early_entry": 2}}

    def scanner_status(self):
        return {"mode": "ws", "uptime_s": 600, "new_tokens": 5, "new_pumpportal": 5, "seen_in_db": 9, "tracked": 3}


# ------------------------------------------------------------------ config
def test_config_defaults_and_overrides():
    s = load_settings(env={})
    assert s.telegram_chat_id == "7922990181" and s.telegram_api_base == "https://api.telegram.org"
    assert s.tg_startup_message is True and s.tg_alerts_dry_run is False
    s = load_settings(env={"TELEGRAM_API_BASE": "https://tg.mirror.example/", "TELEGRAM_ALERTS_DRYRUN": "yes",
                           "TELEGRAM_HOLD_ALERTS": "off", "TELEGRAM_CHAT_ID": "42"})
    assert s.telegram_api_base == "https://tg.mirror.example" and s.tg_alerts_dry_run and not s.tg_hold_alerts
    assert s.telegram_chat_id == "42"


# ------------------------------------------------------------------ API client
def test_api_base_override_and_token_path():
    fake = FakeTG()
    api = mk_api(fake, base="https://tg.mirror.example/")
    run(api.send_message(CHAT, "hi"))
    c = fake.calls[0]
    assert c["host"] == "tg.mirror.example" and c["path"] == f"/bot{TOKEN}/sendMessage"
    assert c["body"]["chat_id"] == CHAT and c["body"]["text"] == "hi"


def test_backoff_on_connect_timeout_then_success():
    fake, sl = FakeTG(), Sleeps()
    fake.fail = [httpx.ConnectTimeout("timed out"), httpx.ConnectTimeout("timed out"), httpx.ConnectError("refused")]
    api = mk_api(fake, sl)
    run(api.send_message(CHAT, "x"))
    assert len(fake.calls) == 4 and api.reachable is True and api.fail_streak == 0
    assert sl.d == [4.0, 8.0, 16.0]            # 2*2^n with jitter factor 1.0 (rand 0.5)
    assert api.retries == 3


def test_connect_timeout_exhausted_raises_without_token(caplog):
    fake = FakeTG()
    fake.fail = [httpx.ConnectTimeout(f"timeout https://api.telegram.org/bot{TOKEN}/sendMessage")] * 10
    api = mk_api(fake, max_retries=2)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TelegramError) as ei:
            run(api.send_message(CHAT, "x"))
    assert TOKEN not in str(ei.value) and TOKEN not in api.last_error and TOKEN not in caplog.text
    assert api.reachable is False and api.fail_streak == 3


def test_429_honours_retry_after_and_4xx_not_retried():
    fake, sl = FakeTG(), Sleeps()
    fake.fail = [429]
    api = mk_api(fake, sl)
    run(api.send_message(CHAT, "x"))
    assert sl.d == [3.5] and api.rate_limited == 1
    fake.fail = [400]
    with pytest.raises(TelegramError) as ei:
        run(api.send_message(CHAT, "x"))
    assert ei.value.code == 400 and len(fake.sent()) == 3


def test_5xx_retried():
    fake = FakeTG()
    fake.fail = [502, 503]
    api = mk_api(fake)
    run(api.send_message(CHAT, "x"))
    assert len(fake.calls) == 3


def test_split_text_long_messages():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    parts = split_text(text)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts) and "\n".join(parts) == text
    assert split_text("y" * 9000) == ["y" * 4096, "y" * 4096, "y" * 808]


def test_send_document_multipart():
    fake = FakeTG()
    api = mk_api(fake)
    run(api.send_document(CHAT, "trades.csv", b"a,b\n1,2\n", "cap"))
    c = fake.sent("sendDocument")[0]
    assert b'filename="trades.csv"' in c["body"]["_multipart"] and b"7922990181" in c["body"]["_multipart"]


# ------------------------------------------------------------------ auth + commands
def test_only_allowed_chat_is_obeyed(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path)
    r = run(bot.handle_update({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/status"}}))
    assert r is None and not bot.queue and bot.stats["ignored_foreign"] == 1
    r = run(bot.handle_update({"update_id": 2, "message": {"chat": {"id": 7922990181}, "text": "/help@Fathersigna147lBot"}}))
    assert r == "help" and len(bot.queue) == 1 and "/export" in bot.queue[0].text


def test_every_command_replies(tmp_path):
    app = FakeApp()
    bot, fake, db, s = mk_bot(tmp_path, app=app)
    for cmd in COMMANDS:
        bot.queue.clear()
        run(bot.handle_update({"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": f"/{cmd}"}}))
        assert bot.queue, cmd
        assert not any("mein error" in (o.text or "") for o in bot.queue), (cmd, bot.queue[0].text)
    assert app.paused is False   # /pause then /resume


def test_pause_resume_and_unknown(tmp_path):
    app = FakeApp()
    bot, fake, db, s = mk_bot(tmp_path, app=app)
    run(bot.run_command("pause"))
    assert app.paused and "PAUSED" in bot.queue[-1].text
    bot.on_signal({"alert_type": "BUY_WATCH", "mint": "M" * 44, "universe": "new"})
    assert bot.stats["paused_skip"] == 1 and len(bot.queue) == 1
    run(bot.run_command("resume"))
    assert not app.paused
    run(bot.run_command("nope"))
    assert "maloom nahi" in bot.queue[-1].text


def test_selftest_dispatches_without_sending(tmp_path):
    app = FakeApp()
    bot, fake, db, s = mk_bot(tmp_path, app=app)
    res = run(bot.selftest())
    assert all(v["ok"] for v in res.values()), res
    assert set(COMMANDS) <= set(res) and res["foreign_chat_ignored"]["ok"]
    assert not bot.queue and not fake.calls


def test_export_sends_csv_documents(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path)
    run(bot.run_command("export"))
    run(drain(bot))
    docs = fake.sent("sendDocument")
    assert len(docs) == 6
    names = b" ".join(d["body"]["_multipart"] for d in docs)
    for n in (b"trades.csv", b"daily_report.csv", b"signals.csv", b"judge.csv", b"strategy_updates.csv", b"equity.csv"):
        assert n in names
    assert "EXPORT" in fake.sent()[0]["body"]["text"]


# ------------------------------------------------------------------ alerts
def bw_row(**kw):
    r = {"alert_type": "BUY_WATCH", "mint": "Mint" + "x" * 40, "symbol": "DOGE2", "universe": "new",
         "strategy": "early_entry", "params_version": 3, "score": 78, "class": "WATCH", "price": 0.0012, "mcap": 50000,
         "liquidity": 12000, "entry": 0.0012, "sl": 0.00105, "tp": 0.00156, "time_stop_min": 20,
         "detectors_json": json.dumps({"volume_spike": {}, "buy_pressure": {}}), "safety_verdict": "PASS", "safety_score": 85}
    r.update(kw)
    return r


def test_buy_watch_alert_has_judge_verdict_and_reasons(tmp_path):
    v = JudgeVerdict("VETO", 0.81, [{"code": "low_liquidity", "kind": "hard", "detail": "liq $12k < $15k"},
                                     {"code": "ok", "kind": "info", "detail": "fine"}])
    t = buy_watch_text(bw_row(), v)
    assert "BUY WATCH" in t and "VETO" in t and "low_liquidity: liq $12k < $15k" in t and "PAPER ONLY" in t
    assert "SL" in t and "-12.5%" in t and "+30.0%" in t and "volume_spike" in t
    t = buy_watch_text(bw_row(), JudgeVerdict("APPROVE", 0.7, []))
    assert "APPROVE" in t and "no objections" in t
    bot, fake, db, s = mk_bot(tmp_path)
    bot.on_signal(bw_row(), v)
    run(drain(bot))
    assert "BUY WATCH" in fake.sent()[0]["body"]["text"] and fake.sent()[0]["body"]["chat_id"] == CHAT


def test_sell_hold_cooldowns_reject_digest_and_flood():
    clock = {"t": 1000.0}
    s = load_settings(env={"TELEGRAM_MAX_ALERTS_PER_MIN": "5"})
    lim = AlertLimiter(s, lambda: clock["t"])
    sell = {"alert_type": "SELL", "mint": "A", "strategy": "x"}
    assert lim.allow(sell) and not lim.allow(sell)
    assert lim.allow({**sell, "strategy": "y"})                 # other strategy = other key
    clock["t"] += 601
    assert lim.allow(sell)
    hold = {"alert_type": "HOLD", "mint": "B"}
    assert lim.allow(hold) and not lim.allow(hold)
    clock["t"] += 30 * 60 + 1
    assert lim.allow(hold)
    for i in range(30):
        assert not lim.allow({"alert_type": "REJECT", "mint": f"R{i}", "symbol": "RUG", "reason": "safety FAIL: mint authority"})
    for i in range(10):
        lim.allow({"alert_type": "BUY_WATCH", "mint": f"F{i}"})
    assert lim.suppressed["BUY_WATCH_flood"] == 6          # 1 HOLD already in this minute
    assert lim.digest() is None                                  # < 60 min since start? (started at t=1000)
    clock["t"] += 3600
    d = lim.digest()
    assert "30 reject" in d and "30× safety FAIL" in d and "RUG 30" in d and "BUY_WATCH_flood 6" in d
    assert lim.digest(force=True) is None                        # nothing new


def test_paper_open_close_alerts(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path)
    p = {"id": 7, "mint": "M" * 44, "symbol": "CAT", "universe": "mcap", "strategy": "breakout", "size_usd": 2.0,
         "entry_price": 1.03, "market_price": 1.0, "entry_slippage_pct": 3.0, "buy_fee": 0.0, "sl": 0.9, "tp": 1.3}
    bot.on_trade("OPEN", p)
    bot.on_trade("CLOSE", {**p, "exit_reason": "tp", "exit_price": 1.28, "exit_market_price": 1.3,
                           "exit_slippage_pct": 1.5, "pnl_usd": 0.49, "pnl_pct": 24.5})
    run(drain(bot))
    texts = [c["body"]["text"] for c in fake.sent()]
    assert "PAPER OPEN #7" in texts[0] and "PAPER CLOSE #7" in texts[1] and "$0.49" in texts[1]


def test_dry_run_alerts_are_not_sent_but_replies_are(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path, telegram_alerts_dryrun="true")
    bot.on_signal(bw_row(), None)
    bot.on_trade("OPEN", {"id": 1, "mint": "M" * 44})
    assert not bot.queue and len(bot.dry_log) == 2
    run(bot.run_command("help"))
    assert len(bot.queue) == 1


def test_alert_bug_never_raises_into_engine(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path)
    bot.on_signal({"alert_type": "BUY_WATCH", "mint": "x", "universe": "new", "detectors_json": "{bad"}, None)
    bot.on_trade("OPEN", {})                                     # missing keys
    assert not bot.queue


# ------------------------------------------------------------------ strategy updates
def add_update(db, n=1):
    return [db.add_strategy_update("param_change", "early_entry", i, i + 1, {"tp_pct": [30, 33]}, "better on test set",
                                   {"before": {"version": i, "score": 0.5, "n": 40}, "after": {"version": i + 1, "score": 1.4, "n": 40}})
            for i in range(1, n + 1)]


def test_strategy_updates_sent_and_marked_only_after_delivery(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path)
    ids = add_update(db, 2)
    fake.fail = [httpx.ConnectTimeout("t")] * 6                  # first message fails all its retries
    assert bot.queue_strategy_updates() == 2
    assert bot.queue_strategy_updates() == 0                     # not queued twice
    t_before = len(db.strategy_updates(unsent_only=True))
    assert t_before == 2

    async def go():
        t = asyncio.create_task(bot.sender_loop())
        for _ in range(500):
            if not bot.queue:
                break
            await asyncio.sleep(0)
        bot.stop()
        await t
    run(go())
    assert db.strategy_updates(unsent_only=True) == []
    texts = [c["body"]["text"] for c in fake.sent() if "STRATEGY UPDATE" in c["body"].get("text", "")]
    assert len(set(texts)) == 2 and len(texts) == 8                # 6 failed attempts recorded + 2 delivered
    t = texts[0]
    assert "tp_pct 30 → 33" in t and "better on test set" in t and "v1 → v2" in t and "on 40" in t
    assert bot.stats["send_retry"] >= 1 and ids


def test_dry_run_leaves_updates_unsent(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path, telegram_alerts_dryrun="1")
    add_update(db, 1)
    bot.queue_strategy_updates()
    bot.queue_strategy_updates()
    assert len(bot.dry_log) == 1 and len(db.strategy_updates(unsent_only=True)) == 1


# ------------------------------------------------------------------ daily report
def test_daily_report_at_pkt_midnight(tmp_path):
    timeutil.set_timezone("Asia/Karachi")
    t = {"t": datetime(2026, 11, 13, 23, 59, 30, tzinfo=PKT).timestamp()}
    bot, fake, db, s = mk_bot(tmp_path, clock=lambda: t["t"])
    assert bot.check_daily_report() is False                     # first run: just remembers yesterday
    assert db.meta_get("tg_daily_report_day") == "2026-11-12"
    assert bot.check_daily_report() is False                     # 23:59:30 on the 13th: nothing yet
    t["t"] = datetime(2026, 11, 14, 0, 0, 5, tzinfo=PKT).timestamp()
    assert bot.check_daily_report() is True
    assert bot.check_daily_report() is False                     # queued once
    assert "DAILY REPORT 13 Nov 2026" in bot.queue[-1].text
    run(drain(bot))
    assert db.meta_get("tg_daily_report_day") == "2026-11-13"
    assert "DAILY REPORT" in fake.sent()[-1]["body"]["text"]


# ------------------------------------------------------------------ polling
def test_poll_skips_backlog_obeys_owner_and_handles_409(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path, telegram_poll_timeout_sec="0")
    fake.updates = [{"update_id": 50, "message": {"chat": {"id": int(CHAT)}, "text": "/paper"}}]   # old backlog

    async def go():
        t = asyncio.create_task(bot.poll_loop())
        await asyncio.sleep(0.5)
        assert bot.offset == 51 and not bot.queue                 # backlog skipped, not answered
        fake.updates = [{"update_id": 51, "message": {"chat": {"id": 1}, "text": "/status"}},
                        {"update_id": 52, "message": {"chat": {"id": int(CHAT)}, "text": "/positions"}}]
        await asyncio.sleep(0.5)
        assert bot.offset == 53 and len(bot.queue) == 1 and bot.stats["ignored_foreign"] == 1
        fake.fail = [409]
        await asyncio.sleep(0.5)
        assert bot.stats["conflict_409"] >= 1
        bot.stop()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    run(go())
    gu = [c for c in fake.calls if c["method"] == "getUpdates"]
    assert gu[0]["body"]["offset"] == -1 and gu[1]["body"]["offset"] == 51


def test_unreachable_telegram_never_blocks_the_engine(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path)
    fake.fail = [httpx.ConnectTimeout("t")] * 1000
    import time as _t

    async def go():
        t = asyncio.create_task(bot.sender_loop())
        t0 = _t.perf_counter()
        for i in range(400):
            bot.on_trade("OPEN", {"id": i, "mint": "M" * 44})
            await asyncio.sleep(0)
        took = _t.perf_counter() - t0
        bot.stop()
        await asyncio.wait_for(t, 2)
        return took
    took = run(go())
    assert took < 2 and len(bot.queue) == s.tg_queue_max and bot.stats["dropped"] >= 100
    assert bot.api.reachable is False


def test_redact_filter_and_httpx_quiet(tmp_path, capsys):
    from shadow.logging_setup import RedactFilter, setup_logging
    s = load_settings(env={"TELEGRAM_BOT_TOKEN": TOKEN, "LOG_DIR": str(tmp_path / "logs")})
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        setup_logging(s)
        logging.getLogger("x").warning("url https://api.telegram.org/bot%s/getMe", TOKEN)
        assert logging.getLogger("httpx").level >= logging.WARNING
        for h in root.handlers:
            h.flush()
        txt = (tmp_path / "logs" / "shadow.log").read_text()
        assert TOKEN not in txt and "<redacted>" in txt
        assert TOKEN not in capsys.readouterr().err
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
    f = RedactFilter([TOKEN])
    rec = logging.LogRecord("a", 20, "", 0, "t=%s", (TOKEN,), None)
    f.filter(rec)
    assert TOKEN not in rec.getMessage()


def test_startup_message(tmp_path):
    app = FakeApp()
    bot, fake, db, s = mk_bot(tmp_path, app=app)
    t = bot.startup_text()
    assert "SHADOW MEME bot online (paper-only)" in t and "PAPER_ONLY=true" in t


def test_bot_run_sends_startup_then_stops(tmp_path):
    bot, fake, db, s = mk_bot(tmp_path, telegram_poll_timeout_sec="0")

    async def go():
        t = asyncio.create_task(bot.run())
        for _ in range(200):
            if fake.sent():
                break
            await asyncio.sleep(0.005)
        bot.stop()
        await asyncio.wait_for(t, 3)
    run(go())
    assert "online (paper-only)" in fake.sent()[0]["body"]["text"]
    assert all(TOKEN in c["url"] for c in fake.calls)


# ------------------------------------------------------------------ app + main
def test_main_exits_2_without_token(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(main, "load_settings", lambda: load_settings(env={"LOG_DIR": str(tmp_path / "l"),
                                                                         "DB_PATH": str(tmp_path / "d.db")}))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert main.main([]) == 2
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_app_pause_persists_and_blocks_new_paper_trades(tmp_path):
    from shadow.app import App
    from shadow.db import WriterLocked
    s = load_settings(env={"DB_PATH": str(tmp_path / "a.db"), "TELEGRAM_BOT_TOKEN": TOKEN})

    async def go():
        app = App(s, api=mk_api(FakeTG()))
        assert app.bot is not None and app.bot.chat_id == CHAT and not app.paused
        app.set_paused(True)
        ok, why = app.paper.can_open({"alert_type": "BUY_WATCH", "judge_verdict": "APPROVE", "score": 90}, None)
        assert not ok and "paused" in why
        st = app.status()
        assert st["n_open"] == 0 and "early_entry" in st["strategies"]
        assert "mode" in app.scanner_status()
        with pytest.raises(WriterLocked):
            App(s)
        await app.client.aclose()
        app.db.release_writer_lock() if hasattr(app.db, "release_writer_lock") else None
        app.db.close()
    run(go())
    db = Database(str(tmp_path / "a.db"))
    assert db.meta_get("paused") == "1"
