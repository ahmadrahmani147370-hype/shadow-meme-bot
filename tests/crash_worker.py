"""Helper process for the kill -9 test (not a test module): hammers the database with the same multi-row
writes the bot does (paper open/close with cash, strategy version + learning log + strategy_update,
judge version change, rollbacks, snapshots, signals) until it is killed. Prints a progress line per step."""
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shadow.config import load_settings  # noqa: E402
from shadow.db import Database  # noqa: E402
from shadow.judge import ensure_judge_params  # noqa: E402
from shadow.paper import PaperEngine  # noqa: E402
from shadow.safety import PASS, SafetyReport  # noqa: E402
from shadow.strategies import ensure_defaults, new_version, rollback  # noqa: E402


class Chk:
    def __init__(self):
        self.reps = {}

    def get_report(self, m):
        return self.reps.get(m)


def main(path: str, seed: int) -> None:
    rnd = random.Random(seed)
    s = load_settings(env={"PAPER_MAX_OPEN": "5", "PAPER_START_BALANCE": "100000",   # pre-v1.1 gates (tiny fixtures)
                           "TRADE_MIN_MCAP_USD": "0", "EVAL_MIN_MCAP_USD": "0", "TRADE_MIN_LIQ_USD": "0",
                           "TRADE_MIN_LIQ_MCAP": "0", "TRADE_MIN_AGE_MIN": "0", "COINS_PER_DAY": "0",
                           "MIN_TRADE_SCORE": "65", "SHADOW_TRACK": "false"})
    db = Database(path)
    db.acquire_writer_lock()
    ensure_defaults(db)
    ensure_judge_params(db, s)
    chk = Chk()
    pe = PaperEngine(s, db, checker=chk)
    step = 0
    while True:
        step += 1
        now = time.time()
        r = rnd.random()
        opens = pe.open_positions()
        if r < 0.45 and len(opens) < 5:
            m = f"M{seed}x{step}".ljust(44, "z")
            chk.reps[m] = SafetyReport(m, now, now + 900, 90, PASS, [], [], {"quote": {"sellable": True, "impact_pct": 1.0}},
                                       quote_ts=now)
            sid = db.add_signal({"ts": now, "mint": m, "symbol": "CR", "universe": "new", "strategy": "early_entry",
                                 "alert_type": "BUY_WATCH", "score": 75, "price": 1.0, "entry": 1.0, "sl": 0.9, "tp": 1.3,
                                 "time_stop_min": 20, "judge_verdict": "APPROVE", "status": "open"})
            pe.open_from_signal({"id": sid, "mint": m, "symbol": "CR", "universe": "new", "strategy": "early_entry",
                                 "params_version": 1, "alert_type": "BUY_WATCH", "judge_verdict": "APPROVE",
                                 "judge_confidence": 0.7, "score": 75, "price": 1.0, "entry": 1.0, "sl": 0.9, "tp": 1.3,
                                 "time_stop_min": 20}, chk.reps[m], {"trail_pct": 10, "trail_activate_pct": 15})
        elif r < 0.85 and opens:
            p = rnd.choice(opens)
            pe.close(p, rnd.uniform(0.7, 1.5), rnd.choice(["take_profit", "stop_loss", "time_stop"]))
        elif r < 0.93:
            name = rnd.choice(["early_entry", "first_pullback", "judge"])
            cur = db.active_strategy_params(name)
            params = dict(cur["params"])
            with db.tx():  # exactly what Learner does on an activation
                v = new_version(db, name, params, "learned", note=f"crash test step {step}")
                db.add_learning_log(name, "activate", "crash test", {"step": step}, {"x": [1, 2]}, cur["version"], v)
                db.add_strategy_update("judge_change" if name == "judge" else "param_change", name, cur["version"], v,
                                       {"x": [1, 2]}, f"crash test step {step}", {"n": step})
        else:
            name = rnd.choice(["early_entry", "first_pullback"])
            cur = db.active_strategy_params(name)
            if cur and cur.get("parent_version"):
                with db.tx():  # what Learner does on a rollback
                    rollback(db, name, cur["parent_version"])
                    db.add_learning_log(name, "rollback", "crash test", {}, {}, cur["version"], cur["parent_version"])
                    db.add_strategy_update("rollback", name, cur["version"], cur["parent_version"], {"x": [2, 1]},
                                           "crash test rollback", {"n": step})
        db.add_snapshots([(f"S{step % 50}".ljust(44, "s"), now, 1.0, 1e5, 1e4, 10.0, 1e5, 1, 1, "test")] * 5)
        print(f"step {step}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
