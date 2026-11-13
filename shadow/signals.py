"""PHASE 5 - Signal engine: 10 detectors, 0-100 score, alerts BUY WATCH / SELL / HOLD / REJECT.

Works on local 1-minute candles (collector / universe collector) plus the latest market data.
Smarter than plain thresholds:
  * multi-timeframe confirmation: every coin's 1m candles are resampled to 5m; a setup scores higher
    when the last 5m candle agrees,
  * relative volume as z-scores (last 2 minutes vs the previous 15, and 5m vs previous 5m buckets),
  * buyer/seller ratio trend (slope of the per-minute buy ratio) and buyer growth,
  * liquidity-adjusted momentum (a move on a thin pool counts less),
  * separate parameter sets for new launches, MCAP coins and TOP VOLUME coins.

Score (0-100) = setup pattern (max 35) + order flow (max 35) + quality (max 30: safety sub-score,
5m confirmation, liquidity) - bearish warnings (exhaustion up to 25, sudden dump up to 40).
80-100 Strong Watch, 65-79 Watch, 50-64 Weak, <50 Reject. Any critical safety flag = REJECT.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field

from .config import Settings
from .db import Database
from .models import MIGRATED, U_MCAP, U_NEW, U_VOLUME, Candle, MarketData
from .safety import PASS
from .strategies import Strategy, load_strategies

PHASE = 5
IMPLEMENTED = True
log = logging.getLogger("shadow.signals")

STRONG_WATCH, WATCH, WEAK, REJECT_CLASS = "STRONG_WATCH", "WATCH", "WEAK", "REJECT"
BUY_WATCH, SELL, HOLD, REJECT = "BUY_WATCH", "SELL", "HOLD", "REJECT"
BULL, BEAR = 1, -1
PATTERNS = ("early_pump", "first_green_candle", "first_pullback", "breakout", "migration_breakout")
FLOW_WEIGHTS = {"volume_increase": 12, "buyer_increase": 10, "buy_sell_pressure": 13}
DETECTORS = PATTERNS + tuple(FLOW_WEIGHTS) + ("pump_exhaustion", "sudden_dump")

# Detector parameters per universe (new launches move far more than $1M+ coins)
UNIVERSE_PARAMS = {
    U_NEW: dict(ep_ret5=12.0, ep_z=1.5, fg_reds=3, fg_body=3.0, pb_impulse=40.0, pb_min=10.0, pb_max=35.0,
                vi_z=2.0, bi_growth=1.5, pr_ratio=0.60, pr_min_trades=10, bo_lookback=15, bo_z=1.0,
                ex_ext=80.0, mb_window=60, dump_ret3=-20.0, dump_sell=0.6, ref_liq=20_000.0, min_candles=3),
    U_VOLUME: dict(ep_ret5=6.0, ep_z=2.0, fg_reds=3, fg_body=1.5, pb_impulse=20.0, pb_min=5.0, pb_max=18.0,
                   vi_z=2.2, bi_growth=1.4, pr_ratio=0.58, pr_min_trades=25, bo_lookback=20, bo_z=1.2,
                   ex_ext=45.0, mb_window=120, dump_ret3=-12.0, dump_sell=0.6, ref_liq=100_000.0, min_candles=8),
    U_MCAP: dict(ep_ret5=4.0, ep_z=2.0, fg_reds=4, fg_body=1.0, pb_impulse=12.0, pb_min=3.0, pb_max=12.0,
                 vi_z=2.5, bi_growth=1.4, pr_ratio=0.58, pr_min_trades=30, bo_lookback=30, bo_z=1.5,
                 ex_ext=30.0, mb_window=120, dump_ret3=-8.0, dump_sell=0.6, ref_liq=250_000.0, min_candles=10),
}


@dataclass
class Det:
    name: str
    side: int
    fired: bool = False
    strength: float = 0.0
    detail: str = ""


def _pct(a, b):
    return (a / b - 1.0) * 100.0 if a and b else None


def _z(x: float, hist: list[float]) -> float:
    if len(hist) < 3:
        return 0.0
    mu = statistics.fmean(hist)
    sd = max(statistics.pstdev(hist), mu * 0.25, 1e-9)
    return (x - mu) / sd


def _slope(ys: list[float]) -> float:
    n = len(ys)
    if n < 3:
        return 0.0
    xm, ym = (n - 1) / 2, statistics.fmean(ys)
    den = sum((i - xm) ** 2 for i in range(n))
    return sum((i - xm) * (y - ym) for i, y in enumerate(ys)) / den if den else 0.0


def resample(candles: list[Candle], minutes: int = 5) -> list[dict]:
    out: dict[int, dict] = {}
    for c in candles:
        k = int(c.ts // (60 * minutes)) * 60 * minutes
        b = out.get(k)
        if b is None:
            out[k] = {"ts": k, "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                      "volume": c.volume_usd or 0.0, "buys": c.buys or 0, "sells": c.sells or 0}
        else:
            b["high"], b["low"], b["close"] = max(b["high"], c.high), min(b["low"], c.low), c.close
            b["volume"] += c.volume_usd or 0.0
            b["buys"] += c.buys or 0
            b["sells"] += c.sells or 0
    return [out[k] for k in sorted(out)]


def compute_features(candles: list[Candle], md: MarketData | None, universe: str, now: float,
                     migrated_ts: float | None = None) -> dict:
    P = UNIVERSE_PARAMS.get(universe, UNIVERSE_PARAMS[U_NEW])
    cs = [c for c in candles if c.close and c.close > 0]
    n = len(cs)
    f: dict = {"n": n, "universe": universe}
    closes = [c.close for c in cs]
    highs, lows, opens = [c.high for c in cs], [c.low for c in cs], [c.open for c in cs]
    vols = [c.volume_usd or 0.0 for c in cs]
    buys, sells = [c.buys or 0 for c in cs], [c.sells or 0 for c in cs]
    price = (md.price_usd if md and md.price_usd else None) or (closes[-1] if closes else None)
    f["price"] = price
    for k in (1, 3, 5, 15, 30, 60):
        f[f"ret_{k}m"] = _pct(closes[-1], closes[-1 - k]) if n > k else None
    if f["ret_5m"] is None and md and md.price_change_m5 is not None:
        f["ret_5m"] = md.price_change_m5
    if f["ret_60m"] is None and md and md.price_change_h1 is not None:
        f["ret_60m"] = md.price_change_h1
    # relative volume (last 2 minutes vs the 15 before), 5m buckets vs previous buckets
    f["vol_z"] = _z(statistics.fmean(vols[-2:]), vols[-17:-2]) if n >= 6 else 0.0
    c5 = resample(cs, 5)
    f["n5"] = len(c5)
    f["vol5_z"] = _z(c5[-1]["volume"], [b["volume"] for b in c5[-7:-1]]) if len(c5) >= 4 else 0.0
    prev5 = c5[-2] if len(c5) >= 2 else None
    f["mtf_up"] = bool(prev5 and prev5["close"] > prev5["open"] and c5[-1]["close"] >= prev5["close"])
    f["mtf_down"] = bool(prev5 and prev5["close"] < prev5["open"] and c5[-1]["close"] < prev5["close"])
    # order flow
    b5, s5 = sum(buys[-5:]), sum(sells[-5:])
    if b5 + s5 == 0 and md:
        b5, s5 = md.buys_m5, md.sells_m5
    f["trades_5m"] = b5 + s5
    f["buy_ratio_5m"] = b5 / (b5 + s5) if b5 + s5 else None
    br = [b / (b + s) for b, s in zip(buys[-10:], sells[-10:]) if b + s > 0]
    f["buy_ratio_slope"] = _slope(br)
    prior = buys[-15:-5]
    f["buyer_growth"] = (statistics.fmean(buys[-5:]) / max(statistics.fmean(prior), 0.5)) if len(prior) >= 3 else None
    f["sell_ratio_1m"] = (sells[-1] / (buys[-1] + sells[-1])) if n and buys[-1] + sells[-1] else None
    # structure
    if n:
        pk = max(range(n), key=lambda i: highs[i])
        low_before = min(lows[: pk + 1])
        f["peak"], f["peak_idx"], f["low_before_peak"] = highs[pk], pk, low_before
        f["impulse_pct"] = _pct(highs[pk], low_before)
        f["pullback_pct"] = (1 - closes[-1] / highs[pk]) * 100 if highs[pk] else None
        f["pullback_low"] = min(lows[pk:]) if pk < n else None
        lb = int(P["bo_lookback"])
        f["prior_high"] = max(highs[-1 - lb:-1]) if n > 2 else None
        reds = 0
        for i in range(n - 2, -1, -1):
            if closes[i] < opens[i] * 0.999:
                reds += 1
            elif closes[i] <= opens[i] * 1.001 and reds:   # doji inside a red run
                continue
            else:
                break
        f["reds_before"] = reds
        f["last_green"] = closes[-1] > opens[-1]
        f["last_body_pct"] = _pct(closes[-1], opens[-1]) or 0.0
        f["last_vol_vs_median"] = (vols[-1] / max(statistics.median(vols[:-1]), 1e-9)) if n > 2 else None
        start = low_before and lows.index(low_before)
        seg = closes[start: pk + 1] if start is not None else []
        mx, dd = 0.0, 0.0
        for c in seg:
            mx = max(mx, c)
            dd = max(dd, (1 - c / mx) * 100 if mx else 0)
        f["dd_during_impulse"] = dd
        tail = cs[-3:]
        f["upper_wick"] = statistics.fmean([(c.high - max(c.open, c.close)) / max(c.high - c.low, 1e-12) for c in tail])
        f["lower_highs"] = n >= 3 and highs[-1] < highs[-2] < highs[-3]
        f["vol_fade"] = (statistics.fmean(vols[-3:]) / max(statistics.fmean(vols[-8:-3]), 1e-9)) if n >= 8 else None
    liq = md.liquidity_usd if md else None
    f["liquidity"] = liq
    f["liq_factor"] = min(1.0, (liq or 0) / P["ref_liq"])
    f["mcap"] = (md.market_cap or md.fdv) if md else None
    f["age_min"] = md.age_min(now) if md else None
    if migrated_ts:
        f["since_migration_min"] = (now - migrated_ts) / 60
        f["post_mig_high"] = max([c.high for c in cs[:-1] if c.ts >= migrated_ts - 60] or [0]) or None
    else:
        f["since_migration_min"] = None
    f["price_change_m5"] = md.price_change_m5 if md else None
    return f


def run_detectors(f: dict, universe: str) -> dict[str, Det]:
    P = UNIVERSE_PARAMS.get(universe, UNIVERSE_PARAMS[U_NEW])
    d = {k: Det(k, BEAR if k in ("pump_exhaustion", "sudden_dump") else BULL) for k in DETECTORS}
    lf = f.get("liq_factor") or 0.0
    enough = f["n"] >= P["min_candles"]
    r5, z, z5 = f.get("ret_5m"), f.get("vol_z", 0.0), f.get("vol5_z", 0.0)
    ratio = f.get("buy_ratio_5m")

    # 1. early pump: fast rise with a volume or buyer burst, weighted by liquidity
    if r5 is not None and r5 >= P["ep_ret5"] and (z >= P["ep_z"] or (ratio or 0) >= P["pr_ratio"]):
        d["early_pump"] = Det("early_pump", BULL, True, min(1.0, r5 / (2 * P["ep_ret5"])) * (0.5 + 0.5 * lf),
                              f"+{r5:.1f}% in 5m, vol z {z:.1f}")
    # 2. first green candle after a red run
    if enough and f.get("last_green") and f.get("reds_before", 0) >= P["fg_reds"] and f["last_body_pct"] >= P["fg_body"] \
            and (f.get("last_vol_vs_median") or 0) >= 1.0:
        d["first_green_candle"] = Det("first_green_candle", BULL, True, min(1.0, f["last_body_pct"] / (3 * P["fg_body"])),
                                      f"green +{f['last_body_pct']:.1f}% after {f['reds_before']} red")
    # 3. first pullback: impulse, a 10-35% (new) dip that holds 38.2% of the move, then a bounce
    imp, pb = f.get("impulse_pct"), f.get("pullback_pct")
    if enough and imp and pb is not None and imp >= P["pb_impulse"] and P["pb_min"] <= pb <= P["pb_max"] \
            and f.get("dd_during_impulse", 99) < P["pb_min"] and f.get("last_green") \
            and f.get("pullback_low") and f["pullback_low"] > f["low_before_peak"] + 0.382 * (f["peak"] - f["low_before_peak"]) \
            and f.get("peak_idx", 0) < f["n"] - 2:
        mid = (P["pb_min"] + P["pb_max"]) / 2
        s = 1 - abs(pb - mid) / ((P["pb_max"] - P["pb_min"]) / 2)
        d["first_pullback"] = Det("first_pullback", BULL, True, max(0.3, min(1.0, 0.5 + 0.5 * s)),
                                  f"impulse +{imp:.0f}%, pullback -{pb:.1f}%, bouncing")
    # 4. volume increase (1m z-score confirmed on 5m)
    if enough and z >= P["vi_z"] and (f.get("n5", 0) < 3 or z5 >= P["vi_z"] * 0.6):
        d["volume_increase"] = Det("volume_increase", BULL, True, min(1.0, z / (2 * P["vi_z"])), f"vol z {z:.1f} / 5m z {z5:.1f}")
    # 5. buyer increase (more buys per minute and a rising buy ratio)
    g = f.get("buyer_growth")
    if g and g >= P["bi_growth"] and f.get("buy_ratio_slope", 0) >= 0:
        d["buyer_increase"] = Det("buyer_increase", BULL, True, min(1.0, (g - 1) / (2 * (P["bi_growth"] - 1))),
                                  f"buys x{g:.1f}, ratio slope {f['buy_ratio_slope']:+.3f}")
    # 6. buy/sell pressure
    if ratio is not None and f.get("trades_5m", 0) >= P["pr_min_trades"] and ratio >= P["pr_ratio"]:
        d["buy_sell_pressure"] = Det("buy_sell_pressure", BULL, True,
                                     min(1.0, (ratio - 0.5) / (2 * (P["pr_ratio"] - 0.5))),
                                     f"{100 * ratio:.0f}% buys of {f['trades_5m']} trades")
    # 7. breakout above the lookback high with volume
    ph, px = f.get("prior_high"), f.get("price")
    if enough and ph and px and f["n"] > P["bo_lookback"] // 2 and px > ph * 1.005 and z >= P["bo_z"]:
        d["breakout"] = Det("breakout", BULL, True, min(1.0, (px / ph - 1) * 100 / 5) * (0.5 + 0.5 * lf),
                            f"above {P['bo_lookback']}m high by {(px / ph - 1) * 100:.1f}%")
    # 8. pump exhaustion (bearish): extended move, wicks/lower highs, fading volume or buy ratio
    ext = max(f.get("ret_15m") or 0, f.get("ret_30m") or 0)
    if enough and ext >= P["ex_ext"] and (f.get("upper_wick", 0) >= 0.5 or f.get("lower_highs")) \
            and ((f.get("vol_fade") or 1) < 0.6 or f.get("buy_ratio_slope", 0) < 0):
        d["pump_exhaustion"] = Det("pump_exhaustion", BEAR, True, min(1.0, ext / (2 * P["ex_ext"])),
                                   f"+{ext:.0f}% then wicks/lower highs, volume fading")
    # 9. migration breakout
    sm, pmh = f.get("since_migration_min"), f.get("post_mig_high")
    if sm is not None and sm <= P["mb_window"] and pmh and px and px > pmh * 1.003 and (ratio or 0) >= 0.55:
        d["migration_breakout"] = Det("migration_breakout", BULL, True, min(1.0, 0.5 + (px / pmh - 1) * 10),
                                      f"{sm:.0f} min after migration, above post-migration high")
    # 10. sudden dump warning (bearish)
    r3, r1, sr = f.get("ret_3m"), f.get("ret_1m"), f.get("sell_ratio_1m")
    pc5 = f.get("price_change_m5")
    if (r3 is not None and r3 <= P["dump_ret3"]) or (r1 is not None and r1 <= P["dump_ret3"] / 2 and (sr or 0) >= P["dump_sell"]) \
            or (pc5 is not None and pc5 <= P["dump_ret3"] * 1.5):
        worst = min(x for x in (r3, r1, pc5) if x is not None)
        d["sudden_dump"] = Det("sudden_dump", BEAR, True, min(1.0, worst / (2 * P["dump_ret3"])), f"{worst:.1f}% drop")
    return d


def score_signal(det: dict[str, Det], f: dict, safety_score: int | None) -> tuple[float, dict]:
    pats = sorted((det[k].strength for k in PATTERNS if det[k].fired), reverse=True)
    pattern = min(35.0, 35 * pats[0] + 4 * (len(pats) - 1)) if pats else 0.0
    flow = sum(w * det[k].strength for k, w in FLOW_WEIGHTS.items() if det[k].fired)
    quality = 15 * (safety_score or 0) / 100 + (8 if f.get("mtf_up") else 0) + 7 * (f.get("liq_factor") or 0)
    if f.get("mtf_down"):
        quality -= 8
    bear = (25 * det["pump_exhaustion"].strength if det["pump_exhaustion"].fired else 0) + \
           (40 * det["sudden_dump"].strength if det["sudden_dump"].fired else 0)
    total = max(0.0, min(100.0, pattern + flow + quality - bear))
    return round(total, 1), {"pattern": round(pattern, 1), "flow": round(flow, 1), "quality": round(quality, 1),
                             "bear": round(bear, 1)}


def classify(score: float, critical: bool = False) -> str:
    if critical:
        return REJECT_CLASS
    return STRONG_WATCH if score >= 80 else WATCH if score >= 65 else WEAK if score >= 50 else REJECT_CLASS


@dataclass
class Evaluation:
    mint: str
    universe: str
    features: dict
    detections: dict
    score: float
    parts: dict
    cls: str
    candidates: list = field(default_factory=list)   # [(strategy_name, ok, why)]


class Sources:
    """One view over the new-launch collector and the universe collector (first match wins)."""

    def __init__(self, *collectors):
        self.collectors = [c for c in collectors if c is not None]

    def tracked_mints(self) -> list[str]:
        seen, out = set(), []
        for c in self.collectors:
            for m in c.tracked_mints():
                if m not in seen:
                    seen.add(m)
                    out.append(m)
        return out

    def owner(self, mint: str):
        for c in self.collectors:
            if mint in getattr(c, "tracked", {}):
                return c
        return None

    def get_market(self, mint: str) -> MarketData | None:
        c = self.owner(mint)
        return c.get_market(mint) if c else (self.collectors[0].get_market(mint) if self.collectors else None)

    def get_candles(self, mint: str, minutes: int = 60):
        c = self.owner(mint)
        return c.get_candles(mint, minutes) if c else []

    def universe_of(self, mint: str) -> str:
        md = self.get_market(mint)
        c = self.owner(mint)
        if c is not None and c is self.collectors[0]:
            return U_NEW
        return (md.universe if md and md.universe else U_NEW)


class SignalEngine:
    def __init__(self, settings: Settings, db: Database, sources: Sources, *, checker=None, judge=None,
                 clock=time.time, on_signal=None):
        self.s, self.db, self.src = settings, db, sources
        self.checker, self.judge = checker, judge
        self.clock = clock
        self.on_signal = on_signal
        self.strategies: dict[str, Strategy] = load_strategies(db)
        self.proximity: dict[str, tuple[float, float]] = {}
        self.last_signal: dict[tuple, float] = {}
        self.pending: dict[tuple, float] = {}           # waiting for a fresh quote since ts
        self.open: dict[int, dict] = {}                 # open BUY WATCH (approved and vetoed, tracked virtually)
        self.stats: Counter = Counter()
        self.by_universe: dict[str, Counter] = {u: Counter() for u in (U_NEW, U_MCAP, U_VOLUME)}
        self.veto_reasons: Counter = Counter()
        self.errors: deque = deque(maxlen=50)
        self._stop = asyncio.Event()
        if checker is not None:
            checker.near_fn = self.near_signal

    # ---- quote proximity for the safety checker (replaces the phase-4 volume placeholder)
    def near_signal(self, mint: str, md=None, rep=None) -> bool:
        p = self.proximity.get(mint)
        return bool(p and p[0] >= self.s.quote_near_score and self.clock() - p[1] <= 60 and not (rep and rep.critical))

    # ---- evaluation (pure apart from reading candles/market)
    def evaluate(self, mint: str) -> Evaluation | None:
        md = self.src.get_market(mint)
        if md is None or not md.price_usd:
            return None
        universe = self.src.universe_of(mint)
        now = self.clock()
        migrated_ts = None
        seen = self.db.get_seen(mint)
        if seen and seen.get("migrated_ts"):
            migrated_ts = seen["migrated_ts"]
        candles = self.src.get_candles(mint, 60)
        f = compute_features(candles, md, universe, now, migrated_ts)
        det = run_detectors(f, universe)
        rep = self.checker.get_report(mint) if self.checker else None
        score, parts = score_signal(det, f, rep.score if rep else None)
        crit = bool(rep and rep.critical)
        ev = Evaluation(mint, universe, f, det, score, parts, classify(score, crit))
        for name, st in self.strategies.items():
            if st.applies(universe):
                ok, why = st.entry_conditions(universe, det, f)
                ev.candidates.append((name, ok, why))
        return ev

    # ---- signal recording
    def _row(self, ev: Evaluation, strategy: Strategy | None, alert: str, reason: str, rep=None, levels=None,
             parent_id=None, status=None) -> dict:
        md = self.src.get_market(ev.mint)
        fired = {k: {"s": round(d.strength, 2), "d": d.detail} for k, d in ev.detections.items() if d.fired}
        feat = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ev.features.items()
                if k in ("ret_1m", "ret_5m", "ret_15m", "ret_60m", "vol_z", "vol5_z", "buy_ratio_5m", "buy_ratio_slope",
                         "buyer_growth", "trades_5m", "impulse_pct", "pullback_pct", "liq_factor", "mtf_up", "mtf_down",
                         "n", "age_min", "since_migration_min")}
        feat["parts"] = ev.parts
        lv = levels or {}
        return {"ts": self.clock(), "mint": ev.mint, "symbol": md.symbol if md else "", "universe": ev.universe,
                "strategy": strategy.name if strategy else None, "alert_type": alert, "score": ev.score, "class": ev.cls,
                "params_version": strategy.version if strategy else None, "price": ev.features.get("price"),
                "mcap": ev.features.get("mcap"), "liquidity": ev.features.get("liquidity"), "entry": lv.get("entry"),
                "sl": lv.get("sl"), "tp": lv.get("tp"), "time_stop_min": lv.get("time_stop_min"),
                "detectors_json": json.dumps(fired), "features_json": json.dumps(feat),
                "safety_verdict": rep.verdict if rep else None, "safety_score": rep.score if rep else None,
                "critical_json": json.dumps(rep.critical) if rep else None, "parent_id": parent_id, "reason": reason,
                "status": status}

    def _emit(self, row: dict, verdict=None) -> int:
        sid = self.db.add_signal(row)
        row["id"] = sid
        self.stats[f"alert_{row['alert_type']}"] += 1
        self.by_universe.setdefault(row["universe"], Counter())[f"{row['alert_type']}:{row.get('strategy') or '-'}"] += 1
        if self.on_signal:
            self.on_signal(row, verdict)
        return sid

    async def handle_candidate(self, ev: Evaluation, st: Strategy, why: str) -> None:
        key = (ev.mint, st.name)
        now = self.clock()
        if now - self.last_signal.get(key, 0) < self.s.signal_cooldown_min * 60:
            return
        if any(o["mint"] == ev.mint and o["strategy"] == st.name for o in self.open.values()):
            return
        rkey = (ev.mint, REJECT)        # one REJECT per coin per cooldown, not one per strategy
        rep = self.checker.get_report(ev.mint) if self.checker else None
        if rep is None or rep.expired(now):
            self.stats["waiting_safety"] += 1
            if self.checker:
                self.checker.prioritize(ev.mint)
            return
        p = st.levels(ev.universe, ev.features["price"], ev.features)
        if rep.critical:
            self.last_signal[key] = now
            if now - self.last_signal.get(rkey, 0) >= self.s.signal_cooldown_min * 60:
                self.last_signal[rkey] = now
                self._emit(self._row(ev, st, REJECT, f"critical safety: {', '.join(rep.critical)}", rep))
            return
        if rep.verdict != PASS:
            self.last_signal[key] = now
            if now - self.last_signal.get(rkey, 0) >= self.s.signal_cooldown_min * 60:
                self.last_signal[rkey] = now
                self._emit(self._row(ev, st, REJECT, f"safety {rep.verdict} (score {rep.score})", rep))
            return
        if ev.score < p["min_score"]:
            self.last_signal[key] = now
            if now - self.last_signal.get(rkey, 0) >= self.s.signal_cooldown_min * 60:
                self.last_signal[rkey] = now
                self._emit(self._row(ev, st, REJECT, f"score {ev.score:.1f} < {p['min_score']} ({ev.cls})", rep))
            return
        # eligible: the judge needs a fresh sell quote
        if not rep.quote_fresh(self.s.safety_quote_ttl_sec, now):
            first = self.pending.setdefault(key, now)
            if now - first < 90:
                if self.checker:
                    self.checker.request_quote(ev.mint, priority=True)
                self.stats["waiting_quote"] += 1
                return
        self.pending.pop(key, None)
        self.last_signal[key] = now
        row = self._row(ev, st, BUY_WATCH, why, rep, levels=p, status="open")
        row["min_score"] = p["min_score"]
        verdict = None
        if self.judge is not None:
            verdict = self.judge.review(row, rep, self.src.get_market(ev.mint), ev, self.judge_context(ev, st))
            row["judge_verdict"], row["judge_confidence"] = verdict.verdict, verdict.confidence
        sid = self._emit(row, verdict)
        if verdict is not None:
            self.judge.record(sid, row, verdict)
            self.stats[f"judge_{verdict.verdict}"] += 1
            if verdict.verdict == "VETO":
                for r in verdict.reasons:
                    if r["kind"] == "hard":
                        self.veto_reasons[r["code"]] += 1
        md = self.src.get_market(ev.mint)
        self.open[sid] = {"id": sid, "mint": ev.mint, "strategy": st.name, "universe": ev.universe, "entry": p["entry"],
                          "sl": p["sl"], "tp": p["tp"], "time_stop_min": p["time_stop_min"], "ts": now,
                          "max_up": 0.0, "max_down": 0.0, "last_hold": now, "approved": bool(verdict and verdict.verdict == "APPROVE"),
                          "symbol": md.symbol if md else "", "creator": (self.db.get_seen(ev.mint) or {}).get("creator")}

    def judge_context(self, ev: Evaluation, st: Strategy) -> dict:
        approved_open = [o for o in self.open.values() if o["approved"]]
        closed = self.db.closed_signals(st.name)
        return {"open_approved": approved_open, "strategy_closed": closed[-50:], "judge_history": self.db.judge_history(),
                "creator": (self.db.get_seen(ev.mint) or {}).get("creator")}

    # ---- open watches: SELL / HOLD and virtual outcomes (approved and vetoed alike)
    def manage_open(self, evals: dict[str, Evaluation]) -> None:
        now = self.clock()
        for sid, o in list(self.open.items()):
            ev = evals.get(o["mint"])
            md = self.src.get_market(o["mint"])
            price = (ev.features.get("price") if ev else None) or (md.price_usd if md else None)
            if not price:
                if o["mint"] not in self.src.tracked_mints() and now - o["ts"] > 120:
                    self._close(o, None, "lost", ev)
                continue
            chg = (price / o["entry"] - 1) * 100
            o["max_up"], o["max_down"] = max(o["max_up"], chg), min(o["max_down"], chg)
            reason = None
            if price <= o["sl"]:
                reason = "stop_loss"
            elif price >= o["tp"]:
                reason = "take_profit"
            elif ev and ev.detections["sudden_dump"].fired:
                reason = "dump_warning"
            elif ev and ev.detections["pump_exhaustion"].fired and ev.detections["pump_exhaustion"].strength >= 0.5:
                reason = "exhaustion"
            elif now - o["ts"] >= o["time_stop_min"] * 60:
                reason = "time_stop"
            if reason:
                self._close(o, price, reason, ev)
            elif o["approved"] and now - o["last_hold"] >= self.s.signal_hold_every_min * 60 and ev:
                o["last_hold"] = now
                st = self.strategies.get(o["strategy"])
                self._emit(self._row(ev, st, HOLD, f"open {chg:+.1f}% (max +{o['max_up']:.1f}% / {o['max_down']:.1f}%)",
                                     parent_id=sid))

    def _close(self, o: dict, price: float | None, reason: str, ev: Evaluation | None) -> None:
        self.open.pop(o["id"], None)
        pnl = (price / o["entry"] - 1) * 100 if price else None
        now = self.clock()
        self.db.update_signal(o["id"], status="closed", exit_ts=now, exit_price=price, exit_reason=reason, pnl_pct=pnl,
                              max_up_pct=o["max_up"], max_down_pct=o["max_down"])
        outcome = "unknown" if pnl is None else ("win" if pnl > 0 else "loss")
        self.db.set_judge_outcome(o["id"], outcome, pnl, now)
        self.stats[f"closed_{reason}"] += 1
        self.stats["closed_approved" if o["approved"] else "closed_vetoed"] += 1
        self.stats[f"outcome_{'approved' if o['approved'] else 'vetoed'}_{outcome}"] += 1
        if ev is not None and o["approved"]:     # vetoed watches are tracked silently (judge accuracy only)
            st = self.strategies.get(o["strategy"])
            self._emit(self._row(ev, st, SELL, f"{reason} {'' if pnl is None else f'{pnl:+.1f}%'} "
                                               f"({'approved' if o['approved'] else 'vetoed'} watch)", parent_id=o["id"]))

    # ---- loop
    async def step(self) -> int:
        evals: dict[str, Evaluation] = {}
        now = self.clock()
        for m in self.src.tracked_mints():
            try:
                ev = self.evaluate(m)
            except Exception as e:  # one bad coin must not stop the engine
                self.stats["errors"] += 1
                self.errors.append(f"{time.strftime('%H:%M:%S')} evaluate {m[:8]}: {type(e).__name__}: {e}")
                continue
            if ev is None:
                continue
            evals[m] = ev
            self.stats["evaluations"] += 1
            bu = self.by_universe.setdefault(ev.universe, Counter())
            bu["evaluated"] += 1
            for k, d in ev.detections.items():
                if d.fired:
                    self.stats[f"det_{k}"] += 1
                    bu[k] += 1
            ok_strats = [(self.strategies[n], why) for n, ok, why in ev.candidates if ok]
            if ok_strats:
                self.proximity[m] = (ev.score, now)
            for st, why in ok_strats:
                await self.handle_candidate(ev, st, why)
        for m in [m for m, (_, ts) in self.proximity.items() if now - ts > 300]:
            self.proximity.pop(m, None)
        self.manage_open(evals)
        return len(evals)

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["errors"] += 1
                self.errors.append(f"{time.strftime('%H:%M:%S')} step: {type(e).__name__}: {e}")
                log.exception("signal step failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.s.signal_eval_sec)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()


__all__ = ["SignalEngine", "Sources", "Evaluation", "Det", "compute_features", "run_detectors", "score_signal",
           "classify", "resample", "UNIVERSE_PARAMS", "DETECTORS", "BUY_WATCH", "SELL", "HOLD", "REJECT"]
