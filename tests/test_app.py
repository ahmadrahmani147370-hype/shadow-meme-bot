"""Phase 10: App runner robustness - log-only Telegram, HEALTH line, core-task watchdog. No network."""
import asyncio
import dataclasses
import json
import logging

from shadow.app import App
from shadow.config import load_settings
from shadow.telegram_api import LogOnlyAPI


async def idle(*a, **k):
    await asyncio.sleep(3600)


async def noop_stop(*a, **k):
    return None


def stub(app, crash=None):
    for name in ("col", "chk", "eng", "scanner", "uni", "maint"):
        x = getattr(app, name)
        if x is None:
            continue
        x.run = idle
        x.stop = noop_stop
    app._learn_loop = idle
    app._dashboard_loop = idle
    if crash:
        async def boom(*a, **k):
            raise RuntimeError(crash)
        app.col.run = boom


def mk(tmp_path, **env):
    s = load_settings(env={"DB_PATH": str(tmp_path / "a.db"), "TELEGRAM_LOG_ONLY": "true",
                           "TELEGRAM_POLL_TIMEOUT_SEC": "0", "TELEGRAM_SEND_INTERVAL_SEC": "0",
                           **{k.upper(): str(v) for k, v in env.items()}})
    return dataclasses.replace(s, health_log_sec=2)


def test_log_only_needs_no_token_and_sends_nothing_over_network(tmp_path, caplog):
    s = mk(tmp_path)
    assert s.telegram_bot_token == "" and s.tg_log_only

    async def go():
        app = App(s)
        stub(app)
        assert isinstance(app.api, LogOnlyAPI)
        t = asyncio.create_task(app.run(seconds=1.5))
        await asyncio.sleep(0.3)
        app.api.inject(app.bot.chat_id, "/status")
        app.api.inject(999, "/paper")
        code = await t
        return app, code
    with caplog.at_level(logging.INFO):
        app, code = asyncio.run(go())
    assert code == 0
    methods = [m for m, _ in app.api.sent]
    assert methods.count("sendMessage+button") == 1                     # startup message with dashboard button
    assert any(first.startswith("📟 STATUS") for _, first in app.api.sent)  # /status answered (log only)
    assert app.bot.stats["ignored_foreign"] == 1
    assert "[log-only sendMessage" in caplog.text


def test_health_line_has_resources_and_api_counters(tmp_path, caplog):
    s = mk(tmp_path)

    async def go():
        app = App(s, telegram=False)
        stub(app)
        return await app.run(seconds=3.5)
    with caplog.at_level(logging.INFO):
        assert asyncio.run(go()) == 0
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("HEALTH "))
    d = json.loads(line[7:])
    assert d["rss_mb"] > 5 and d["cpu_pct"] is not None and "api" in d and "db_mb" in d and "loop_lag_ms" in d
    assert d["paper"]["equity"] == 100.0 and set(d["tracked"]) == {"new", "mcap", "volume"}


def test_watchdog_exits_1_when_a_core_task_dies(tmp_path, caplog):
    s = mk(tmp_path)

    async def go():
        app = App(s, telegram=False)
        stub(app, crash="websocket parser bug")
        return await app.run(seconds=30)
    with caplog.at_level(logging.INFO):
        code = asyncio.run(go())
    assert code == 1
    assert "core task collector stopped (RuntimeError: websocket parser bug)" in caplog.text
    assert "stopped after a task failure (exit 1)" in caplog.text
