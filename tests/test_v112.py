"""v1.1.2: let winners run to +1000%, protect the profit (breakeven + tiered trailing stop + 50% partial at +30%).
Price paths are SYNTHETIC unit-test inputs, not market data."""
from types import SimpleNamespace

import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.exits import ExitPolicy, parse_tiers
from shadow.learning import Learner, simulate
from shadow.maintenance import consistency
from shadow.paper import PaperEngine
from shadow.paper_reset import archive_and_reset
from shadow.safety import PASS, SafetyReport
from shadow.signals import SignalEngine
from shadow.strategies import DEFAULT_PARAMS, LIMITS, load_strategies, migrate_v112, new_version
from shadow.telegram_bot import partial_text, sell_text, trade_text

pytestmark = pytest.mark.v11
NOW = 1_800_000_000.0
P = ExitPolicy(sl_pct=0)          # v1.1.2 rules with the strategy's own SL (v1.1.3's fixed 20% SL: test_v113.py)


def S(**env):
    return load_settings(env={"DB_PATH": "x.db", **{k.upper(): str(v) for k, v in env.items()}})


# ------------------------------------------------------------------ config + policy
def test_config_defaults_and_classic_rollback():
    s = S()
    assert s.exit_policy == "run" and s.exit_tp_pct == 1000 and s.exit_be_trigger_pct == 15
    assert s.exit_be_lock_pct == 2 and s.exit_partial_pct == 30 and s.exit_partial_frac == 0.5
    assert s.exit_trail_tiers == "50:10,100:15,300:25,inf:30" and s.exit_max_hold_h == 24
    assert ExitPolicy.from_settings(s).run and not ExitPolicy.from_settings(S(exit_policy="classic")).run
    assert parse_tiers("50:10,inf:30") == [(50.0, 10.0), (float("inf"), 30.0)]
    assert "TP +1000%" in P.summary() and "sell 50% at +30%" in P.summary()


def test_trail_widens_with_profit_and_breakeven_floor():
    assert [P.trail_for(g) for g in (20, 60, 150, 500)] == [10, 15, 25, 30]
    assert P.stop(1.0, 1.10, 0.9) == (0.9, "sl")                      # not protected yet: strategy SL
    st, kind = P.stop(1.0, 1.15, 0.9)                                 # +15%: protected, trail 10% of 1.15
    assert kind == "trail" and st == pytest.approx(1.035) and st >= 1.02   # (always above the BE floor here)
    wide = ExitPolicy(tiers=parse_tiers("inf:30"))
    assert wide.stop(1.0, 1.2, 0.9) == (pytest.approx(1.02), "breakeven")   # BE floor binds with a wide trail
    assert P.stop(1.0, 2.5, 0.9) == (pytest.approx(2.5 * 0.75), "trail")   # peak +150%: trail 25%
    assert P.stop(1.0, 5.0, 0.9) == (pytest.approx(3.5), "trail")          # peak +400%: trail 30%


def test_decide_never_cuts_a_winner_at_the_old_tp():
    # old early_entry TP was +30%: now +30% only triggers the 50% partial; the rest is not sold
    assert P.decide(1.0, 1.31, 1.31, 0.88, 1.3, 60, 20, False) == "partial"
    assert P.decide(1.0, 1.31, 1.31, 0.88, 1.3, 60, 20, True) is None
    assert P.decide(1.0, 5.0, 5.0, 0.88, 1.3, 60, 20, True) is None             # +400% keeps running
    assert P.decide(1.0, 11.0, 11.0, 0.88, 1.3, 60, 20, True) == "take_profit"  # +1000%
    assert P.decide(1.0, 0.87, 1.0, 0.88, 11, 60, 20, False) == "stop_loss"     # losers: SL unchanged
    assert P.decide(1.0, 1.03, 1.16, 0.88, 11, 60, 20, True) == "trailing_stop"      # 10% below a +16% peak
    assert ExitPolicy(tiers=parse_tiers("inf:30")).decide(1.0, 1.019, 1.2, 0.88, 11, 60, 20, True) == "breakeven_stop"
    assert P.decide(1.0, 3.4, 5.0, 0.88, 11, 60, 20, True) == "trailing_stop"
    # time stop only while unprotected; protected trades get max hold
    assert P.decide(1.0, 1.05, 1.08, 0.88, 11, 21 * 60, 20, False) == "time_stop"
    assert P.decide(1.0, 1.2, 1.25, 0.88, 11, 21 * 60, 20, True) is None
    assert P.decide(1.0, 1.2, 1.25, 0.88, 11, 25 * 3600, 20, True) == "max_hold"


def test_replay_partial_and_rest():
    pts = [[30, 1.1], [60, 1.3], [90, 2.0], [120, 2.5], [150, 1.8]]      # peak +150% -> trail 25% = 1.875
    g, r = P.replay(1.0, pts, 0.88, 20)
    assert r == "trailing_stop" and g == pytest.approx(0.5 * 30 + 0.5 * 80)
    assert P.replay(1.0, [[30, 1.05]], 0.88, 20) is None                  # undecided


# ------------------------------------------------------------------ paper engine
def _paper(tmp_path, **env):
    s = S(trade_min_mcap_usd=0, trade_min_liq_usd=0, trade_min_liq_mcap=0, **env)
    db = Database(str(tmp_path / "p.db"))
    t = {"now": NOW}
    msgs = []
    pe = PaperEngine(s, db, src=SimpleNamespace(get_market=lambda m: None), checker=None, clock=lambda: t["now"],
                     on_trade=lambda k, p: msgs.append((k, p)))
    rep = SafetyReport("A" * 44, NOW - 30, NOW + 900, 90, PASS, [], [], {"quote": {"sellable": True, "impact_pct": 1}},
                       quote_ts=NOW - 5)
    row = {"id": 1, "mint": "A" * 44, "symbol": "AAA", "universe": "mcap", "strategy": "first_pullback",
           "params_version": 1, "alert_type": "BUY_WATCH", "judge_verdict": "APPROVE", "judge_confidence": .7,
           "score": 80, "price": 1.0, "entry": 1.0, "sl": .9, "tp": 1.2, "time_stop_min": 60, "mcap": 2e6,
           "liquidity": 1e6}
    pid = pe.open_from_signal(row, rep, {})
    return s, db, pe, t, msgs, pid


def _tick(pe, t, price, dt=5):
    import asyncio
    t["now"] += dt
    pe.fast_prices["A" * 44] = (price, 1e6, "dexscreener", t["now"])
    return asyncio.run(pe.manage())


def test_paper_partial_then_trailing_close_accounting(tmp_path):
    s, db, pe, t, msgs, pid = _paper(tmp_path)
    size = pe.get(pid)["size_usd"]
    _tick(pe, t, 1.21)                       # old TP (+20%) - must NOT close
    assert pe.get(pid)["status"] == "open"
    _tick(pe, t, 1.32)                       # +32% -> 50% partial
    p = pe.get(pid)
    assert p["status"] == "open" and p["partial_ts"] is not None and p["partial_proceeds"] > 0
    assert [k for k, _ in msgs] == ["OPEN", "PARTIAL"]
    assert consistency(db)["ok"]
    _tick(pe, t, 3.0)                        # runs to +200%
    assert pe.get(pid)["status"] == "open"
    _tick(pe, t, 2.2)                        # trail 25% of 3.0 = 2.25 -> trailing stop
    p = pe.get(pid)
    assert p["status"] == "closed" and p["exit_reason"] == "trailing_stop"
    assert p["proceeds"] == pytest.approx(p["partial_proceeds"] + p["qty"] * p["exit_price"])
    assert p["pnl_usd"] == pytest.approx(p["proceeds"] - size - p["buy_fee"] - p["sell_fee"])
    assert p["pnl_usd"] > 0 and p["max_up_pct"] >= 199
    assert pe.account()["cash"] == pytest.approx(100 + p["pnl_usd"])
    assert consistency(db)["ok"]
    txt = sell_text(p)
    assert "Peak reached: +2" in txt and "sold earlier at +" in txt and "trailing stop" in txt
    assert "SELL 50% (partial profit)" in partial_text(msgs[1][1]) and "breakeven" in partial_text(msgs[1][1])
    assert "peak +" in trade_text("CLOSE", p) and "partial profit" in trade_text("PARTIAL", msgs[1][1])


def test_paper_profit_protected_after_15pct(tmp_path):
    s, db, pe, t, msgs, pid = _paper(tmp_path)
    _tick(pe, t, 1.16)
    _tick(pe, t, 1.01)                       # falls back: sold at the protected stop, not ridden into a loss
    p = pe.get(pid)
    assert p["status"] == "closed" and p["exit_reason"] == "trailing_stop" and p["partial_ts"] is None
    assert "Peak reached: +16" in sell_text(p)


def test_paper_stop_loss_and_signal_exit_rules(tmp_path):
    s, db, pe, t, msgs, pid = _paper(tmp_path)
    _tick(pe, t, 1.2)                                         # protected
    assert pe.close_by_signal(1, "dump_warning") is None      # winners: trailing stop decides
    assert pe.stats["sell_signal_ignored_protected"] == 1
    s, db, pe, t, msgs, pid = _paper(tmp_path / "b")
    _tick(pe, t, 0.81)
    assert pe.get(pid)["status"] == "open"                   # v1.1.3: -19% is inside the fixed 20% SL
    _tick(pe, t, 0.79)
    assert pe.get(pid)["exit_reason"] == "stop_loss"


def test_classic_policy_still_available(tmp_path):
    s, db, pe, t, msgs, pid = _paper(tmp_path, exit_policy="classic")
    _tick(pe, t, 1.21)
    assert pe.get(pid)["exit_reason"] == "take_profit"


def test_reset_with_partial_position(tmp_path):
    s, db, pe, t, msgs, pid = _paper(tmp_path)
    _tick(pe, t, 1.32)
    assert pe.get(pid)["partial_ts"] is not None
    res = archive_and_reset(db, 100.0, archive_dir=tmp_path / "arch", now=t["now"] + 60, label="era-t")
    assert res["trades_archived"] == 1 and res["closed_at_reset"] == 1 and res["wins"] == 1
    assert db.con.execute("SELECT partial_proceeds FROM paper_archive_positions").fetchone()[0] > 0
    assert consistency(db)["ok"]


# ------------------------------------------------------------------ watches + learning + migration
def test_watch_partial_and_blended_outcome(tmp_path):
    db = Database(str(tmp_path / "w.db"))
    sid = db.add_signal({"ts": NOW, "mint": "W" * 44, "universe": "new", "strategy": "early_entry",
                         "alert_type": "BUY_WATCH", "score": 70, "class": "WATCH", "params_version": 1, "entry": 1.0,
                         "sl": 0.88, "tp": 11.0, "time_stop_min": 20, "status": "open"})
    eng = SimpleNamespace(policy=P, stats=__import__("collections").Counter(), open={}, db=db, clock=lambda: NOW + 60)
    o = {"id": sid, "entry": 1.0, "sl": 0.88, "tp": 11.0, "ts": NOW, "time_stop_min": 20, "max_up": 35.0,
         "max_down": 0.0, "shadow": True}
    assert SignalEngine._run_exit(eng, o, 1.35, None, NOW + 60) == "partial" and o["banked"] == pytest.approx(17.5)
    o["max_up"] = 99.0
    assert SignalEngine._run_exit(eng, o, 1.84, None, NOW + 90) is None               # within 15% of the +99% peak
    assert SignalEngine._run_exit(eng, o, 1.6, None, NOW + 95) == "trailing_stop"
    eng.open[sid] = o
    SignalEngine._close(eng, o, 1.6, "trailing_stop", None)
    pnl = db.con.execute("SELECT pnl_pct FROM signals WHERE id=?", (sid,)).fetchone()[0]
    assert pnl == pytest.approx(17.5 + 0.5 * 60)


def test_learning_respects_tp_1000(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    lr = Learner(S(), db)
    assert lr.policy.run and "tp_pct" not in lr.tunable("early_entry")
    assert LIMITS["tp_pct"][0] >= 1000
    assert all(ps["tp_pct"] >= 1000 for d in DEFAULT_PARAMS.values() for ps in d["params"].values())
    ee = {**DEFAULT_PARAMS["early_entry"]["params"]["new"]}
    smp = {"entry": 1.0, "score": 80, "sl_dist_pct": 12,
           "path": [[30 * (i + 1), 1 + x / 100] for i, x in enumerate((10, 33, 80, 150, 80))]}
    r = simulate("early_entry", ee, smp, 0.0, P)
    assert r["reason"] == "trailing_stop" and r["net"] == pytest.approx(0.5 * 33 + 0.5 * 80)


def test_migrate_v112_raises_learned_tp_and_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    load_strategies(db)
    db.meta_set("migrated_v112", "0")
    cur = db.active_strategy_params("early_entry")
    params = {u: {**ps, "tp_pct": 30.0, "min_score": 66} for u, ps in cur["params"].items()}
    v = new_version(db, "early_entry", params, "learned", "test")
    assert migrate_v112(db) == ["early_entry"]
    st = load_strategies(db)["early_entry"]
    assert st.version == v + 1 and all(ps["tp_pct"] == 1000 and ps["min_score"] == 66 for ps in st.params.values())
    up = db.strategy_updates()[0]
    assert up["kind"] == "param_change" and "v1.1.2" in up["reason"]
    assert migrate_v112(db) == [] and load_strategies(db)["early_entry"].version == v + 1
