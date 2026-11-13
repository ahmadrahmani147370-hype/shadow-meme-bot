"""PHASE 6 - Guarded self-learning for strategy params and judge thresholds.

Data: every BUY WATCH (approved or vetoed) stores its price path after entry (`signals.path_json`,
followed for 2x the time stop, >= 60 min). A path lets us replay ANY exit params exactly, so candidate
params are compared on the same real opportunities, after fees and slippage.

Guardrails (all in .env):
  * minimum sample size: LEARN_MIN_TRADES closed samples per strategy+universe (LEARN_MIN_TEST of them
    in the newest part). v1.1: samples = every BUY WATCH (approved or vetoed) AND every SHADOW near miss
    (setup present, safety PASS, score up to SHADOW_SCORE_MARGIN under min_score), so learning sees what a
    lower min_score would have done; gains are shrunk by n/(n+LEARN_SHRINK_K) so small samples need
    proportionally bigger, out-of-sample-confirmed improvements;
  * walk-forward: the oldest LEARN_TRAIN_FRAC of samples chooses a candidate, the newest part (never
    seen while choosing) must confirm it; both must beat the current params by LEARN_MIN_IMPROVE_PCT;
  * bounded change: one param per step, at most LEARN_MAX_STEP_PCT, inside hard limits;
  * cooldown: one change per strategy (or the judge) per LEARN_COOLDOWN_H;
  * automatic rollback: after LEARN_ROLLBACK_MIN live samples a learned version is replayed against its
    parent on exactly those samples; if it is worse, the parent is re-activated;
  * every decision goes to `learning_log`; every applied change and rollback also creates a
    `strategy_updates` notification (old -> new values, why, before/after metrics with sample sizes,
    version) for Telegram (phase 8) and /strategies.
"""
from __future__ import annotations

import copy
import json
import logging
import time

from .config import Settings
from .db import Database
from .judge import APPROVE, JUDGE_LIMITS, JUDGE_TUNABLE, VETO, default_judge_params, ensure_judge_params
from .exits import ExitPolicy
from .strategies import LIMITS, STRATEGIES, TUNABLE, load_strategies, new_version, rollback

PHASE = 6
IMPLEMENTED = True
log = logging.getLogger("shadow.learning")
TUNED_VETO_CODES = {"low_confidence", "impact", "spread", "thin_liquidity"}


def cost_pct(s: Settings) -> float:
    """Round-trip cost in % of the trade size: both fees + slippage at the minimum (quote) level each way."""
    return (s.paper_buy_fee + s.paper_sell_fee) / s.paper_trade_size * 100 + 2 * s.paper_min_slippage_pct


# ---------------------------------------------------------------- replay
def simulate(strategy: str, p: dict, sample: dict, costs: float, policy=None) -> dict | None:
    """Replay one recorded opportunity with params p. Returns net % (after costs) and the exit reason.
    None if the path is too short to decide (the exit lies beyond the recorded horizon).
    v1.1.2: with a `run` ExitPolicy the exits are the policy's (TP 1000%, breakeven, tiered trail, partial) -
    the same rules as the paper engine; only SL / time stop / min_score come from p."""
    entry, pts = sample["entry"], sample["path"]
    if not entry or not pts:
        return None
    if (sample.get("score") or 0) < p.get("min_score", 0):
        return {"taken": False, "net": 0.0, "reason": "filtered"}
    if strategy == "first_pullback":
        dist = min(sample["sl_dist_pct"] or p["max_sl_pct"], p["max_sl_pct"])
    else:
        dist = p["sl_pct"]
    if policy is not None and policy.run:
        r = policy.replay(entry, pts, entry * (1 - dist / 100), p["time_stop_min"])
        return None if r is None else {"taken": True, "net": r[0] - costs, "reason": r[1]}
    sl, tp = entry * (1 - dist / 100), entry * (1 + p["tp_pct"] / 100)
    stop_t = p["time_stop_min"] * 60
    peak = entry
    for t, price in pts:
        if t > stop_t:
            return {"taken": True, "net": (price / entry - 1) * 100 - costs, "reason": "time_stop"}
        peak = max(peak, price)
        if price <= sl:
            return {"taken": True, "net": (price / entry - 1) * 100 - costs, "reason": "stop_loss"}
        if price >= tp:
            return {"taken": True, "net": (price / entry - 1) * 100 - costs, "reason": "take_profit"}
        if p.get("trail_pct") and (peak / entry - 1) * 100 >= p.get("trail_activate_pct", 1e9) \
                and price <= peak * (1 - p["trail_pct"] / 100):
            return {"taken": True, "net": (price / entry - 1) * 100 - costs, "reason": "trailing_stop"}
    if pts[-1][0] >= stop_t - 60:          # path reached the time stop
        return {"taken": True, "net": (pts[-1][1] / entry - 1) * 100 - costs, "reason": "time_stop"}
    return None


def evaluate(strategy: str, p: dict, samples: list[dict], costs: float, policy=None) -> dict:
    """Mean net % per OPPORTUNITY (filtered-out ones count as 0), so skipping losers is rewarded."""
    res = [simulate(strategy, p, x, costs, policy) for x in samples]
    res = [r for r in res if r is not None]
    taken = [r for r in res if r["taken"]]
    n = len(res)
    return {"n": n, "taken": len(taken), "score": (sum(r["net"] for r in taken) / n) if n else None,
            "mean_trade": (sum(r["net"] for r in taken) / len(taken)) if taken else None,
            "win_rate": (sum(1 for r in taken if r["net"] > 0) / len(taken)) if taken else None}


def candidates(cur: dict, keys: tuple, limits: dict, step_pct: float) -> list[tuple[str, float, float]]:
    out = []
    for k in keys:
        if k not in cur or cur[k] is None:
            continue
        v = cur[k]
        lo, hi = limits.get(k, (float("-inf"), float("inf")))
        if k == "min_score":
            steps = [v - 3, v + 3, v + 5]
        else:
            steps = [v * (1 - step_pct / 100), v * (1 - step_pct / 200), v * (1 + step_pct / 200), v * (1 + step_pct / 100)]
        for nv in steps:
            nv = min(max(nv, lo), hi)
            nv = int(round(nv)) if isinstance(v, int) else round(nv, 4)
            if nv != v and abs(nv - v) <= abs(v) * step_pct / 100 + (5 if k == "min_score" else 1e-9):
                out.append((k, v, nv))
    return out


def shrink(gain: float | None, n: int, k: float) -> float | None:
    """v1.1: James-Stein style shrinkage toward 'no change': a gain measured on n samples counts n/(n+k) of
    itself. With k=20, 12 samples need a raw gain 2.7x the threshold, 60 samples only 1.33x."""
    if gain is None:
        return None
    return gain * n / (n + k) if k > 0 else gain


def walk_forward(samples: list[dict], train_frac: float) -> tuple[list, list]:
    samples = sorted(samples, key=lambda x: x["ts"])
    k = int(len(samples) * train_frac)
    return samples[:k], samples[k:]


# ---------------------------------------------------------------- samples
def strategy_samples(db: Database, strategy: str, universe: str, version: int | None = None,
                     since_ts: float = 0) -> list[dict]:
    q = ("SELECT id, ts, score, entry, sl, path_json, params_version, judge_verdict FROM signals "
         "WHERE alert_type IN ('BUY_WATCH','SHADOW')"
         " AND strategy=? AND universe=? AND path_json IS NOT NULL AND ts>=?")
    a = [strategy, universe, since_ts]
    if version is not None:
        q, a = q + " AND params_version=?", a + [version]
    out = []
    for r in db.con.execute(q + " ORDER BY ts", a):
        path = json.loads(r["path_json"] or "[]")
        if len(path) < 2 or not r["entry"]:
            continue
        out.append({"id": r["id"], "ts": r["ts"], "score": r["score"], "entry": r["entry"], "path": path,
                    "sl_dist_pct": (1 - r["sl"] / r["entry"]) * 100 if r["sl"] else None,
                    "version": r["params_version"], "approved": r["judge_verdict"] == APPROVE})
    return out


def judge_samples(db: Database, since_ts: float = 0, params_version: int | None = None) -> list[dict]:
    out = []
    for r in db.con.execute("SELECT ts, verdict, confidence, reasons_json, inputs_json, outcome_pnl_pct FROM judge_verdicts "
                            "WHERE outcome IN ('win','loss') AND outcome_pnl_pct IS NOT NULL AND ts>=? ORDER BY ts",
                            (since_ts,)):
        inp = json.loads(r["inputs_json"] or "{}")
        if params_version is not None and inp.get("judge_params_version") != params_version:
            continue
        codes = {x["code"] for x in json.loads(r["reasons_json"] or "[]") if x.get("kind") == "hard"}
        out.append({"ts": r["ts"], "verdict": r["verdict"], "confidence": inp.get("confidence", r["confidence"]),
                    "liquidity": inp.get("liquidity", (inp.get("features") or {}).get("liquidity")),
                    "universe": inp.get("universe", "new"), "impact_pct": inp.get("impact_pct"),
                    "sell_loss_pct": inp.get("sell_loss_pct"), "other_hard": bool(codes - TUNED_VETO_CODES),
                    "pnl": r["outcome_pnl_pct"]})
    return out


def judge_decide(x: dict, P: dict) -> bool:
    """Would the judge APPROVE this past signal with thresholds P? (other hard vetoes stay as they were)"""
    if x["other_hard"]:
        return False
    if x["confidence"] is not None and x["confidence"] < P["min_confidence"]:
        return False
    if x["impact_pct"] is not None and x["impact_pct"] > P["max_impact_pct"]:
        return False
    if x["sell_loss_pct"] is not None and x["sell_loss_pct"] > P["max_sell_loss_pct"]:
        return False
    need = P["min_liq_new"] if x["universe"] == "new" else P["min_liq_large"]
    return not (x["liquidity"] is not None and x["liquidity"] < need)


def judge_eval(P: dict, samples: list[dict], costs: float) -> dict:
    taken = [x["pnl"] - costs for x in samples if judge_decide(x, P)]
    n = len(samples)
    return {"n": n, "taken": len(taken), "score": (sum(taken) / n) if n else None,
            "mean_trade": (sum(taken) / len(taken)) if taken else None}


# ---------------------------------------------------------------- judge accuracy
def judge_stats(db: Database, since_ts: float = 0, costs: float = 0.0) -> dict:
    rows = [dict(r) for r in db.con.execute(
        "SELECT verdict, outcome, outcome_pnl_pct, reasons_json FROM judge_verdicts WHERE ts>=?", (since_ts,))]
    def agg(rs):
        done = [r for r in rs if r["outcome"] in ("win", "loss") and r["outcome_pnl_pct"] is not None]
        return {"n": len(rs), "closed": len(done),
                "win_rate": (sum(1 for r in done if r["outcome"] == "win") / len(done)) if done else None,
                "avg_pnl_pct": (sum(r["outcome_pnl_pct"] for r in done) / len(done)) if done else None,
                "avg_net_pct": (sum(r["outcome_pnl_pct"] - costs for r in done) / len(done)) if done else None}
    app = [r for r in rows if r["verdict"] == APPROVE]
    vet = [r for r in rows if r["verdict"] == VETO]
    a, v = agg(app), agg(vet)
    vdone = [r for r in vet if r["outcome"] in ("win", "loss")]
    by_reason: dict[str, dict] = {}
    for r in vet:
        for x in json.loads(r["reasons_json"] or "[]"):
            if x.get("kind") != "hard":
                continue
            d = by_reason.setdefault(x["code"], {"n": 0, "closed": 0, "losses": 0, "sum_pnl": 0.0})
            d["n"] += 1
            if r["outcome"] in ("win", "loss") and r["outcome_pnl_pct"] is not None:
                d["closed"] += 1
                d["losses"] += r["outcome"] == "loss"
                d["sum_pnl"] += r["outcome_pnl_pct"]
    for d in by_reason.values():
        d["correct_rate"] = d["losses"] / d["closed"] if d["closed"] else None
        d["avg_pnl_pct"] = d.pop("sum_pnl") / d["closed"] if d["closed"] else None
    edge = (a["avg_pnl_pct"] - v["avg_pnl_pct"]) if a["avg_pnl_pct"] is not None and v["avg_pnl_pct"] is not None else None
    return {"approved": a, "vetoed": v, "veto_precision": (sum(1 for r in vdone if r["outcome"] == "loss") / len(vdone))
            if vdone else None, "edge_pct": edge, "by_reason": by_reason}


# ---------------------------------------------------------------- learner
class Learner:
    def __init__(self, settings: Settings, db: Database, clock=time.time, judge=None):
        self.s, self.db, self.clock, self.judge = settings, db, clock, judge
        self.policy = ExitPolicy.from_settings(settings)    # v1.1.2: replay with the live exit rules
        self.last_summary: list[dict] = []

    POLICY_KEYS = ("tp_pct", "trail_pct", "trail_activate_pct")

    def tunable(self, name: str) -> tuple:
        """v1.1.2: with the run exit policy, TP (1000%) and the trailing stop belong to the policy - the learner
        tunes only entries / SL / time stop and can never shrink the take profit back."""
        keys = TUNABLE[name]
        return tuple(k for k in keys if k not in self.POLICY_KEYS) if self.policy.run else keys

    def _last_change_ts(self, target: str) -> float:
        r = self.db.con.execute("SELECT MAX(ts) FROM strategy_updates WHERE target=?", (target,)).fetchone()
        return r[0] or 0.0

    def _log(self, target, action, reason, metrics=None, changes=None, fv=None, tv=None):
        self.db.add_learning_log(target, action, reason, metrics, changes, fv, tv, ts=self.clock())

    # -- strategies
    def learn_strategy(self, name: str) -> list[dict]:
        s, costs, out = self.s, cost_pct(self.s), []
        st = load_strategies(self.db)[name]
        rb = self.check_rollback(name, st, costs)
        if rb:
            return [rb]
        if self.clock() - self._last_change_ts(name) < s.learn_cooldown_h * 3600:
            return [{"target": name, "action": "cooldown"}]
        best, evaluated = None, False
        for u in st.universes:
            samples = strategy_samples(self.db, name, u)
            cur = st.p(u)
            train, test = walk_forward(samples, s.learn_train_frac)
            if len(samples) < s.learn_min_trades or len(test) < s.learn_min_test:
                out.append({"target": name, "universe": u, "action": "insufficient", "n": len(samples),
                            "need": s.learn_min_trades})
                continue
            base_tr, base_te = evaluate(name, cur, train, costs, self.policy), evaluate(name, cur, test, costs, self.policy)
            if base_tr["score"] is None or base_te["score"] is None:
                continue
            evaluated = True
            for k, old, new in candidates(cur, self.tunable(name), LIMITS, s.learn_max_step_pct):
                cand = {**cur, k: new}
                tr = evaluate(name, cand, train, costs, self.policy)
                if tr["score"] is None:
                    continue
                g = shrink(tr["score"] - base_tr["score"], tr["n"], s.learn_shrink_k)
                if g < s.learn_min_improve_pct:
                    continue
                if best is None or g > best["gain"]:
                    best = {"universe": u, "k": k, "old": old, "new": new, "gain": g,
                            "train": (base_tr, tr), "cand": cand, "test": test, "base_te": base_te}
        if best is None:
            if evaluated:
                out.append({"target": name, "action": "no_better_candidate"})
            return out
        te = evaluate(name, best["cand"], best["test"], costs, self.policy)
        base_te = best["base_te"]
        metrics = {"before": {"train": best["train"][0], "test": base_te}, "after": {"train": best["train"][1], "test": te},
                   "cost_pct": costs, "universe": best["universe"]}
        change = {f"{best['universe']}.{best['k']}": [best["old"], best["new"]]}
        if te["score"] is None or shrink(te["score"] - base_te["score"], te["n"], s.learn_shrink_k) < s.learn_min_improve_pct:
            self._log(name, "rejected_out_of_sample", f"{best['universe']}.{best['k']} {best['old']}->{best['new']} "
                      f"helped on train but not on the newest {te['n']} samples", metrics, change, st.version)
            out.append({"target": name, "action": "rejected_out_of_sample", "change": change})
            return out
        params = copy.deepcopy(st.params)
        params[best["universe"]] = {**st.p(best["universe"]), best["k"]: best["new"]}
        reason = (f"{best['universe']} {best['k']} {best['old']} -> {best['new']}: mean net per opportunity "
                  f"{base_te['score']:+.2f}% -> {te['score']:+.2f}% on the newest {te['n']} samples "
                  f"(train {best['train'][0]['score']:+.2f}% -> {best['train'][1]['score']:+.2f}% on {best['train'][0]['n']})")
        with self.db.tx():  # version + change log + notification: all or nothing (crash-safe)
            v = new_version(self.db, name, params, "learned", note=reason)
            self._log(name, "activate", reason, metrics, change, st.version, v)
            self.db.add_strategy_update("param_change", name, st.version, v, change, reason, metrics, ts=self.clock())
        out.append({"target": name, "action": "activate", "version": v, "change": change, "reason": reason})
        return out

    def check_rollback(self, name: str, st, costs: float) -> dict | None:
        row = self.db.active_strategy_params(name)
        if not row or row["source"] != "learned" or not row["parent_version"]:
            return None
        parent = self.db.strategy_params_version(name, row["parent_version"])
        if parent is None:
            return None
        from .strategies import Strategy
        pst = Strategy(name, parent["version"], parent["universes"], parent["params"])
        live_new, live_old, n = 0.0, 0.0, 0
        per = {}
        for u in st.universes:
            samples = strategy_samples(self.db, name, u, version=row["version"])
            a, b = evaluate(name, st.p(u), samples, costs, self.policy), evaluate(name, pst.p(u), samples, costs, self.policy)
            if a["n"]:
                live_new += a["score"] * a["n"]
                live_old += b["score"] * b["n"]
                n += a["n"]
                per[u] = {"new": a, "old": b}
        if n < self.s.learn_rollback_min:
            return None
        new_m, old_m = live_new / n, live_old / n
        metrics = {"before": {"version": parent["version"], "score": old_m, "n": n},
                   "after": {"version": row["version"], "score": new_m, "n": n}, "per_universe": per, "cost_pct": costs}
        if new_m < old_m - self.s.learn_rollback_margin_pct:
            with self.db.tx():  # version + change log + notification: all or nothing (crash-safe)
                rollback(self.db, name, parent["version"])
                changes = diff_params(st.params, pst.params)
                reason = (f"v{row['version']} did worse live than v{parent['version']} on the same {n} watches: "
                          f"{new_m:+.2f}% vs {old_m:+.2f}% net per opportunity -> back to v{parent['version']}")
                self._log(name, "rollback", reason, metrics, changes, row["version"], parent["version"])
                self.db.add_strategy_update("rollback", name, row["version"], parent["version"], changes, reason, metrics,
                                            ts=self.clock())
            return {"target": name, "action": "rollback", "version": parent["version"], "reason": reason}
        # probation passed: mark it so it is not re-judged every run
        with self.db.tx():
            self.db.con.execute("UPDATE strategy_params SET source='learned_confirmed' WHERE strategy=? AND version=?",
                                (name, row["version"]))
        self._log(name, "confirmed", f"v{row['version']} kept: {new_m:+.2f}% vs parent {old_m:+.2f}% on {n} live watches",
                  metrics, None, row["version"], row["version"])
        return None

    # -- judge
    def tune_judge(self) -> dict:
        s, costs = self.s, cost_pct(self.s)
        row = ensure_judge_params(self.db, s)
        cur = {**default_judge_params(s), **row["params"]}
        # rollback check for a learned judge version
        if row["source"] == "learned" and row["parent_version"]:
            parent = self.db.strategy_params_version("judge", row["parent_version"])
            live = judge_samples(self.db, params_version=row["version"])
            if parent and len(live) >= s.learn_rollback_min:
                P_old = {**default_judge_params(s), **parent["params"]}
                a, b = judge_eval(cur, live, costs), judge_eval(P_old, live, costs)
                metrics = {"before": {"version": parent["version"], **b}, "after": {"version": row["version"], **a},
                           "cost_pct": costs}
                if (a["score"] or 0) < (b["score"] or 0) - s.learn_rollback_margin_pct:
                    with self.db.tx():  # version + change log + notification: all or nothing (crash-safe)
                        rollback(self.db, "judge", parent["version"])
                        changes = {k: [cur[k], P_old[k]] for k in JUDGE_TUNABLE if cur[k] != P_old[k]}
                        reason = (f"judge thresholds v{row['version']} did worse than v{parent['version']} on the same "
                                  f"{a['n']} signals ({a['score']:+.2f}% vs {b['score']:+.2f}% net per signal)")
                        self._log("judge", "rollback", reason, metrics, changes, row["version"], parent["version"])
                        self.db.add_strategy_update("judge_rollback", "judge", row["version"], parent["version"], changes,
                                                    reason, metrics, ts=self.clock())
                    self._reload_judge()
                    return {"target": "judge", "action": "rollback", "reason": reason}
                with self.db.tx():
                    self.db.con.execute("UPDATE strategy_params SET source='learned_confirmed' WHERE strategy='judge' "
                                        "AND version=?", (row["version"],))
        if not s.judge_tuning_enabled:
            return {"target": "judge", "action": "disabled"}
        if self.clock() - self._last_change_ts("judge") < s.learn_cooldown_h * 3600:
            return {"target": "judge", "action": "cooldown"}
        samples = judge_samples(self.db)
        train, test = walk_forward(samples, s.learn_train_frac)
        if len(samples) < s.learn_min_trades or len(test) < s.learn_min_test:
            return {"target": "judge", "action": "insufficient", "n": len(samples), "need": s.learn_min_trades}
        btr, bte = judge_eval(cur, train, costs), judge_eval(cur, test, costs)
        best = None
        for k, old, new in candidates(cur, JUDGE_TUNABLE, JUDGE_LIMITS, s.learn_max_step_pct):
            cand = {**cur, k: new}
            tr = judge_eval(cand, train, costs)
            gain = shrink((tr["score"] or 0) - (btr["score"] or 0), len(train), s.learn_shrink_k)
            if gain >= s.learn_min_improve_pct and (best is None or gain > best[0]):
                best = (gain, k, old, new, cand, tr)
        if best is None:
            return {"target": "judge", "action": "no_better_candidate", "n": len(samples)}
        _, k, old, new, cand, tr = best
        te = judge_eval(cand, test, costs)
        metrics = {"before": {"train": btr, "test": bte}, "after": {"train": tr, "test": te}, "cost_pct": costs}
        if shrink((te["score"] or 0) - (bte["score"] or 0), len(test), s.learn_shrink_k) < s.learn_min_improve_pct:
            self._log("judge", "rejected_out_of_sample", f"{k} {old}->{new} not confirmed on the newest {te['n']}",
                      metrics, {k: [old, new]}, row["version"])
            return {"target": "judge", "action": "rejected_out_of_sample"}
        reason = (f"judge {k} {old} -> {new}: net per signal {bte['score']:+.2f}% -> {te['score']:+.2f}% on the newest "
                  f"{te['n']} signals (train {btr['score']:+.2f}% -> {tr['score']:+.2f}% on {tr['n']})")
        with self.db.tx():  # version + change log + notification: all or nothing (crash-safe)
            v = new_version(self.db, "judge", cand, "learned", note=reason)
            self._log("judge", "activate", reason, metrics, {k: [old, new]}, row["version"], v)
            self.db.add_strategy_update("judge_change", "judge", row["version"], v, {k: [old, new]}, reason, metrics,
                                        ts=self.clock())
        self._reload_judge()
        return {"target": "judge", "action": "activate", "version": v, "reason": reason}

    def _reload_judge(self) -> None:
        if self.judge is not None:
            self.judge.reload()

    def run_once(self) -> list[dict]:
        out = []
        if not self.s.learn_enabled:
            return [{"target": "all", "action": "disabled"}]
        for name in STRATEGIES:
            try:
                out += self.learn_strategy(name)
            except Exception as e:
                log.exception("learning %s failed", name)
                out.append({"target": name, "action": "error", "reason": f"{type(e).__name__}: {e}"})
        try:
            out.append(self.tune_judge())
        except Exception as e:
            log.exception("judge tuning failed")
            out.append({"target": "judge", "action": "error", "reason": f"{type(e).__name__}: {e}"})
        self.last_summary = out
        return out


def diff_params(a: dict, b: dict) -> dict:
    out = {}
    for u in set(a) | set(b):
        for k in set(a.get(u, {})) | set(b.get(u, {})):
            x, y = a.get(u, {}).get(k), b.get(u, {}).get(k)
            if x != y:
                out[f"{u}.{k}"] = [x, y]
    return out


__all__ = ["Learner", "simulate", "evaluate", "candidates", "walk_forward", "strategy_samples", "judge_samples",
           "judge_decide", "judge_eval", "judge_stats", "cost_pct", "diff_params"]
