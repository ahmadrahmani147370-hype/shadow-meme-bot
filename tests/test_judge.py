"""Judge rules: a clean baseline is APPROVED, then each veto rule is triggered on its own."""
import copy
import json

import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.judge import APPROVE, VETO, Judge, judge_accuracy, strategy_health
from shadow.models import MarketData
from shadow.safety import PASS, REJECT, SafetyReport
from shadow.signals import DETECTORS, Det, Evaluation

S = load_settings(env={})
NOW = 1_800_000_000.0
MINT = "A" * 44


def base():
    feat = {"n": 20, "price": 1.0, "liquidity": 30000, "ret_15m": 20.0, "ret_60m": 40.0, "buy_ratio_5m": 0.7,
            "buy_ratio_slope": 0.01, "mtf_up": True}
    det = {k: Det(k, -1 if k in ("pump_exhaustion", "sudden_dump") else 1) for k in DETECTORS}
    det["early_pump"] = Det("early_pump", 1, True, 0.8, "+20%")
    det["buy_sell_pressure"] = Det("buy_sell_pressure", 1, True, 0.8, "70% buys")
    ev = Evaluation(MINT, "new", feat, det, 82.0, {}, "STRONG_WATCH")
    sig = {"ts": NOW, "mint": MINT, "universe": "new", "strategy": "early_entry", "score": 82.0, "min_score": 65,
           "class": "STRONG_WATCH", "features_json": json.dumps(feat)}
    rep = SafetyReport(MINT, NOW - 60, NOW + 600, 90, PASS, [], [], {"quote": {"sellable": True, "impact_pct": 0.4},
                                                                     "sell_loss_pct": 3.0}, quote_ts=NOW - 20)
    md = MarketData(MINT, symbol="PEPE", name="Pepe", price_usd=1.0, liquidity_usd=30000)
    ctx = {"open_approved": [], "strategy_closed": [], "judge_history": [], "creator": "C1"}
    return sig, rep, md, ev, ctx


def review(**mods):
    sig, rep, md, ev, ctx = base()
    for k, fn in mods.items():
        fn(sig, rep, md, ev, ctx)
    return Judge(S).review(sig, rep, md, ev, ctx)


def test_clean_signal_is_approved():
    v = review()
    assert v.verdict == APPROVE and v.confidence >= S.judge_min_confidence and not v.hard_codes()


@pytest.mark.parametrize("code,mod", [
    ("safety_not_pass", lambda s, r, m, e, c: setattr(r, "verdict", REJECT)),
    ("stale_quote", lambda s, r, m, e, c: setattr(r, "quote_ts", NOW - 600)),
    ("stale_quote", lambda s, r, m, e, c: r.data["quote"].update(sellable=False)),
    ("impact", lambda s, r, m, e, c: r.data["quote"].update(impact_pct=4.5)),
    ("spread", lambda s, r, m, e, c: r.data.update(sell_loss_pct=14.0)),
    ("thin_liquidity", lambda s, r, m, e, c: e.features.update(liquidity=5000)),
    ("late_entry", lambda s, r, m, e, c: e.features.update(ret_15m=75.0)),
    ("late_entry", lambda s, r, m, e, c: e.features.update(ret_60m=260.0)),
    ("dump_conflict", lambda s, r, m, e, c: e.detections.update(sudden_dump=Det("sudden_dump", -1, True, 0.9, "-25%"))),
    ("exhaustion_conflict", lambda s, r, m, e, c: e.detections.update(
        pump_exhaustion=Det("pump_exhaustion", -1, True, 0.6, "wicks"))),
    ("max_open", lambda s, r, m, e, c: c.update(open_approved=[
        {"mint": str(i), "symbol": f"X{i}", "universe": "mcap", "strategy": "first_pullback", "creator": f"K{i}"}
        for i in range(S.paper_max_open)])),
    ("correlated", lambda s, r, m, e, c: c.update(open_approved=[
        {"mint": "B" * 44, "symbol": "pepe", "universe": "new", "strategy": "early_entry", "creator": "Z"}])),
    ("correlated", lambda s, r, m, e, c: c.update(open_approved=[
        {"mint": "B" * 44, "symbol": "OTHER", "universe": "new", "strategy": "early_entry", "creator": "C1"}])),
    ("strategy_drawdown", lambda s, r, m, e, c: c.update(strategy_closed=[{"pnl_pct": p} for p in
                                                                           [5, 5, -10, -8, 3, -9, -6, 2, -4, -3]])),
    ("strategy_drawdown", lambda s, r, m, e, c: c.update(strategy_closed=[{"pnl_pct": p} for p in
                                                                           [9, 9, 9, 9, 9, -1, -1, -1, -1, -1]])),
])
def test_each_hard_veto(code, mod):
    v = review(m=mod)
    assert v.verdict == VETO and code in v.hard_codes(), v.reasons


def test_large_coin_needs_more_liquidity_and_less_extension():
    def mcap(s, r, m, e, c):
        s["universe"] = e.universe = "mcap"
        e.features.update(liquidity=60000)
    assert "thin_liquidity" in review(m=mcap).hard_codes()

    def mcap_late(s, r, m, e, c):
        s["universe"] = e.universe = "mcap"
        e.features.update(liquidity=200000, ret_15m=18.0)
    assert "late_entry" in review(m=mcap_late).hard_codes()


def test_soft_reasons_lower_confidence_until_veto():
    def weak(s, r, m, e, c):
        s["score"] = 68.0
        e.features.update(buy_ratio_5m=0.5, mtf_up=False, n=5)
    v = review(m=weak)
    assert v.verdict == VETO and "low_confidence" in v.hard_codes()
    assert {r["code"] for r in v.reasons if r["kind"] == "soft"} >= {"weak_flow", "thin_margin", "no_5m_confirmation",
                                                                     "few_candles"}


def test_health_and_accuracy_helpers():
    h = strategy_health([{"pnl_pct": p} for p in (10, -5, -5, 20, -30)])
    assert h["n"] == 5 and h["drawdown_pct"] == 30 and h["win_rate"] == 0.4
    hist = [{"verdict": APPROVE, "outcome": "win"}] * 3 + [{"verdict": APPROVE, "outcome": "loss"}] + \
           [{"verdict": VETO, "outcome": "loss"}] * 2 + [{"verdict": VETO, "outcome": None}]
    a = judge_accuracy(hist)
    assert a["approved_win_rate"] == 0.75 and a["approved_n"] == 4 and a["vetoed_win_rate"] == 0 and a["vetoed_n"] == 2


def test_record_and_outcome(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    sig, rep, md, ev, ctx = base()
    j = Judge(S, db)
    v = j.review(sig, rep, md, ev, ctx)
    sid = db.add_signal({**{k: sig[k] for k in ("ts", "mint", "universe", "strategy", "score", "class")},
                         "alert_type": "BUY_WATCH", "status": "open"})
    vid = j.record(sid, sig, v)
    assert vid
    db.set_judge_outcome(sid, "win", 12.5, NOW + 600)
    row = db.con.execute("SELECT verdict, outcome, outcome_pnl_pct, judge_version, inputs_json FROM judge_verdicts").fetchone()
    assert row[0] == APPROVE and row[1] == "win" and row[2] == 12.5 and row[3] == "judge-1"
    assert json.loads(row[4])["features"]["liquidity"] == 30000
    hist = db.judge_history()
    assert hist and hist[0]["verdict"] == APPROVE
    db.close()
