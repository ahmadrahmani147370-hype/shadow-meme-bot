"""Phase 6 self-learning guardrails. Price paths here are SYNTHETIC unit-test inputs (not market data)."""
import json

import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.judge import Judge
from shadow.learning import (Learner, candidates, cost_pct, evaluate, judge_stats, simulate, strategy_samples,
                             walk_forward)
from shadow.reports import mark_strategy_updates_sent, pending_strategy_updates, strategies_text
from shadow.strategies import DEFAULT_PARAMS, LIMITS, TUNABLE, load_strategies, new_version

S = load_settings(env={})
T0 = 1_800_000_000.0
EE = {**DEFAULT_PARAMS["early_entry"]["params"]["new"]}


def path(*pcts, every=30):
    return [[every * (i + 1), 1 + p / 100] for i, p in enumerate(pcts)]


def add_watch(db, ts, pts, version=1, score=70, strategy="early_entry", universe="new"):
    return db.add_signal({"ts": ts, "mint": f"M{ts}", "universe": universe, "strategy": strategy, "alert_type": "BUY_WATCH",
                          "score": score, "class": "WATCH", "params_version": version, "entry": 1.0, "sl": 0.88,
                          "tp": 1.3, "time_stop_min": 20, "status": "closed", "judge_verdict": "APPROVE"}) and \
        db.con.execute("UPDATE signals SET path_json=? WHERE ts=?", (json.dumps(pts), ts))


def test_simulate_exits():
    smp = {"entry": 1.0, "score": 70, "sl_dist_pct": 12, "path": path(5, 10, 33)}
    r = simulate("early_entry", EE, smp, 0.0)
    assert r["reason"] == "take_profit" and r["net"] == pytest.approx(33)
    assert simulate("early_entry", EE, {**smp, "path": path(-5, -13)}, 0)["reason"] == "stop_loss"
    assert simulate("early_entry", EE, {**smp, "path": path(16, 20, 5)}, 0)["reason"] == "trailing_stop"
    assert simulate("early_entry", EE, {**smp, "path": path(1, 2, every=60 * 25)}, 0)["reason"] == "time_stop"
    assert simulate("early_entry", EE, {**smp, "path": path(1, 2)}, 0) is None          # horizon too short
    assert simulate("early_entry", {**EE, "min_score": 75}, smp, 0)["taken"] is False
    assert cost_pct(S) == pytest.approx(1.0)        # default: no fees, 2 x 0.5% minimum slippage
    from dataclasses import replace
    assert cost_pct(replace(S, paper_buy_fee=0.5, paper_sell_fee=0.5)) == pytest.approx(51.0)  # $1 on $2 = 50% + 1%


def test_candidates_are_bounded():
    for k, old, new in candidates(EE, TUNABLE["early_entry"], LIMITS, 20):
        if k == "min_score":
            assert abs(new - old) <= 5
        else:
            assert abs(new - old) <= abs(old) * 0.2 + 1e-9
        lo, hi = LIMITS[k]
        assert lo <= new <= hi


def test_walk_forward_is_chronological():
    xs = [{"ts": t} for t in (5, 1, 4, 2, 3, 9, 7, 8, 6, 10)]
    tr, te = walk_forward(xs, 0.7)
    assert [x["ts"] for x in tr] == list(range(1, 8)) and [x["ts"] for x in te] == [8, 9, 10]


def test_insufficient_samples_change_nothing(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    for i in range(10):
        add_watch(db, T0 + i * 600, path(5, 10, 33, 25, 38, -20) + [[1300, 0.9]])
    res = Learner(S, db, clock=lambda: T0 + 10 ** 6).run_once()
    assert any(r["action"] == "insufficient" for r in res)
    assert db.strategy_updates() == [] and load_strategies(db)["early_entry"].version == 1
    db.close()


def _seed_tp_samples(db, n=40, version=1, ts0=T0):
    # every opportunity touches +33%, dips to +25%, then reaches +38% before collapsing
    for i in range(n):
        add_watch(db, ts0 + i * 600, path(5, 10, 33, 25, 38, -20) + [[1300, 0.9]], version=version)


def test_learns_bounded_change_with_walk_forward_and_notifies(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    _seed_tp_samples(db)
    clock = T0 + 10 ** 6
    res = Learner(S, db, clock=lambda: clock).learn_strategy("early_entry")
    act = [r for r in res if r["action"] == "activate"]
    assert act, res
    st = load_strategies(db)["early_entry"]
    assert st.version == 2
    (key, (old, new)), = act[0]["change"].items()
    assert key == "new.tp_pct" and old == 30.0 and new == 36.0          # 20% max step
    ups = db.strategy_updates()
    assert len(ups) == 1 and ups[0]["kind"] == "param_change" and ups[0]["to_version"] == 2
    m = ups[0]["metrics"]
    assert m["before"]["test"]["n"] == 12 and m["after"]["test"]["score"] > m["before"]["test"]["score"]
    assert "tp_pct 30.0 -> 36.0" in ups[0]["reason"]
    log = db.learning_log("early_entry")
    assert log[0]["action"] == "activate" and log[0]["to_version"] == 2
    # cooldown: no second change right away
    assert Learner(S, db, clock=lambda: clock + 60).learn_strategy("early_entry") == [
        {"target": "early_entry", "action": "cooldown"}]
    # notifications for phase 8 + /strategies
    pend = pending_strategy_updates(db)
    assert len(pend) == 1 and "STRATEGY UPDATE early_entry v1 → v2" in pend[0][1] and "new.tp_pct 30.0 → 36.0" in pend[0][1]
    assert "Before: test" in pend[0][1] and "on 12" in pend[0][1]
    mark_strategy_updates_sent(db, [pend[0][0]])
    assert pending_strategy_updates(db) == []
    assert "new.tp_pct 30.0 → 36.0" in strategies_text(db, S)
    db.close()


def test_out_of_sample_rejection(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    # oldest 28: higher TP helps; newest 12: price stops at +33% and collapses -> not confirmed
    for i in range(28):
        add_watch(db, T0 + i * 600, path(5, 10, 33, 25, 38, -20) + [[1300, 0.9]])
    for i in range(28, 40):
        add_watch(db, T0 + i * 600, path(5, 10, 33, 25, -20) + [[1300, 0.9]])
    res = Learner(S, db, clock=lambda: T0 + 10 ** 6).learn_strategy("early_entry")
    assert not any(r["action"] == "activate" for r in res)
    assert load_strategies(db)["early_entry"].version == 1 and db.strategy_updates() == []
    db.close()


def test_auto_rollback_creates_notification(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    load_strategies(db)
    params = {"new": {**EE, "tp_pct": 36.0}}
    v = new_version(db, "early_entry", params, "learned", "test")
    assert v == 2
    # live watches under v2: +33% then collapse -> v1 (TP 30) would have won, v2 misses
    for i in range(12):
        add_watch(db, T0 + i * 600, path(5, 10, 33, 25, -20) + [[1300, 0.9]], version=2)
    res = Learner(S, db, clock=lambda: T0 + 10 ** 6).learn_strategy("early_entry")
    assert res[0]["action"] == "rollback"
    assert load_strategies(db)["early_entry"].version == 1
    up = db.strategy_updates()[0]
    assert up["kind"] == "rollback" and up["from_version"] == 2 and up["to_version"] == 1
    assert up["changes"] == {"new.tp_pct": [36.0, 30.0]}
    assert up["metrics"]["after"]["n"] == 12 and up["metrics"]["after"]["score"] < up["metrics"]["before"]["score"]
    assert "ROLLBACK early_entry v2 → v1" in pending_strategy_updates(db)[0][1]
    db.close()


def _judge_rows(db, n=40):
    for i in range(n):
        thin = i % 2 == 0
        reasons = [{"code": "thin_liquidity", "kind": "hard", "detail": "x"}] if thin else []
        db.add_judge_verdict({"signal_id": i + 1, "ts": T0 + i * 600, "mint": f"J{i}", "universe": "new",
                              "strategy": "early_entry", "verdict": "VETO" if thin else "APPROVE", "confidence": 0.7,
                              "reasons_json": json.dumps(reasons), "judge_version": "judge-1/p1",
                              "inputs_json": json.dumps({"universe": "new", "liquidity": 7000 if thin else 20000,
                                                         "impact_pct": 0.5, "sell_loss_pct": 2.0, "confidence": 0.7,
                                                         "judge_params_version": 1})})
        db.set_judge_outcome(i + 1, "win", 80.0 if thin else 60.0, T0 + i * 600 + 900)


def test_judge_accuracy_and_guarded_tuning(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    _judge_rows(db)
    js = judge_stats(db, costs=51.0)
    assert js["approved"]["closed"] == 20 and js["vetoed"]["closed"] == 20
    assert js["veto_precision"] == 0 and js["edge_pct"] == pytest.approx(-20)
    assert js["by_reason"]["thin_liquidity"]["correct_rate"] == 0
    judge = Judge(S, db)
    assert judge.params["min_liq_new"] == 8000
    res = Learner(S, db, clock=lambda: T0 + 10 ** 6, judge=judge).tune_judge()
    assert res["action"] == "activate", res
    assert judge.params["min_liq_new"] == 6400.0 and judge.params_version == 2      # reloaded, 20% step
    up = db.strategy_updates()[0]
    assert up["kind"] == "judge_change" and up["changes"] == {"min_liq_new": [8000.0, 6400.0]}
    assert up["metrics"]["after"]["test"]["n"] == 12
    assert "JUDGE UPDATE judge v1 → v2" in pending_strategy_updates(db)[0][1]
    db.close()


def test_samples_by_version(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    _seed_tp_samples(db, n=3, version=1)
    _seed_tp_samples(db, n=2, version=2, ts0=T0 + 10 ** 5)
    assert len(strategy_samples(db, "early_entry", "new")) == 5
    assert len(strategy_samples(db, "early_entry", "new", version=2)) == 2
    assert evaluate("early_entry", EE, strategy_samples(db, "early_entry", "new"), 0)["n"] == 5
    db.close()
