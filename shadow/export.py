"""CSV export (PHASE 7): trade history, daily reports, equity curve, signals, judge verdicts and
strategy updates. Times are written twice: unix ts and local time in the configured TIMEZONE (PKT).
Each `*_csv` returns the CSV text (phase 8 can send it as a Telegram document); `export_all` writes files.
"""
from __future__ import annotations

import csv
import io
import json
import time
from collections import defaultdict
from pathlib import Path

from .db import Database
from .timeutil import day_key, fmt, iso, tz_name

PHASE = 7
IMPLEMENTED = True

TRADE_COLS = ("id", "status", "mint", "symbol", "universe", "strategy", "params_version", "open_ts", "open_time",
              "market_price", "entry_price", "entry_slippage_pct", "size_usd", "qty", "buy_fee", "sl", "tp",
              "trail_pct", "trail_activate_pct", "time_stop_min", "score", "judge_confidence", "exit_ts", "exit_time",
              "exit_market_price", "exit_price", "exit_slippage_pct", "sell_fee", "exit_reason", "proceeds",
              "pnl_usd", "pnl_pct", "held_min", "signal_id")


def _csv(cols, rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else (round(r[c], 10) if isinstance(r[c], float) else r[c]) for c in cols])
    return buf.getvalue()


def trades_rows(db: Database, since_ts: float = 0, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM paper_positions WHERE open_ts >= ?" + (" AND status=?" if status else "") + " ORDER BY open_ts"
    out = []
    for r in db.con.execute(q, (since_ts, status) if status else (since_ts,)):
        d = dict(r)
        d["open_time"], d["exit_time"] = iso(d["open_ts"]), iso(d["exit_ts"])
        d["held_min"] = round((d["exit_ts"] - d["open_ts"]) / 60, 2) if d["exit_ts"] else None
        out.append(d)
    return out


def trades_csv(db: Database, since_ts: float = 0, status: str | None = None) -> str:
    return _csv(TRADE_COLS, trades_rows(db, since_ts, status))


def daily_rows(db: Database, since_ts: float = 0) -> list[dict]:
    """One row per local (PKT) day: trades opened/closed, wins, P&L gross / fees / after fees, end-of-day equity."""
    days: dict = defaultdict(lambda: {"opened": 0, "closed": 0, "wins": 0, "gross_usd": 0.0, "fees_usd": 0.0,
                                      "pnl_usd": 0.0, "best_usd": None, "worst_usd": None})
    for r in db.con.execute("SELECT open_ts FROM paper_positions WHERE open_ts >= ?", (since_ts,)):
        days[day_key(r[0])]["opened"] += 1
    for r in db.con.execute("SELECT exit_ts, proceeds, size_usd, buy_fee, sell_fee, pnl_usd FROM paper_positions "
                            "WHERE status='closed' AND exit_ts >= ?", (since_ts,)):
        d = days[day_key(r["exit_ts"])]
        d["closed"] += 1
        d["wins"] += 1 if r["pnl_usd"] > 0 else 0
        d["gross_usd"] += r["proceeds"] - r["size_usd"]
        d["fees_usd"] += (r["buy_fee"] or 0) + (r["sell_fee"] or 0)
        d["pnl_usd"] += r["pnl_usd"]
        d["best_usd"] = r["pnl_usd"] if d["best_usd"] is None else max(d["best_usd"], r["pnl_usd"])
        d["worst_usd"] = r["pnl_usd"] if d["worst_usd"] is None else min(d["worst_usd"], r["pnl_usd"])
    for r in db.con.execute("SELECT ts, equity, cash FROM paper_equity WHERE ts >= ? ORDER BY ts", (since_ts,)):
        days[day_key(r[0])]["equity_end"] = round(r[1], 4)
    for r in db.con.execute("SELECT ts, universe, alert_type FROM signals WHERE ts >= ?", (since_ts,)):
        k = f"sig_{r[2].lower()}"
        days[day_key(r[0])][k] = days[day_key(r[0])].get(k, 0) + 1
    out = []
    for k in sorted(days):
        d = days[k]
        d["day"] = k
        d["win_rate_pct"] = round(100 * d["wins"] / d["closed"], 1) if d["closed"] else None
        for f in ("gross_usd", "fees_usd", "pnl_usd"):
            d[f] = round(d[f], 4)
        out.append(d)
    return out


DAILY_COLS = ("day", "opened", "closed", "wins", "win_rate_pct", "gross_usd", "fees_usd", "pnl_usd", "best_usd",
              "worst_usd", "equity_end", "sig_buy_watch", "sig_sell", "sig_hold", "sig_reject")


def daily_csv(db: Database, since_ts: float = 0) -> str:
    return _csv(DAILY_COLS, daily_rows(db, since_ts))


def equity_csv(db: Database, since_ts: float = 0) -> str:
    rows = [dict(r) | {"time": iso(r["ts"])} for r in
            db.con.execute("SELECT * FROM paper_equity WHERE ts >= ? ORDER BY ts", (since_ts,))]
    return _csv(("ts", "time", "cash", "open_value", "equity", "n_open", "realized_pnl"), rows)


SIGNAL_COLS = ("id", "ts", "time", "mint", "symbol", "universe", "strategy", "alert_type", "class", "score",
               "params_version", "price", "mcap", "liquidity", "entry", "sl", "tp", "time_stop_min", "safety_verdict",
               "safety_score", "judge_verdict", "judge_confidence", "reason", "status", "exit_ts", "exit_price",
               "exit_reason", "pnl_pct", "max_up_pct", "max_down_pct")


def signals_csv(db: Database, since_ts: float = 0, include_rejects: bool = False) -> str:
    w = "" if include_rejects else " AND alert_type != 'REJECT'"
    rows = [dict(r) | {"time": iso(r["ts"])} for r in
            db.con.execute(f"SELECT * FROM signals WHERE ts >= ?{w} ORDER BY ts", (since_ts,))]
    return _csv(SIGNAL_COLS, rows)


def judge_csv(db: Database, since_ts: float = 0) -> str:
    rows = []
    for r in db.con.execute("SELECT * FROM judge_verdicts WHERE ts >= ? ORDER BY ts", (since_ts,)):
        d = dict(r)
        d["time"] = iso(d["ts"])
        rs = json.loads(d["reasons_json"] or "[]") if d.get("reasons_json") else []
        d["reasons"] = "; ".join(f"{x.get('code')}: {x.get('detail')}" if isinstance(x, dict) else str(x) for x in rs)
        rows.append(d)
    return _csv(("id", "signal_id", "ts", "time", "mint", "universe", "strategy", "verdict", "confidence", "reasons",
                 "judge_version", "outcome", "outcome_pnl_pct"), rows)


def strategy_updates_csv(db: Database, since_ts: float = 0) -> str:
    rows = []
    for r in db.con.execute("SELECT * FROM strategy_updates WHERE ts >= ? ORDER BY ts", (since_ts,)):
        d = dict(r)
        d["time"] = iso(d["ts"])
        rows.append(d)
    return _csv(("id", "ts", "time", "kind", "target", "from_version", "to_version", "changes_json", "reason",
                 "metrics_json", "sent_ts"), rows)


EXPORTS = {"trades": trades_csv, "daily_report": daily_csv, "equity": equity_csv, "signals": signals_csv,
           "judge": judge_csv, "strategy_updates": strategy_updates_csv}


def export_all(db: Database, out_dir: str | Path, since_ts: float = 0, now: float | None = None) -> dict[str, dict]:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = fmt(now or time.time(), "%Y%m%d-%H%M%S")
    out = {}
    for name, fn in EXPORTS.items():
        text = fn(db, since_ts)
        p = d / f"{name}-{stamp}.csv"
        tmp = p.with_suffix(".csv.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
        out[name] = {"path": str(p), "rows": max(text.count("\n") - 1, 0)}
    out["_timezone"] = {"path": "", "rows": 0, "tz": tz_name()}  # type: ignore[dict-item]
    return out


__all__ = ["trades_csv", "daily_csv", "equity_csv", "signals_csv", "judge_csv", "strategy_updates_csv",
           "export_all", "trades_rows", "daily_rows", "EXPORTS"]
