"""PHASE 5 - Judge agent: an independent, rule-based reviewer of every BUY WATCH signal (free, no LLM).

Inputs: the signal (strategy, score, levels), the safety report (incl. the Jupiter sell quote), market
context and features, recent judge accuracy and strategy health (from virtually tracked outcomes of
earlier approved AND vetoed watches). Output: APPROVE or VETO, a confidence 0-1 and every reason.

Hard vetoes (any one = VETO): safety not PASS, no fresh sell quote, sell price impact or value loss too
high (spread proxy), liquidity too thin for the universe, late entry after an extended move, a
conflicting dump / exhaustion warning, too many open watches, correlated coins (same creator, same
symbol or name), a strategy in drawdown.
Soft reasons lower the confidence (weak flow, small score margin, no 5m confirmation, few candles,
judge doing worse than chance). APPROVE needs no hard veto and confidence >= JUDGE_MIN_CONFIDENCE.
Every verdict is stored in `judge_verdicts` with its inputs; outcomes are filled in when the virtual
watch closes, so phase 6 can measure approved vs vetoed results.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from .config import Settings
from .db import Database
from .models import U_NEW
from .safety import PASS

PHASE = 5
IMPLEMENTED = True
JUDGE_VERSION = "judge-1"
APPROVE, VETO = "APPROVE", "VETO"

# late-entry limits per universe: (max % over 15 min, max % over 60 min)
# v1.2: mcap / volume limits relaxed (were 15/40 and 25/60): a +22% 15m breakout of a $17M coin was vetoed
LATE_ENTRY = {"new": (60.0, 200.0), "volume": (40.0, 100.0), "mcap": (30.0, 80.0)}


@dataclass
class JudgeVerdict:
    verdict: str
    confidence: float
    reasons: list = field(default_factory=list)   # [{"code", "kind": hard|soft|info, "detail"}]
    version: str = JUDGE_VERSION

    @property
    def approved(self) -> bool:
        return self.verdict == APPROVE

    def hard_codes(self) -> list[str]:
        return [r["code"] for r in self.reasons if r["kind"] == "hard"]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def strategy_health(closed: list[dict]) -> dict:
    """From closed virtual watches of one strategy: n, win rate, summed % and peak-to-trough drawdown."""
    pnls = [c["pnl_pct"] for c in closed if c.get("pnl_pct") is not None]
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    last5 = pnls[-5:]
    return {"n": len(pnls), "win_rate": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else None,
            "sum_pct": round(eq, 2), "drawdown_pct": round(dd, 2),
            "losing_streak": len(last5) == 5 and all(p <= 0 for p in last5)}


def judge_accuracy(history: list[dict]) -> dict:
    """Approved vs vetoed outcomes of past verdicts (vetoed ones are tracked virtually)."""
    def rate(rows):
        rows = [r for r in rows if r.get("outcome") in ("win", "loss")]
        return (sum(1 for r in rows if r["outcome"] == "win") / len(rows), len(rows)) if rows else (None, 0)
    a, na = rate([h for h in history if h["verdict"] == APPROVE])
    v, nv = rate([h for h in history if h["verdict"] == VETO])
    return {"approved_win_rate": a, "approved_n": na, "vetoed_win_rate": v, "vetoed_n": nv}


# Thresholds phase-6 learning may tune (stored as versions of strategy "judge" in strategy_params)
JUDGE_TUNABLE = ("min_confidence", "max_impact_pct", "max_sell_loss_pct", "min_liq_new", "min_liq_large")
JUDGE_LIMITS = {"min_confidence": (0.30, 0.90), "max_impact_pct": (0.5, 10.0), "max_sell_loss_pct": (2.0, 25.0),
                "min_liq_new": (3000.0, 100000.0), "min_liq_large": (20000.0, 1000000.0)}


def default_judge_params(s: Settings) -> dict:
    return {"min_confidence": s.judge_min_confidence, "max_impact_pct": s.judge_max_impact_pct,
            "max_sell_loss_pct": s.judge_max_sell_loss_pct, "min_liq_new": s.judge_min_liq_new,
            "min_liq_large": s.judge_min_liq_large}


def ensure_judge_params(db: Database, s: Settings) -> dict:
    """Seed version 1 of the judge thresholds from .env; return the active row."""
    row = db.active_strategy_params("judge")
    if row is None and not db.strategy_param_versions("judge"):
        db.save_strategy_params("judge", 1, default_judge_params(s), [], "default", note="phase 6: thresholds from .env")
        row = db.active_strategy_params("judge")
    return row


class Judge:
    def __init__(self, settings: Settings, db: Database | None = None):
        self.s, self.db = settings, db
        self.params, self.params_version = default_judge_params(settings), 0
        self.reload()

    def reload(self) -> None:
        """Pick up tuned thresholds (phase-6 learning) - the .env values are version 1."""
        if self.db is None:
            return
        row = ensure_judge_params(self.db, self.s)
        if row:
            self.params = {**default_judge_params(self.s), **row["params"]}
            self.params_version = row["version"]

    @property
    def version(self) -> str:
        return f"{JUDGE_VERSION}/p{self.params_version}"

    def review(self, signal: dict, report, md, ev, context: dict) -> JudgeVerdict:
        s, P = self.s, self.params
        reasons: list[dict] = []
        hard = lambda c, d: reasons.append({"code": c, "kind": "hard", "detail": d})  # noqa: E731
        soft = lambda c, d, pen: reasons.append({"code": c, "kind": "soft", "detail": d, "penalty": pen})  # noqa: E731
        f = ev.features
        universe = signal.get("universe") or U_NEW
        now = signal.get("ts") or time.time()

        # safety + sellability
        if report is None or report.verdict != PASS or report.critical:
            hard("safety_not_pass", f"safety {getattr(report, 'verdict', 'missing')}")
        q = (report.data.get("quote") if report else None) or {}
        if not report or not report.quote_fresh(s.safety_quote_ttl_sec, now) or q.get("sellable") is not True:
            age = (now - report.quote_ts) if report and report.quote_ts else None
            hard("stale_quote", "no sell quote" if age is None else f"sell quote {age:.0f}s old")
        else:
            imp = q.get("impact_pct") or 0.0
            if imp > P["max_impact_pct"]:
                hard("impact", f"selling ${s.paper_trade_size:g} moves price {imp:.1f}% (max {P['max_impact_pct']:g}%)")
            loss = report.data.get("sell_loss_pct")
            if loss is not None and loss > P["max_sell_loss_pct"]:
                hard("spread", f"round trip loses {loss:.1f}% (max {P['max_sell_loss_pct']:g}%)")
        # liquidity for the size and the universe
        liq = f.get("liquidity") or 0
        need = P["min_liq_new"] if universe == U_NEW else P["min_liq_large"]
        if liq < need:
            hard("thin_liquidity", f"liquidity ${liq:,.0f} < ${need:,.0f} for {universe} coins")
        # late entry after an extended move
        lim15, lim60 = LATE_ENTRY.get(universe, LATE_ENTRY["new"])
        r15, r60 = f.get("ret_15m"), f.get("ret_60m")
        if (r15 is not None and r15 > lim15) or (r60 is not None and r60 > lim60):
            fmt = lambda r: "n/a" if r is None else f"{r:+.0f}%"  # noqa: E731
            hard("late_entry", f"already {fmt(r15)} in 15m / {fmt(r60)} in 60m (limits {lim15:g}% / {lim60:g}%)")
        # conflicting warnings
        det = ev.detections
        if det["sudden_dump"].fired:
            hard("dump_conflict", det["sudden_dump"].detail)
        if det["pump_exhaustion"].fired and det["pump_exhaustion"].strength >= 0.4:
            hard("exhaustion_conflict", det["pump_exhaustion"].detail)
        # exposure and correlation
        open_ = context.get("open_approved") or []
        if len(open_) >= s.paper_max_open:
            hard("max_open", f"{len(open_)} approved watches open (max {s.paper_max_open})")
        sym, name = _norm(md.symbol if md else ""), _norm(md.name if md else "")
        creator = context.get("creator")
        corr = [o for o in open_ if (creator and o.get("creator") == creator)
                or (sym and _norm(o.get("symbol", "")) == sym)]
        if corr:
            hard("correlated", f"same creator/symbol as {len(corr)} open watch(es)")
        same_u = [o for o in open_ if o.get("universe") == universe and o.get("strategy") == signal.get("strategy")]
        if len(same_u) >= max(1, s.judge_max_correlated) + 1:
            hard("correlated", f"{len(same_u)} open {signal.get('strategy')} watches in {universe}")
        # strategy health
        h = strategy_health(context.get("strategy_closed") or [])
        if h["n"] >= 10 and (h["drawdown_pct"] >= s.judge_max_strategy_dd_pct or h["losing_streak"]):
            hard("strategy_drawdown", f"{signal.get('strategy')} drawdown {h['drawdown_pct']:.0f}% "
                                      f"over {h['n']} watches{' + 5 losses in a row' if h['losing_streak'] else ''}")
        elif h["n"] < 10:
            reasons.append({"code": "strategy_history", "kind": "info", "detail": f"only {h['n']} closed watches"})
        # soft reasons
        ratio = f.get("buy_ratio_5m")
        if ratio is not None and ratio < 0.55:      # v1.2: graded (a 46% buy ratio is not a sell-off)
            soft("weak_flow", f"only {100 * ratio:.0f}% buys in 5m", 0.10 if ratio < 0.40 else 0.05)
        if (f.get("buy_ratio_slope") or 0) < -0.02:
            soft("fading_buyers", "buy ratio falling", 0.05)
        margin = signal["score"] - (signal.get("min_score") or 65)
        if margin < 5:
            soft("thin_margin", f"score {signal['score']:.0f} just above the minimum", 0.10)
        if not f.get("mtf_up"):
            soft("no_5m_confirmation", "5m candle does not confirm", 0.10)
        if (f.get("n") or 0) < 10:
            soft("few_candles", f"only {f.get('n')} 1m candles", 0.10)
        acc = judge_accuracy(context.get("judge_history") or [])
        if acc["approved_n"] >= 20 and acc["vetoed_n"] >= 20 and acc["approved_win_rate"] < acc["vetoed_win_rate"]:
            soft("judge_underperforming", f"approved win {acc['approved_win_rate']:.0%} < vetoed "
                                          f"{acc['vetoed_win_rate']:.0%}", 0.05)
        # v1.2: confidence starts at 0.50 for a score exactly at the strategy's minimum and grows with the margin
        # (was score/100: with a 0.60 floor that silently overrode min_score 50-65 for every >= $1M setup)
        conf = 0.50 + margin / 100 + (0.05 if f.get("mtf_up") else 0) \
            - sum(r.get("penalty", 0) for r in reasons if r["kind"] == "soft")
        conf = round(max(0.0, min(1.0, conf)), 3)
        if any(r["kind"] == "hard" for r in reasons):
            verdict = VETO
        elif conf < P["min_confidence"]:
            verdict = VETO
            reasons.append({"code": "low_confidence", "kind": "hard",
                            "detail": f"confidence {conf:.2f} < {P['min_confidence']:.2f}"})
        else:
            verdict = APPROVE
        v = JudgeVerdict(verdict, conf, reasons, self.version)
        v.inputs = {"universe": universe, "liquidity": liq, "impact_pct": q.get("impact_pct"),
                    "sell_loss_pct": report.data.get("sell_loss_pct") if report else None, "confidence": conf,
                    "judge_params_version": self.params_version}
        return v

    def record(self, signal_id: int, signal: dict, v: JudgeVerdict) -> int | None:
        if self.db is None:
            return None
        inputs = {"score": signal.get("score"), "class": signal.get("class"), "price": signal.get("price"),
                  "liquidity": signal.get("liquidity"), "safety": signal.get("safety_verdict"),
                  "safety_score": signal.get("safety_score"), "params_version": signal.get("params_version"),
                  "features": json.loads(signal.get("features_json") or "{}"), **(getattr(v, "inputs", None) or {})}
        return self.db.add_judge_verdict({"signal_id": signal_id, "ts": signal.get("ts") or time.time(),
                                          "mint": signal["mint"], "universe": signal.get("universe"),
                                          "strategy": signal.get("strategy"), "verdict": v.verdict,
                                          "confidence": v.confidence, "reasons_json": json.dumps(v.reasons),
                                          "inputs_json": json.dumps(inputs), "judge_version": v.version})


__all__ = ["JUDGE_TUNABLE", "JUDGE_LIMITS", "default_judge_params", "ensure_judge_params", "Judge", "JudgeVerdict", "strategy_health", "judge_accuracy", "APPROVE", "VETO", "JUDGE_VERSION"]
