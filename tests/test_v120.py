"""v1.2.0: why no trades opened since v1.1 and the loosened (still >= $1M only) entry path."""
import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.judge import APPROVE, VETO, Judge
from shadow.strategies import DEFAULT_PARAMS, load_strategies, migrate_v120, new_version
from tests.test_judge import base

pytestmark = pytest.mark.v11


def S_():
    return load_settings(env={})


def test_v120_defaults_keep_the_fixed_rules():
    S = S_()
    assert S.trade_min_mcap_usd == 1_000_000 and S.trade_min_liq_usd == 50_000 and S.trade_min_liq_mcap == 0.015
    assert S.judge_min_confidence == 0.35 and S.judge_min_liq_large == 50_000 and S.paper_only
    assert S.exit_sl_pct == 20 and S.exit_tp_pct == 1000 and S.tg_notify == "trades"
    assert S.universe_gt_pages == 3
    assert DEFAULT_PARAMS["early_entry"]["params"]["new"]["min_score"] == 70
    for n in ("first_pullback", "migration_breakout", "momentum_breakout"):
        assert all(ps["min_score"] == 50 for ps in DEFAULT_PARAMS[n]["params"].values())


def _real(score, min_score, ratio, slope, mtf, universe="mcap", r15=5.0, r60=10.0, liq=150_000):
    """A >= $1M BUY_WATCH as recorded on 13 Nov (features from judge_verdicts)."""
    sig, rep, md, ev, ctx = base()
    sig.update(score=score, min_score=min_score, universe=universe, strategy="momentum_breakout")
    ev.universe = universe
    ev.features.update(liquidity=liq, buy_ratio_5m=ratio, buy_ratio_slope=slope, mtf_up=mtf, ret_15m=r15, ret_60m=r60)
    return Judge(S_()).review(sig, rep, md, ev, ctx)


def test_recorded_vetoes_under_v120_rules():
    # every one of these was VETOED by v1.1.3 (confidence floor 0.60 / mcap late-entry 15%)
    mihao = _real(64.9, 50, 0.46, 0.0, True, r15=22, r60=22, liq=479_533)
    paws = _real(53.1, 50, 0.42, 0.0, True, liq=118_876)
    mx = _real(61.4, 50, 0.37, -0.05, False, liq=177_046)
    nio = _real(53.6, 50, 0.19, -0.05, True, liq=130_953)
    assert mihao.verdict == APPROVE and paws.verdict == APPROVE and mx.verdict == APPROVE
    assert nio.verdict == VETO and "low_confidence" in nio.hard_codes()      # 19% buys + falling: still vetoed
    # a genuinely late entry is still vetoed
    late = _real(73.3, 60, 0.7, 0.0, True, universe="new", r15=5, r60=10757, liq=143_270)
    assert late.verdict == VETO and "late_entry" in late.hard_codes()


def test_confidence_is_margin_based():
    at_min = _real(50.0, 50, 0.7, 0.01, True)
    assert at_min.confidence == pytest.approx(0.45)            # 0.50 + 0 margin + 0.05 mtf - 0.10 thin margin
    assert _real(70.0, 50, 0.7, 0.01, True).confidence == pytest.approx(0.75)


def test_real_dangers_still_vetoed():
    assert "thin_liquidity" in _real(70, 50, 0.7, 0.01, True, liq=45_000).hard_codes()


def test_migrate_v120_lowers_min_score_and_judge_and_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    db.meta_set("migrated_v120", "1")
    load_strategies(db)
    Judge(load_settings(env={"JUDGE_MIN_CONFIDENCE": "0.6", "JUDGE_MIN_LIQ_LARGE": "75000"}), db)   # old judge v1
    cur = db.active_strategy_params("first_pullback")
    new_version(db, "first_pullback", {u: {**ps, "min_score": 65, "sl_pct": 20.0} for u, ps in cur["params"].items()},
                "learned", "t")
    cur = db.active_strategy_params("momentum_breakout")
    new_version(db, "momentum_breakout", {u: {**ps, "min_score": 47} for u, ps in cur["params"].items()}, "learned", "t")
    db.meta_set("migrated_v120", "0")
    done = migrate_v120(db)
    assert "first_pullback" in done and "judge" in done and "momentum_breakout" not in done   # 47 stays (lower)
    st = load_strategies(db)
    assert all(ps["min_score"] == 50 and ps["sl_pct"] == 20 for ps in st["first_pullback"].params.values())
    assert all(ps["min_score"] == 47 for ps in st["momentum_breakout"].params.values())
    j = Judge(S_(), db)
    assert j.params["min_confidence"] == 0.35 and j.params["min_liq_large"] == 50_000 and j.params_version == 2
    assert migrate_v120(db) == []
    db.close()


def test_dashboard_hourly_and_perf_are_real_counts(tmp_path):
    from shadow.dashboard import hourly_series, perf_stats
    db = Database(str(tmp_path / "d.db"))
    now = 1_800_001_800.0  # mid-hour
    db.con.execute("CREATE TABLE IF NOT EXISTS dash_counted (mint TEXT PRIMARY KEY, ts REAL, rejected INTEGER)")
    db.con.executemany("INSERT INTO dash_counted VALUES (?,?,?)",
                       [("a", now - 10, 1), ("b", now - 20, 0), ("c", now - 3 * 3600, 1), ("old", now - 20 * 3600, 1)])
    db.con.commit()
    h = hourly_series(db, now)
    assert len(h["rugs"]) == 12 and h["start"] + 11 * 3600 <= now < h["start"] + 12 * 3600
    assert h["rugs"][-1] == 1 and h["checked"][-1] == 2 and sum(h["rugs"]) == 2 and sum(h["trades"]) == 0
    p = perf_stats(db)
    assert p == {"closed": 0, "wins": 0, "losses": 0, "avg_pnl_usd": None, "avg_pnl_pct": None, "pnl_usd": 0.0,
                 "win_rate": None}
    db.close()
