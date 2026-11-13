"""Phase 5 signal engine tests on REAL recorded candles (tests/fixtures/candles_recorded.json, built by
tools/record_candles_fixture.py from the phase-4/5 live tests and GeckoTerminal 1m OHLCV).
Every detector except pump_exhaustion fired on recorded data; pump_exhaustion never fired in ~25 min
of real data, so its test uses a real series with the move amplified (labelled below)."""
import asyncio
import json
from pathlib import Path

import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.judge import Judge
from shadow.models import Candle, MarketData
from shadow.safety import PASS, REJECT, SafetyReport
from shadow.signals import (DETECTORS, SignalEngine, Sources, classify, compute_features, resample,
                            run_detectors, score_signal)

FIX = json.loads((Path(__file__).parent / "fixtures" / "candles_recorded.json").read_text())
S = load_settings(env={})


def candles(key, start=0, end=None):
    ser = FIX["series"][key]
    return [Candle(ser["mint"], t, o, h, lo, c, v, b, s, 1, "fixture")
            for t, o, h, lo, c, v, b, s in ser["candles"][start:end]]


def window(w):
    ser = FIX["series"][w["series"]]
    cs = candles(w["series"], w["start"], w["end"])
    md = MarketData(ser["mint"], symbol=ser["symbol"], liquidity_usd=ser["liquidity_usd"], launch_ts=ser["launch_ts"])
    f = compute_features(cs, md, w["universe"], cs[-1].ts + 60, ser.get("migrated_ts"))
    return cs, md, f


@pytest.mark.parametrize("name", [d for d in DETECTORS if d != "pump_exhaustion"])
def test_detector_fires_on_recorded_window(name):
    ws = FIX["windows"].get(name) or []
    assert ws, f"no recorded window for {name}"
    for w in ws:
        _, _, f = window(w)
        d = run_detectors(f, w["universe"])[name]
        assert d.fired, (name, w)
        assert 0 < d.strength <= 1 and d.detail


def _exhaustion_like_real_window():
    """A real GeckoTerminal 1m window of a $1M+ coin: a rise, then lower highs with fading volume."""
    best = None
    for key, ser in FIX["series"].items():
        if not key.startswith("gt:"):
            continue
        cs_all = candles(key)
        for end in range(20, len(cs_all) + 1):
            cs = cs_all[:end]
            f = compute_features(cs, MarketData(ser["mint"], liquidity_usd=300000), "mcap", cs[-1].ts + 60)
            ext = max(f.get("ret_15m") or 0, f.get("ret_30m") or 0)
            if f["lower_highs"] and (f.get("vol_fade") or 1) < 0.6 and ext >= 3 and (best is None or ext > best[0]):
                best = (ext, cs)
    return best


def test_pump_exhaustion_on_amplified_real_series():
    # pump_exhaustion never fired in ~25 min of recorded data. This test takes the most extended real
    # window that already shows lower highs + fading volume and amplifies its price move around the close
    # 30 candles back until it is >= 1.2x the mcap threshold - a DERIVED input, not a recorded one.
    best = _exhaustion_like_real_window()
    assert best, "a real lower-highs + fading-volume window exists"
    ext, cs = best
    k = max(1.0, 1.2 * 30.0 / ext)
    base = cs[-31].close if len(cs) > 31 else cs[0].close
    amp = lambda x: base + (x - base) * k  # noqa: E731
    dcs = [Candle(c.mint, c.ts, amp(c.open), amp(c.high), amp(c.low), amp(c.close), c.volume_usd, 0, 0, 1, "derived")
           for c in cs]
    f = compute_features(dcs, MarketData(cs[0].mint, liquidity_usd=300000), "mcap", cs[-1].ts + 60)
    d = run_detectors(f, "mcap")["pump_exhaustion"]
    assert f["lower_highs"] and d.fired and d.side < 0
    # the unamplified real window is NOT exhaustion by mcap standards
    f0 = compute_features(cs, MarketData(cs[0].mint, liquidity_usd=300000), "mcap", cs[-1].ts + 60)
    assert ext < 30 and not run_detectors(f0, "mcap")["pump_exhaustion"].fired


def test_quiet_series_fire_nothing_bullish():
    # a flat stretch of a big coin: no early pump / breakout / pullback
    for key, ser in FIX["series"].items():
        if ser["universe"] == "mcap" and key.startswith("gt:"):
            cs = candles(key)
            f = compute_features(cs[:3], MarketData(ser["mint"], liquidity_usd=ser["liquidity_usd"]), "mcap", cs[2].ts + 60)
            d = run_detectors(f, "mcap")
            assert not d["first_pullback"].fired and not d["breakout"].fired   # too few candles for mcap
            break


def test_resample_5m_and_mtf():
    key = next(k for k in FIX["series"] if k.startswith("gt:"))
    cs = candles(key, 0, 30)
    b5 = resample(cs, 5)
    assert 6 <= len(b5) <= 7
    assert abs(sum(b["volume"] for b in b5) - sum(c.volume_usd for c in cs)) < 1e-6
    assert b5[0]["high"] == max(c.high for c in cs if c.ts // 300 == b5[0]["ts"] // 300)


def test_universe_parameter_sets_differ():
    # the same real +15% 5m move is an early pump for an mcap coin but not for a brand-new launch
    w = FIX["windows"]["early_pump"][2]
    cs, md, _ = window(w)
    f_new = compute_features(cs, md, "new", cs[-1].ts + 60)
    f_mcap = compute_features(cs, md, "mcap", cs[-1].ts + 60)
    r5 = f_new["ret_5m"]
    assert r5 is not None and 4 <= r5
    assert run_detectors(f_mcap, "mcap")["early_pump"].fired or f_mcap["buy_ratio_5m"] < 0.58


def test_score_bounds_and_classes():
    for name, ws in FIX["windows"].items():
        for w in ws:
            _, _, f = window(w)
            det = run_detectors(f, w["universe"])
            sc, parts = score_signal(det, f, 90)
            assert 0 <= sc <= 100 and set(parts) == {"pattern", "flow", "quality", "bear"}
    assert classify(85) == "STRONG_WATCH" and classify(80) == "STRONG_WATCH"
    assert classify(79.9) == "WATCH" and classify(65) == "WATCH"
    assert classify(64) == "WEAK" and classify(50) == "WEAK"
    assert classify(49.9) == "REJECT"
    assert classify(99, critical=True) == "REJECT"          # critical safety always wins


def test_dump_lowers_score():
    w = FIX["windows"]["sudden_dump"][0]
    _, _, f = window(w)
    det = run_detectors(f, w["universe"])
    with_dump, _ = score_signal(det, f, 90)
    det["sudden_dump"].fired = False
    without, _ = score_signal(det, f, 90)
    assert with_dump < without


# ---------------------------------------------------------------- engine flow with mocks
class FakeCollector:
    def __init__(self, mint, cs, md):
        self.cs, self.md, self.tracked = cs, md, {mint: True}

    def tracked_mints(self):
        return list(self.tracked)

    def get_market(self, mint):
        return self.md if mint in self.tracked else None

    def get_candles(self, mint, minutes=60):
        return self.cs


class FakeChecker:
    def __init__(self, rep):
        self.rep, self.near_fn, self.prioritized, self.quotes = rep, None, [], []

    def get_report(self, mint):
        return self.rep

    def prioritize(self, mint):
        self.prioritized.append(mint)

    def request_quote(self, mint, priority=False):
        self.quotes.append((mint, priority))


def report(mint, now, verdict=PASS, critical=(), quote_age=10, impact=0.5, loss=3.0):
    data = {"quote": {"sellable": True, "impact_pct": impact}, "sell_loss_pct": loss}
    return SafetyReport(mint, now - 30, now + 600, 90 if verdict == PASS else 20, verdict, list(critical), [], data,
                        quote_ts=None if quote_age is None else now - quote_age)


def early_setup():
    """A recorded new-launch window where Early Entry's setup exists and scores >= 65 with a clean safety."""
    for key, ser in FIX["series"].items():
        if ser["universe"] != "new":
            continue
        cs_all = candles(key)
        for end in range(3, len(cs_all) + 1):
            cs = cs_all[:end]
            md = MarketData(ser["mint"], symbol=ser["symbol"] or "T", price_usd=cs[-1].close, liquidity_usd=30000,
                            launch_ts=cs[0].ts - 60)
            f = compute_features(cs, md, "new", cs[-1].ts + 60)
            det = run_detectors(f, "new")
            fired = {k for k, d in det.items() if d.fired}
            if "buy_sell_pressure" in fired and fired & {"early_pump", "first_green_candle", "volume_increase"} \
                    and "sudden_dump" not in fired and (f.get("ret_60m") or 0) <= 150 and score_signal(det, f, 90)[0] >= 65:
                return cs, md
    return None


def make_engine(tmp_path, cs, md, rep, judge=True):
    db = Database(str(tmp_path / "e.db"))
    now = cs[-1].ts + 60
    clock = {"t": now}
    src = Sources(FakeCollector(md.mint, cs, md))
    chk = FakeChecker(rep)
    out = []
    eng = SignalEngine(S, db, src, checker=chk, judge=Judge(S, db) if judge else None, clock=lambda: clock["t"],
                       on_signal=lambda row, v: out.append((row, v)))
    return db, eng, chk, out, clock


def test_engine_buy_watch_judge_and_virtual_outcome(tmp_path):
    setup = early_setup()
    assert setup, "recorded data contains an early-entry setup"
    cs, md = setup
    now = cs[-1].ts + 60
    db, eng, chk, out, clock = make_engine(tmp_path, cs, md, report(md.mint, now))
    asyncio.run(eng.step())
    buys = [r for r, v in out if r["alert_type"] == "BUY_WATCH"]
    assert buys and buys[0]["strategy"] == "early_entry" and buys[0]["params_version"] == 1
    assert chk.near_fn is not None and eng.near_signal(md.mint) is True
    v = db.con.execute("SELECT verdict, confidence, reasons_json FROM judge_verdicts").fetchall()
    assert len(v) == len(buys) and v[0][0] in ("APPROVE", "VETO") and json.loads(v[0][2]) is not None
    sid = buys[0]["id"] if "id" in buys[0] else db.con.execute("SELECT id FROM signals WHERE alert_type='BUY_WATCH'").fetchone()[0]
    # price crashes below SL -> the watch closes and the judge outcome is filled in
    md.price_usd = cs[-1].close * 0.5
    clock["t"] += 60
    asyncio.run(eng.step())
    row = db.get_signal(sid)
    assert row["status"] == "closed" and row["pnl_pct"] < 0
    assert db.con.execute("SELECT outcome FROM judge_verdicts WHERE signal_id=?", (sid,)).fetchone()[0] == "loss"


def test_engine_critical_safety_rejects(tmp_path):
    cs, md = early_setup()
    now = cs[-1].ts + 60
    db, eng, chk, out, _ = make_engine(tmp_path, cs, md, report(md.mint, now, REJECT, ["creator_rug_history"]))
    asyncio.run(eng.step())
    types = [r["alert_type"] for r, _ in out]
    assert types == ["REJECT"] and "creator_rug_history" in out[0][0]["reason"]
    assert out[0][0]["class"] == "REJECT"
    assert db.con.execute("SELECT count(*) FROM judge_verdicts").fetchone()[0] == 0


def test_engine_waits_for_safety_and_quote(tmp_path):
    cs, md = early_setup()
    now = cs[-1].ts + 60
    db, eng, chk, out, clock = make_engine(tmp_path, cs, md, None)
    asyncio.run(eng.step())
    assert out == [] and chk.prioritized == [md.mint]
    chk.rep = report(md.mint, now, quote_age=None)            # PASS but no sell quote yet
    clock["t"] += 15
    asyncio.run(eng.step())
    assert out == [] and (md.mint, True) in chk.quotes


def test_engine_paper_path_and_restart(tmp_path):
    from shadow.paper import PaperEngine
    cs, md = early_setup()
    now = cs[-1].ts + 60
    db = Database(str(tmp_path / "e.db"))
    clock = {"t": now}
    src = Sources(FakeCollector(md.mint, cs, md))
    chk = FakeChecker(report(md.mint, now))
    pe = PaperEngine(S, db, src=src, checker=chk, clock=lambda: clock["t"])
    j = Judge(S, db)
    eng = SignalEngine(S, db, src, checker=chk, judge=j, clock=lambda: clock["t"], paper=pe)
    asyncio.run(eng.step())
    watches = db.con.execute("SELECT id, judge_verdict FROM signals WHERE alert_type='BUY_WATCH'").fetchall()
    assert watches
    approved = [w for w in watches if w[1] == "APPROVE"]
    assert len(pe.open_positions()) == min(1, len(approved))        # one position per coin
    for i in range(3):
        clock["t"] += 30
        asyncio.run(eng.step())
    asyncio.run(eng.stop())
    pth = json.loads(db.get_signal(watches[0][0])["path_json"])
    assert len(pth) >= 3 and pth[0][1] == md.price_usd
    # restart: open watches come back from SQLite
    eng2 = SignalEngine(S, db, src, checker=chk, judge=j, clock=lambda: clock["t"], paper=pe)
    assert set(eng2.open) == {w[0] for w in watches} and eng2.stats["reloaded_open"] == len(watches)
    db.close()


def test_backfill_flow_features_ignore_backfilled_candles():
    # backfilled / gap-filled candles (n_snapshots=0) carry no buys/sells and must not fake a buyer surge
    key = next(k for k in FIX["series"] if k.startswith("live_p5:"))
    cs = candles(key)
    back = [Candle(c.mint, c.ts - 3600, c.open, c.high, c.low, c.close, c.volume_usd, 0, 0, 0, "local") for c in cs]
    md = MarketData(cs[0].mint, liquidity_usd=50000)
    f1 = compute_features(cs, md, "new", cs[-1].ts + 60)
    f2 = compute_features(back + cs, md, "new", cs[-1].ts + 60)
    assert f1["buy_ratio_5m"] == f2["buy_ratio_5m"] and f1["buyer_growth"] == f2["buyer_growth"]
