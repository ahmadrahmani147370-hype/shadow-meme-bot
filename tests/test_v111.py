"""v1.1.1: paper reset with archive, BUY + SELL pushes, daily coin minimum (pace controller)."""
import asyncio
import csv
import gzip
import sqlite3
from types import SimpleNamespace

import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.limits import DailyCap, PaceController
from shadow.models import MarketData
from shadow.paper import PaperEngine
from shadow.paper_reset import archive_and_reset
from shadow.safety import PASS, SafetyReport
from shadow.signals import SignalEngine, Sources
from shadow.telegram_bot import TelegramBot, sell_text
from shadow.timeutil import day_start

pytestmark = pytest.mark.v11
NOW = 1_800_000_000.0
MID = day_start(NOW)                 # PKT midnight before NOW


def S(**env):
    return load_settings(env={"DB_PATH": "x.db", **{k.upper(): str(v) for k, v in env.items()}})


# ------------------------------------------------------------------ paper reset
def _paper(tmp_path):
    s = S(trade_min_mcap_usd=0, trade_min_liq_usd=0, trade_min_liq_mcap=0)
    db = Database(str(tmp_path / "r.db"))
    t = {"now": NOW}
    pe = PaperEngine(s, db, src=SimpleNamespace(get_market=lambda m: None), checker=None, clock=lambda: t["now"])
    rep = lambda m: SafetyReport(m, t["now"] - 30, t["now"] + 900, 90, PASS, [], [],  # noqa: E731
                                 {"quote": {"sellable": True, "impact_pct": 1}}, quote_ts=t["now"] - 5)
    row = lambda m, i: {"id": i, "mint": m, "symbol": m[:3], "universe": "mcap", "strategy": "first_pullback",  # noqa: E731
                        "params_version": 1, "alert_type": "BUY_WATCH", "judge_verdict": "APPROVE",
                        "judge_confidence": .7, "score": 80, "price": 1.0, "entry": 1.0, "sl": .9, "tp": 1.2,
                        "time_stop_min": 60, "mcap": 2e6, "liquidity": 1e5}
    return s, db, pe, rep, row, t


def test_reset_archives_everything_and_restarts_at_100(tmp_path):
    s, db, pe, rep, row, t = _paper(tmp_path)
    a, b = "A" * 44, "B" * 44
    pa = pe.open_from_signal(row(a, 1), rep(a), {})
    pe.close(pe.get(pa), 0.8, "stop_loss")
    pb = pe.open_from_signal(row(b, 2), rep(b), {})
    db.con.execute("UPDATE paper_positions SET last_price=1.1 WHERE id=?", (pb,))
    db.con.commit()
    db.con.execute("INSERT INTO paper_equity (ts, cash, open_value, equity, n_open, realized_pnl) VALUES (?,?,?,?,?,?)",
                   (NOW, 96, 2, 98, 1, -0.4))
    db.con.commit()
    old_cash = pe.account()["cash"]
    res = archive_and_reset(db, 100.0, archive_dir=tmp_path / "arch", now=NOW + 60, label="era-test")
    assert res["trades_archived"] == 2 and res["closed_at_reset"] == 1 and res["wins"] == 1
    assert res["old_cash"] > old_cash                                   # the open one was credited at its last mark
    # fresh account
    acc = pe.account()
    assert acc["cash"] == 100 and acc["start_balance"] == 100 and acc["realized_pnl"] == 0
    assert pe.open_positions() == [] and db.con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0
    # history kept, honestly marked
    arch = [dict(r) for r in db.con.execute("SELECT * FROM paper_archive_positions ORDER BY id")]
    assert [r["era"] for r in arch] == ["era-test"] * 2
    assert arch[0]["exit_reason"] == "stop_loss" and arch[1]["exit_reason"] == "reset_archived"
    assert arch[1]["exit_price"] == pytest.approx(1.1)                     # last mark, no invented fill
    assert db.con.execute("SELECT COUNT(*) FROM paper_archive_equity").fetchone()[0] == 1
    r = dict(db.con.execute("SELECT * FROM paper_resets").fetchone())
    assert r["n_trades"] == 2 and r["new_balance"] == 100 and r["era"] == "era-test"
    assert db.meta_get("paper_era") == "era-test"
    # backup + CSV files exist and are readable
    with gzip.open(res["backup"]) as f:
        raw = f.read()
    p = tmp_path / "b.db"
    p.write_bytes(raw)
    assert sqlite3.connect(p).execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 2
    rows = list(csv.reader(open(res["csv"][0])))
    assert rows[0][:2] == ["era", "archived_ts"] and len(rows) == 3
    from shadow.maintenance import consistency
    assert consistency(db)["ok"]
    db.close()


def test_reset_twice_keeps_both_eras(tmp_path):
    s, db, pe, rep, row, t = _paper(tmp_path)
    a = "A" * 44
    pe.close(pe.get(pe.open_from_signal(row(a, 1), rep(a), {})), 1.1, "take_profit")
    archive_and_reset(db, 100, archive_dir=tmp_path / "a", now=NOW, label="e1", backup=False)
    pe.close(pe.get(pe.open_from_signal(row(a, 2), rep(a), {})), 0.9, "stop_loss")
    archive_and_reset(db, 50, archive_dir=tmp_path / "a", now=NOW + 1, label="e2", backup=False)
    eras = [r[0] for r in db.con.execute("SELECT era FROM paper_archive_positions ORDER BY archived_ts")]
    assert eras == ["e1", "e2"] and pe.account()["cash"] == 50
    db.close()


# ------------------------------------------------------------------ Telegram BUY + SELL
class API:
    reachable = True
    calls = errors = retries = rate_limited = 0

    def redact(self, s):
        return s


def test_trades_mode_pushes_buy_and_sell_only(tmp_path):
    s = load_settings(env={"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "7922990181"})
    assert s.tg_notify == "trades"
    db = Database(str(tmp_path / "t.db"))
    bot = TelegramBot(s, db, API(), clock=lambda: NOW)
    for a in ("BUY_WATCH", "SELL", "HOLD", "REJECT", "SHADOW"):
        bot.on_signal({"alert_type": a, "mint": "M" * 44, "symbol": "MM", "universe": "mcap", "score": 80}, None)
    assert not bot.queue
    pos = {"id": 3, "mint": "M" * 44, "symbol": "MM", "universe": "mcap", "strategy": "first_pullback", "size_usd": 2,
           "entry_price": 1.0, "sl": .92, "tp": 1.1, "open_ts": NOW - 1500}
    bot.on_trade("OPEN", pos)
    bot.on_trade("CLOSE", dict(pos, exit_price=1.1, pnl_usd=0.19, pnl_pct=9.6, exit_reason="take_profit",
                               exit_ts=NOW, exit_slippage_pct=0.4))
    bot.on_trade("CLOSE", dict(pos, id=4, exit_price=0.5, pnl_usd=-1.0, pnl_pct=-50.0, exit_reason="liquidity_gone",
                               exit_ts=NOW))
    texts = [q.text for q in bot.queue]
    assert len(texts) == 3 and texts[0].startswith("🟢 BUY")
    assert texts[1].startswith("🔴 SELL") and "PROFIT" in texts[1] and "take_profit" in texts[1] and "+9.6%" in texts[1]
    assert "held 25 min" in texts[1]
    assert "LOSS" in texts[2] and "liquidity_gone" in texts[2] and "-50.0%" in texts[2]
    db.add_strategy_update("param_change", "early_entry", 1, 2, {}, "x", {})
    bot.queue_strategy_updates()
    db.meta_set("tg_daily_report_day", "2000-01-01")
    bot.check_daily_report()
    bot.check_digest()
    assert len(bot.queue) == 3                                        # nothing else pushed
    assert "push: BUY + SELL only" in bot.cmd_status()
    db.close()


def test_sell_text_has_pnl_and_reason():
    t = sell_text({"id": 1, "mint": "Q" * 44, "universe": "volume", "strategy": "momentum_breakout", "entry_price": 2,
                   "exit_price": 1.8, "pnl_usd": -0.2, "pnl_pct": -10, "exit_reason": "stop_loss", "size_usd": 2})
    assert "🔴 SELL" in t and "-10.0%" in t and "stop_loss" in t and "PAPER" in t.upper()


def test_notify_setting_values():
    assert S(telegram_notify="buy").tg_notify == "buy" and S(telegram_notify="all").tg_notify == "all"
    assert S(telegram_notify="nonsense").tg_notify == "trades"


# ------------------------------------------------------------------ pace controller
def pace_at(tmp_path, now, used=0, cap=1000, minimum=500):
    db = Database(str(tmp_path / "pc.db"))
    t = {"now": now}
    c = DailyCap(db, cap, clock=lambda: t["now"])
    for i in range(used):
        c.admit(f"m{i}")
    return db, t, c, PaceController(c, minimum, [750_000, 250_000, 100_000, 50_000, 25_000, 10_000, 5_000], 10,
                                         clock=lambda: t["now"])


def test_pace_lowers_eval_floor_then_widens_when_behind(tmp_path):
    db, t, c, pc = pace_at(tmp_path, MID + 12 * 3600, used=220)         # noon: target 250, 30 behind
    assert pc.target() == pytest.approx(250) and pc.behind() and pc.floor == 750_000
    floors = []
    for _ in range(8):
        pc.update()
        floors.append(pc.floor)
        t["now"] += 61
    assert floors[:6] == [250_000, 100_000, 50_000, 25_000, 10_000, 5_000] and pc.widen
    n = sum(pc.allow_fill() for _ in range(30))
    assert n == 10                                                      # PACE_FILL_PER_MIN
    t["now"] += 61
    assert pc.allow_fill()
    db.close()


def test_pace_stops_filling_at_target_and_recovers_floor(tmp_path):
    db, t, c, pc = pace_at(tmp_path, MID + 2.4 * 3600, used=80)         # target 50 at 02:24, already 80 (>= +25)
    assert not pc.behind() and not pc.allow_fill()
    pc.tier = 3
    pc.widen = True
    for _ in range(3):
        pc.update()
        t["now"] += 61
    assert pc.tier == 0 and not pc.widen and pc.floor == 750_000
    db.close()


def test_pace_never_exceeds_max_and_min_capped_by_max(tmp_path):
    db, t, c, pc = pace_at(tmp_path, MID + 23 * 3600, used=5, cap=5, minimum=500)
    assert pc.min == 5 and not pc.behind() and not pc.allow_fill()
    db.close()


def test_projection(tmp_path):
    db, t, c, pc = pace_at(tmp_path, MID + 23 * 3600, used=0, minimum=500)
    for i in range(10):                                                  # 10 normal admissions in the last hour
        c.admit(f"x{i}")
    t["now"] += 3600 - 1
    assert pc.projection() == 10                                         # behind, no fills working: honest base
    pc.allow_fill()
    assert pc.projection() == 10                  # v1.1.3: fills alone no longer claim the minimum is reached
    db.close()


# ------------------------------------------------------------------ engine: pace fill for evaluation only
class Col:
    def __init__(self, mds):
        self.mds = mds
        self.tracked = {m: SimpleNamespace(market=md) for m, md in mds.items()}

    def tracked_mints(self):
        return list(self.tracked)

    def get_market(self, m):
        return self.mds.get(m)

    def get_candles(self, m, minutes=60):
        return []


def test_pace_fill_evaluates_small_coins_but_never_trades_them(tmp_path):
    mds = {f"s{i}": MarketData(f"s{i}", price_usd=1, market_cap=60_000, liquidity_usd=20_000) for i in range(15)}
    db = Database(str(tmp_path / "e.db"))
    t = {"now": MID + 12 * 3600}
    cap = DailyCap(db, 1000, clock=lambda: t["now"])
    pace = PaceController(cap, 500, [750_000, 250_000, 100_000, 50_000], 10, clock=lambda: t["now"])
    eng = SignalEngine(S(), db, Sources(Col(mds)), checker=None, judge=None, clock=lambda: t["now"], cap=cap, pace=pace)
    assert not eng.eval_allowed("s0", mds["s0"])                        # floor still 750k
    for _ in range(3):
        pace.update()
        t["now"] += 61
    assert pace.floor == 50_000
    ok = [m for m in mds if eng.eval_allowed(m, mds[m])]
    assert len(ok) == 10 and cap.used == 10 and eng.stats["pace_fill"] == 10      # 10/min
    assert eng.eval_allowed("s0", mds["s0"])                            # admitted today -> stays evaluable
    ev = SimpleNamespace(features={"mcap": 60_000, "liquidity": 20_000, "age_min": 60}, score=99)
    assert eng.trade_gate(ev)[0] == "silent"                            # trading floor untouched
    db.close()


def test_widened_discovery_uses_more_free_sources(tmp_path):
    from shadow.universes import UniverseCollector
    calls = []
    u = UniverseCollector(S(), Database(str(tmp_path / "u.db")))

    async def fake_get(limiter, url, params=None):
        calls.append((limiter, url, (params or {}).get("page")))
        return None
    u._get = fake_get

    async def nov():
        return {}
    u.validate_once = nov
    asyncio.run(u.discover_once())
    normal = len(calls)
    calls.clear()
    u.widen = True
    asyncio.run(u.discover_once())
    # v1.2: 5 GT lists x 3 pages + 3 DexScreener lists always; widened adds new_pools (3 pages)
    assert normal == 5 * 3 + 3 and len(calls) == 6 * 3 + 3
    assert any("new_pools" in c[1] for c in calls) and any("token-profiles/latest" in c[1] for c in calls)


def test_v113_far_behind_jumps_to_lowest_floor_and_honest_projection(tmp_path):
    db, t, c, pc = pace_at(tmp_path, MID + 12 * 3600, used=20)          # noon: target 250, 230 behind
    pc.update()
    assert pc.floor == 5_000 and pc.widen and pc.stats["floor_jump"] == 1   # no 1-tier-per-minute ramp
    for i in range(60):                                  # 60 admissions in the last hour (the first 20 are older)
        t["now"] += 61
        c.admit(f"y{i}")
    left_h = (MID + 86400 - t["now"]) / 3600
    assert pc.rate_per_h() == 60 and pc.projection() == int(80 + 60 * left_h)
    db.close()


def test_v113_recent_admissions_survive_restart(tmp_path):
    db, t, c, pc = pace_at(tmp_path, MID + 12 * 3600, used=0)
    for i in range(30):
        t["now"] += 60
        c.admit(f"z{i}")
    c2 = DailyCap(db, 1000, clock=lambda: t["now"])
    assert c2.used == 30 and len(c2.recent) == 30
    db.close()
