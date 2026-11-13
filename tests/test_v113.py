"""v1.1.3: stop loss exactly -20% from entry for every strategy, locked against self-learning.
v1.1.2 profit protection / trail / partial / TP 1000% unchanged. Price paths are SYNTHETIC test inputs."""
import asyncio
from types import SimpleNamespace

import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.exits import ExitPolicy
from shadow.learning import Learner, simulate
from shadow.paper import PaperEngine
from shadow.safety import PASS, SafetyReport
from shadow.strategies import DEFAULT_PARAMS, LIMITS, TUNABLE, load_strategies, migrate_v113, new_version

pytestmark = pytest.mark.v11
NOW = 1_800_000_000.0
P = ExitPolicy()


def S(**env):
    return load_settings(env={"DB_PATH": "x.db", **{k.upper(): str(v) for k, v in env.items()}})


def test_defaults_sl_20_everywhere():
    assert S().exit_sl_pct == 20 and P.sl_pct == 20 and "SL -20%" in P.summary()
    for name, d in DEFAULT_PARAMS.items():
        for u, ps in d["params"].items():
            assert ps["sl_pct"] == 20.0, (name, u)
            assert ps.get("max_sl_pct", 20.0) == 20.0
    # v1.1.2 rules unchanged
    assert P.tp_pct == 1000 and P.be_trigger_pct == 15 and P.partial_pct == 30 and P.partial_frac == 0.5
    assert [P.trail_for(g) for g in (20, 60, 150, 500)] == [10, 15, 25, 30]


def test_levels_exactly_minus_20_for_every_strategy(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    for name, st in load_strategies(db).items():
        for u in st.universes:
            lv = st.levels(u, 2.0, {"pullback_low": 1.9})
            assert lv["sl"] == pytest.approx(1.6), (name, u)
    db.close()


def test_policy_enforces_20_whatever_the_stored_sl():
    assert P.stop(1.0, 1.05, 0.88) == (pytest.approx(0.8), "sl")       # an old 12% SL row -> 20%
    assert P.decide(1.0, 0.85, 1.0, 0.88, 11, 60, 20, False) is None    # -15%: still open
    assert P.decide(1.0, 0.80, 1.0, 0.95, 11, 60, 20, False) == "stop_loss"
    assert P.decide(1.0, 1.03, 1.16, 0.88, 11, 60, 20, True) == "trailing_stop"   # protection unchanged


def test_paper_open_stores_sl_20_and_sells_at_minus_20(tmp_path):
    s = S(trade_min_mcap_usd=0, trade_min_liq_usd=0, trade_min_liq_mcap=0)
    db = Database(str(tmp_path / "p.db"))
    t = {"now": NOW}
    pe = PaperEngine(s, db, src=SimpleNamespace(get_market=lambda m: None), checker=None, clock=lambda: t["now"])
    rep = SafetyReport("A" * 44, NOW - 30, NOW + 900, 90, PASS, [], [], {"quote": {"sellable": True, "impact_pct": 1}},
                       quote_ts=NOW - 5)
    row = {"id": 1, "mint": "A" * 44, "symbol": "AAA", "universe": "mcap", "strategy": "momentum_breakout",
           "params_version": 1, "alert_type": "BUY_WATCH", "judge_verdict": "APPROVE", "judge_confidence": .7,
           "score": 80, "price": 1.0, "entry": 1.0, "sl": .92, "tp": 11, "time_stop_min": 60, "mcap": 2e6,
           "liquidity": 1e6}
    pid = pe.open_from_signal(row, rep, {})
    assert pe.get(pid)["sl"] == pytest.approx(0.8)
    for px in (0.9, 0.81, 0.79):
        t["now"] += 5
        pe.fast_prices["A" * 44] = (px, 1e6, "dexscreener", t["now"])
        asyncio.run(pe.manage())
    p = pe.get(pid)
    assert p["status"] == "closed" and p["exit_reason"] == "stop_loss" and p["exit_market_price"] == 0.79
    db.close()


def test_learning_cannot_change_sl(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    lr = Learner(S(), db)
    for name in TUNABLE:
        keys = lr.tunable(name)
        assert "sl_pct" not in keys and "max_sl_pct" not in keys and "tp_pct" not in keys
        assert "min_score" in keys
    # classic exits too: the SL lock is separate from the exit policy
    assert "sl_pct" not in Learner(S(exit_policy="classic"), db).tunable("early_entry")
    assert "sl_pct" in Learner(S(exit_sl_pct=0, exit_policy="classic"), db).tunable("early_entry")
    smp = {"entry": 1.0, "score": 80, "sl_dist_pct": 5, "path": [[30, 0.9], [60, 0.85], [90, 0.79]]}
    r = simulate("first_pullback", DEFAULT_PARAMS["first_pullback"]["params"]["new"], smp, 0.0, P)
    assert r["reason"] == "stop_loss" and r["net"] == pytest.approx(-21)
    db.close()


def test_migrate_v113_sets_sl_20_keeps_learned_params_and_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    load_strategies(db)
    db.meta_set("migrated_v113", "0")
    cur = db.active_strategy_params("first_pullback")
    params = {u: {**ps, "sl_pct": 8.0, "max_sl_pct": 8.0, "min_score": 67} for u, ps in cur["params"].items()}
    v = new_version(db, "first_pullback", params, "learned", "test")
    assert migrate_v113(db) == ["first_pullback"]
    st = load_strategies(db)["first_pullback"]
    assert st.version == v + 1
    assert all(ps["sl_pct"] == 20 and ps["max_sl_pct"] == 20 and ps["min_score"] == 67 and ps["tp_pct"] == 1000
               for ps in st.params.values())
    up = db.strategy_updates()[0]
    assert up["kind"] == "param_change" and "v1.1.3" in up["reason"] and up["changes"]["new.sl_pct"] == [8.0, 20.0]
    assert migrate_v113(db) == [] and load_strategies(db)["first_pullback"].version == v + 1
    assert LIMITS["tp_pct"][0] >= 1000
    db.close()
