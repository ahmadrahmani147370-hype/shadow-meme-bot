"""PHASE 5 - Three independent strategies with versioned parameters.

Each strategy declares the universes it applies to and keeps its own entry / stop-loss / take-profit /
time-stop parameters per universe. Parameters live in the SQLite table `strategy_params`
(strategy, version, active, source, parent_version, params_json): version 1 is seeded from the
defaults below; phase 6 self-learning writes new versions (source='learned') and can roll back by
re-activating an older version. Every signal stores the params_version it was produced with.

    early_entry         new launches only: early pump / first green candle / volume burst + buy pressure
    first_pullback      all universes: first 10-35% (new) / 3-12% (mcap/volume) pullback after an impulse, then a bounce
    migration_breakout  migrated pump.fun / launchpad coins breaking their post-migration high
    momentum_breakout   v1.1, coins >= $1M: breakout above the lookback high confirmed by volume, buy pressure
                        or the 5m candle, no exhaustion; tight trailing stop. Replay of the recorded 13 Nov data
                        is SMALL (8-10 closed setups, 50-75% win, avg -2.2% .. +1.3%): a starting point for the
                        learner / shadow tracking, not a proven edge.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from .db import Database
from .models import U_MCAP, U_NEW, U_VOLUME

PHASE = 5
IMPLEMENTED = True

DEFAULT_PARAMS: dict[str, dict] = {
    "early_entry": {
        "universes": [U_NEW],
        "params": {
            U_NEW: {"min_score": 70, "max_age_min": 60, "max_ext_60m_pct": 150.0, "sl_pct": 20.0, "tp_pct": 1000.0,
                    "time_stop_min": 20, "need_pressure": True,
                    "trail_pct": 10.0, "trail_activate_pct": 15.0},
        },
    },
    "first_pullback": {
        "universes": [U_NEW, U_MCAP, U_VOLUME],
        "params": {
            U_NEW: {"min_score": 65, "sl_below_low_pct": 3.0, "sl_pct": 20.0, "max_sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 45,
                    "trail_pct": 8.0, "trail_activate_pct": 12.0},
            U_MCAP: {"min_score": 65, "sl_below_low_pct": 1.5, "sl_pct": 20.0, "max_sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 120,
                     "trail_pct": 3.0, "trail_activate_pct": 5.0},
            U_VOLUME: {"min_score": 65, "sl_below_low_pct": 2.0, "sl_pct": 20.0, "max_sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 90,
                       "trail_pct": 4.0, "trail_activate_pct": 7.0},
        },
    },
    "migration_breakout": {
        "universes": [U_NEW, U_MCAP, U_VOLUME],
        "params": {
            U_NEW: {"min_score": 65, "max_since_migration_min": 60, "sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 30,
                    "trail_pct": 10.0, "trail_activate_pct": 15.0},
            U_MCAP: {"min_score": 65, "max_since_migration_min": 120, "sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 60,
                     "trail_pct": 4.0, "trail_activate_pct": 8.0},
            U_VOLUME: {"min_score": 65, "max_since_migration_min": 120, "sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 45,
                       "trail_pct": 6.0, "trail_activate_pct": 10.0},
        },
    },
    "momentum_breakout": {
        "universes": [U_NEW, U_MCAP, U_VOLUME],
        "params": {
            U_NEW: {"min_score": 60, "sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 45,
                    "trail_pct": 4.0, "trail_activate_pct": 7.0},
            U_MCAP: {"min_score": 50, "sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 120,
                     "trail_pct": 3.0, "trail_activate_pct": 5.0},
            U_VOLUME: {"min_score": 55, "sl_pct": 20.0, "tp_pct": 1000.0, "time_stop_min": 90,
                       "trail_pct": 3.5, "trail_activate_pct": 6.0},
        },
    },
}
STRATEGIES = tuple(DEFAULT_PARAMS)

# What phase-6 self-learning may tune (one param per step, bounded), with hard limits per param
TUNABLE: dict[str, tuple] = {
    "early_entry": ("sl_pct", "tp_pct", "time_stop_min", "trail_pct", "trail_activate_pct", "min_score"),
    "first_pullback": ("max_sl_pct", "tp_pct", "time_stop_min", "trail_pct", "trail_activate_pct", "min_score"),
    "migration_breakout": ("sl_pct", "tp_pct", "time_stop_min", "trail_pct", "trail_activate_pct", "min_score"),
    "momentum_breakout": ("sl_pct", "tp_pct", "time_stop_min", "trail_pct", "trail_activate_pct", "min_score"),
}
LIMITS = {"sl_pct": (2.0, 30.0), "max_sl_pct": (2.0, 30.0), "tp_pct": (1000.0, 100000.0),   # v1.1.2: never < 1000%
           "time_stop_min": (5, 240),
          "trail_pct": (1.0, 40.0), "trail_activate_pct": (1.0, 100.0), "min_score": (45, 90)}


@dataclass
class Strategy:
    name: str
    version: int
    universes: list
    params: dict

    def applies(self, universe: str) -> bool:
        return universe in self.universes and universe in self.params

    def p(self, universe: str) -> dict:
        """Stored params on top of the defaults (older versions may lack newer keys, e.g. trailing)."""
        base = DEFAULT_PARAMS.get(self.name, {}).get("params", {}).get(universe, {})
        return {**base, **(self.params.get(universe) or {})}

    def entry_conditions(self, universe: str, det: dict, feat: dict) -> tuple[bool, str]:
        """Does this strategy's setup exist right now? (score/safety/judge are checked afterwards)"""
        p = self.p(universe)
        fired = {k for k, d in det.items() if d.fired}
        if "sudden_dump" in fired:
            return False, "dump warning"
        if self.name == "early_entry":
            if feat.get("age_min") is not None and feat["age_min"] > p["max_age_min"]:
                return False, "too old for early entry"
            if not ({"early_pump", "first_green_candle", "volume_increase"} & fired):
                return False, "no early move"
            if p.get("need_pressure", True) and "buy_sell_pressure" not in fired:
                return False, "no buy pressure"
            if (feat.get("ret_60m") or 0) > p["max_ext_60m_pct"]:
                return False, "already extended"
            return True, "early move with buy pressure"
        if self.name == "first_pullback":
            if "first_pullback" not in fired:
                return False, "no pullback setup"
            return True, "first pullback bounce"
        if self.name == "migration_breakout":
            if "migration_breakout" not in fired:
                return False, "no migration breakout"
            since = feat.get("since_migration_min")
            if since is None or since > p["max_since_migration_min"]:
                return False, "migration too long ago"
            return True, "post-migration breakout"
        if self.name == "momentum_breakout":
            if "breakout" not in fired:
                return False, "no breakout"
            if "pump_exhaustion" in fired:
                return False, "exhaustion"
            conf = ({"volume_increase", "buy_sell_pressure"} & fired) or ({"mtf"} if feat.get("mtf_up") else set())
            if not conf:
                return False, "breakout without volume / buy pressure / 5m confirmation"
            return True, "breakout confirmed by " + ", ".join(sorted(conf))
        return False, "unknown strategy"

    def levels(self, universe: str, price: float, feat: dict) -> dict:
        p = self.p(universe)
        if "sl_pct" in p:            # v1.1.3: fixed SL from entry for every strategy (first_pullback too)
            sl = price * (1 - p["sl_pct"] / 100)
        elif self.name == "first_pullback" and feat.get("pullback_low"):
            sl = feat["pullback_low"] * (1 - p["sl_below_low_pct"] / 100)
            sl = max(sl, price * (1 - p["max_sl_pct"] / 100))
        else:
            sl = price * (1 - p["sl_pct"] / 100)
        return {"entry": price, "sl": sl, "tp": price * (1 + p["tp_pct"] / 100), "time_stop_min": p["time_stop_min"],
                "min_score": p["min_score"], "trail_pct": p.get("trail_pct"), "trail_activate_pct": p.get("trail_activate_pct")}


def ensure_defaults(db: Database) -> None:
    """Seed version 1 of every strategy (never overwrites learned versions)."""
    for name, d in DEFAULT_PARAMS.items():
        if db.active_strategy_params(name) is None and not db.strategy_param_versions(name):
            db.save_strategy_params(name, 1, d["params"], d["universes"], "default", note="phase 5 defaults")


def migrate_v11(db: Database) -> list[str]:
    """One-time: strategies still on their untouched phase-5 defaults get the v1.1 defaults as a new version
    (e.g. early_entry min_score 65 -> 70: in the recorded watches score < 70 won 46%, 70-80 won 61-71%).
    Learned versions are never overwritten. Recorded in strategy_updates, so /strategies shows it."""
    if db.meta_get("migrated_v11") == "1":
        return []
    done = []
    for name, d in DEFAULT_PARAMS.items():
        cur = db.active_strategy_params(name)
        if cur is None or cur.get("source") != "default" or cur["params"] == d["params"]:
            continue
        v = new_version(db, name, copy.deepcopy(d["params"]), "v1.1", note="v1.1 defaults (data-backed, 13 Nov analysis)")
        changes = {f"{u}.{k}": [cur["params"].get(u, {}).get(k), val] for u, ps in d["params"].items()
                   for k, val in ps.items() if cur["params"].get(u, {}).get(k) != val}   # same format as learning
        db.add_strategy_update("param_change", name, cur["version"], v, changes,
                               "v1.1 upgrade: stricter entry defaults from the 13 Nov win-rate analysis", {})
        done.append(name)
    db.meta_set("migrated_v11", "1")
    return done


def migrate_v112(db: Database, tp_pct: float = 1000.0) -> list[str]:
    """One-time (v1.1.2, Ahmed: "let winners run to 1000%"): every active strategy version with a take profit
    below 1000% gets a new version with tp_pct=1000 on all its universes; all other (possibly learned)
    params are kept. Recorded in strategy_updates for /strategies."""
    if db.meta_get("migrated_v112") == "1":
        return []
    done = []
    for name in DEFAULT_PARAMS:
        cur = db.active_strategy_params(name)
        if cur is None:
            continue
        params = copy.deepcopy(cur["params"])
        changes = {}
        for u, ps in params.items():
            if ps.get("tp_pct") is not None and ps["tp_pct"] < tp_pct:
                changes[f"{u}.tp_pct"] = [ps["tp_pct"], tp_pct]
                ps["tp_pct"] = tp_pct
        if not changes:
            continue
        v = new_version(db, name, params, "v1.1.2", note="let winners run: TP 1000% + breakeven / tiered trail")
        db.add_strategy_update("param_change", name, cur["version"], v, changes,
                               "v1.1.2: take profit raised to +1000%; profits protected by breakeven + tiered "
                               "trailing stop + 50% partial at +30% (exit policy)", {})
        done.append(name)
    db.meta_set("migrated_v112", "1")
    return done


def migrate_v113(db: Database, sl_pct: float = 20.0) -> list[str]:
    """One-time (v1.1.3, Ahmed: "stop loss 20% hoga"): every active strategy version whose stop loss is not
    exactly sl_pct gets a new version with sl_pct (and first_pullback's max_sl_pct) = 20; all other params kept."""
    if db.meta_get("migrated_v113") == "1":
        return []
    done = []
    for name in DEFAULT_PARAMS:
        cur = db.active_strategy_params(name)
        if cur is None:
            continue
        params = copy.deepcopy(cur["params"])
        changes = {}
        for u, ps in params.items():
            for k in ("sl_pct", "max_sl_pct"):
                if k == "max_sl_pct" and k not in ps:
                    continue
                if ps.get(k) != sl_pct:
                    changes[f"{u}.{k}"] = [ps.get(k), sl_pct]
                    ps[k] = sl_pct
        if not changes:
            continue
        v = new_version(db, name, params, "v1.1.3", note=f"stop loss fixed at {sl_pct:g}% from entry")
        db.add_strategy_update("param_change", name, cur["version"], v, changes,
                               f"v1.1.3: stop loss fixed at -{sl_pct:g}% from entry for every strategy "
                               "(locked: self-learning can't change it)", {})
        done.append(name)
    db.meta_set("migrated_v113", "1")
    return done


def load_strategies(db: Database) -> dict[str, Strategy]:
    ensure_defaults(db)
    try:
        migrate_v11(db)
        migrate_v112(db)
        migrate_v113(db)
    except Exception:  # noqa: BLE001  read-only views (reports/export) must still load strategies
        pass
    out = {}
    for name in STRATEGIES:
        row = db.active_strategy_params(name)
        if row is None:                       # all versions deactivated -> fall back to defaults, in memory
            d = copy.deepcopy(DEFAULT_PARAMS[name])
            out[name] = Strategy(name, 0, d["universes"], d["params"])
        else:
            out[name] = Strategy(name, row["version"], row["universes"], row["params"])
    return out


def new_version(db: Database, name: str, params: dict, source: str, note: str = "") -> int:
    """Phase 6 hook: store tuned params as the next version and activate it (old versions stay for rollback)."""
    versions = db.strategy_param_versions(name)
    cur = db.active_strategy_params(name)
    v = (max(r["version"] for r in versions) if versions else 0) + 1
    universes = cur["universes"] if cur else DEFAULT_PARAMS.get(name, {}).get("universes", [])
    db.save_strategy_params(name, v, params, universes, source, parent_version=cur["version"] if cur else None, note=note)
    return v


def rollback(db: Database, name: str, version: int) -> None:
    rows = {r["version"]: r for r in db.strategy_param_versions(name)}
    if version not in rows:
        raise ValueError(f"{name} has no version {version}")
    with db.tx():
        db.con.execute("UPDATE strategy_params SET active=0 WHERE strategy=?", (name,))
        db.con.execute("UPDATE strategy_params SET active=1 WHERE strategy=? AND version=?", (name, version))


__all__ = ["Strategy", "DEFAULT_PARAMS", "STRATEGIES", "ensure_defaults", "load_strategies", "migrate_v11", "migrate_v112", "migrate_v113",
           "new_version", "rollback"]
