"""PHASE 6 - Report and stats functions for Telegram (phase 8) and `--debug paper`.

Each `*_stats()` returns plain data; each `*_text()` returns a short plain-text message:
    /paper       paper_text()        balance, equity, P&L after fees, win rate, today vs target
    /positions   positions_text()    open paper trades with live P&L
    /history     history_text(n)     last closed trades
    /report      report_text()       today's report: trades, P&L, strategies, judge, best/worst
    /strategies  strategies_text()   active versions + params, per-strategy results, update history
Strategy self-improvement notifications (every param change, rollback, judge change):
    pending_strategy_updates(db) -> [(id, text)]   unsent, oldest first (phase 8 sends them)
    mark_strategy_updates_sent(db, ids)
    strategy_update_text(row)                      one notification as text
"""
from __future__ import annotations

import time

from .config import Settings
from .db import Database
from .learning import cost_pct, judge_stats
from .paper import day_start
from .strategies import STRATEGIES, load_strategies

PHASE = 6
IMPLEMENTED = True


def _usd(x: float | None) -> str:
    return "n/a" if x is None else (f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}")


def _pct(x: float | None, sign: bool = True) -> str:
    return "n/a" if x is None else (f"{x:+.1f}%" if sign else f"{x:.0f}%")


def _t(ts: float | None) -> str:
    return time.strftime("%d %b %H:%M", time.localtime(ts)) if ts else "-"


def _closed(db: Database, since_ts: float = 0) -> list[dict]:
    return [dict(r) for r in db.con.execute(
        "SELECT * FROM paper_positions WHERE status='closed' AND exit_ts>=? ORDER BY exit_ts", (since_ts,))]


def _open(db: Database) -> list[dict]:
    return [dict(r) for r in db.con.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY open_ts")]


def trade_stats(rows: list[dict]) -> dict:
    pnl = [r["pnl_usd"] for r in rows if r["pnl_usd"] is not None]
    wins = [p for p in pnl if p > 0]
    return {"n": len(pnl), "wins": len(wins), "win_rate": len(wins) / len(pnl) if pnl else None,
            "pnl_usd": sum(pnl), "avg_usd": sum(pnl) / len(pnl) if pnl else None,
            "fees_usd": sum((r["buy_fee"] or 0) + (r["sell_fee"] or 0) for r in rows),
            "gross_usd": sum((r["proceeds"] or 0) - (r["size_usd"] or 0) for r in rows),
            "best": max(rows, key=lambda r: r["pnl_usd"] or 0) if rows else None,
            "worst": min(rows, key=lambda r: r["pnl_usd"] or 0) if rows else None}


# ---------------------------------------------------------------- /paper
def paper_stats(db: Database, s: Settings, now: float | None = None) -> dict:
    now = now or time.time()
    acc = db.con.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    acc = dict(acc) if acc else {"start_balance": s.paper_start_balance, "cash": s.paper_start_balance,
                                 "realized_pnl": 0.0, "fees_paid": 0.0}
    opens = _open(db)
    value = sum((p["qty"] or 0) * (p["last_price"] or p["market_price"] or 0) for p in opens)
    unreal = sum((p["qty"] or 0) * (p["last_price"] or p["market_price"] or 0) - p["size_usd"] for p in opens)
    st = trade_stats(_closed(db))
    today = db.con.execute("SELECT COUNT(*) FROM paper_positions WHERE open_ts>=?", (day_start(now),)).fetchone()[0]
    return {"start_balance": acc["start_balance"], "cash": acc["cash"], "open_value": value,
            "equity": acc["cash"] + value, "realized_pnl": acc["realized_pnl"], "unrealized_pnl": unreal,
            "fees_paid": acc["fees_paid"], "closed": st, "n_open": len(opens), "max_open": s.paper_max_open,
            "trades_today": today, "daily_target": s.paper_daily_target,
            "return_pct": (acc["cash"] + value - acc["start_balance"]) / acc["start_balance"] * 100}


def paper_text(db: Database, s: Settings, now: float | None = None) -> str:
    d = paper_stats(db, s, now)
    c = d["closed"]
    return "\n".join([
        "📄 PAPER ACCOUNT (virtual money, paper only)",
        f"Equity {_usd(d['equity'])} ({_pct(d['return_pct'])}) | cash {_usd(d['cash'])} | open value {_usd(d['open_value'])}",
        f"Realized P&L after fees {_usd(d['realized_pnl'])} | unrealized {_usd(d['unrealized_pnl'])} | fees paid {_usd(d['fees_paid'])}",
        f"Closed trades {c['n']} | wins {c['wins']} | win rate {_pct(c['win_rate'] * 100 if c['win_rate'] is not None else None, False)}",
        f"Open {d['n_open']}/{d['max_open']} | trades today {d['trades_today']}/{d['daily_target']} (target, never forced)",
    ])


# ---------------------------------------------------------------- /positions
def positions_text(db: Database, now: float | None = None) -> str:
    now = now or time.time()
    opens = _open(db)
    if not opens:
        return "📭 No open paper positions."
    lines = [f"📂 OPEN PAPER POSITIONS ({len(opens)})"]
    for p in opens:
        last = p["last_price"] or p["market_price"]
        val = (p["qty"] or 0) * last
        pnl = val - p["size_usd"] - p["buy_fee"]
        lines.append(f"• {p['symbol'] or p['mint'][:6]} [{p['universe']}/{p['strategy']}] {_usd(p['size_usd'])} @ "
                     f"{p['entry_price']:.6g} now {last:.6g} ({_pct((last / p['entry_price'] - 1) * 100)}) | "
                     f"P&L incl. buy fee {_usd(pnl)} | SL {p['sl']:.6g} TP {p['tp']:.6g} | "
                     f"{(now - p['open_ts']) / 60:.0f}/{p['time_stop_min']:.0f} min")
    return "\n".join(lines)


# ---------------------------------------------------------------- /history
def history_text(db: Database, n: int = 10) -> str:
    rows = [dict(r) for r in db.con.execute(
        "SELECT * FROM paper_positions WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?", (n,))]
    if not rows:
        return "📜 No closed paper trades yet."
    lines = [f"📜 LAST {len(rows)} CLOSED PAPER TRADES"]
    for r in rows:
        icon = "✅" if (r["pnl_usd"] or 0) > 0 else "❌"
        lines.append(f"{icon} {_t(r['exit_ts'])} {r['symbol'] or r['mint'][:6]} [{r['strategy']}] "
                     f"{r['exit_reason']} | price {_pct((r['exit_market_price'] / r['market_price'] - 1) * 100)} | "
                     f"P&L after fees {_usd(r['pnl_usd'])} ({_pct(r['pnl_pct'])}) | held "
                     f"{(r['exit_ts'] - r['open_ts']) / 60:.0f} min")
    return "\n".join(lines)


# ---------------------------------------------------------------- /report
def report_stats(db: Database, s: Settings, now: float | None = None) -> dict:
    now = now or time.time()
    since = day_start(now)
    closed = _closed(db, since)
    by_strat = {}
    for r in closed:
        by_strat.setdefault(r["strategy"], []).append(r)
    sig = {}
    for r in db.con.execute("SELECT universe, alert_type, COUNT(*) FROM signals WHERE ts>=? GROUP BY 1, 2", (since,)):
        sig.setdefault(r[0], {})[r[1]] = r[2]
    return {"since": since, "paper": paper_stats(db, s, now), "closed_today": trade_stats(closed),
            "by_strategy": {k: trade_stats(v) for k, v in by_strat.items()}, "signals": sig,
            "judge": judge_stats(db, since, cost_pct(s)),
            "updates": db.con.execute("SELECT COUNT(*) FROM strategy_updates WHERE ts>=?", (since,)).fetchone()[0]}


def report_text(db: Database, s: Settings, now: float | None = None) -> str:
    d = report_stats(db, s, now)
    c, j = d["closed_today"], d["judge"]
    lines = [f"📊 DAILY REPORT {time.strftime('%d %b %Y', time.localtime(d['since']))} (paper only)",
             f"Trades opened {d['paper']['trades_today']}/{d['paper']['daily_target']} | closed {c['n']} | wins {c['wins']} | "
             f"P&L after fees {_usd(c['pnl_usd'])} (gross {_usd(c['gross_usd'])}, fees {_usd(c['fees_usd'])})",
             f"Equity {_usd(d['paper']['equity'])} | open {d['paper']['n_open']}"]
    if c["best"]:
        lines.append(f"Best {c['best']['symbol']} {_usd(c['best']['pnl_usd'])} | worst {c['worst']['symbol']} "
                     f"{_usd(c['worst']['pnl_usd'])}")
    for k, v in d["by_strategy"].items():
        lines.append(f"  {k}: {v['n']} trades, {v['wins']} wins, {_usd(v['pnl_usd'])}")
    for u, v in sorted(d["signals"].items()):
        lines.append(f"Signals {u}: " + ", ".join(f"{k} {n}" for k, n in sorted(v.items())))
    a, v = j["approved"], j["vetoed"]
    lines.append(f"Judge: approved {a['n']} (closed {a['closed']}, avg {_pct(a['avg_pnl_pct'])}) | vetoed {v['n']} "
                 f"(closed {v['closed']}, avg {_pct(v['avg_pnl_pct'])}) | veto precision "
                 f"{_pct(j['veto_precision'] * 100 if j['veto_precision'] is not None else None, False)}")
    lines.append(f"Strategy updates today: {d['updates']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- /strategies
def strategies_stats(db: Database, s: Settings) -> dict:
    sts = load_strategies(db)
    out = {}
    for name in STRATEGIES:
        st = sts[name]
        rows = [dict(r) for r in db.con.execute("SELECT * FROM paper_positions WHERE status='closed' AND strategy=?",
                                                (name,))]
        w = db.con.execute("SELECT COUNT(*), SUM(pnl_pct > 0), AVG(pnl_pct) FROM signals WHERE strategy=? AND "
                           "alert_type='BUY_WATCH' AND status='closed'", (name,)).fetchone()
        out[name] = {"version": st.version, "universes": st.universes, "params": {u: st.p(u) for u in st.universes},
                     "paper": trade_stats(rows), "watches_closed": w[0] or 0, "watch_wins": w[1] or 0,
                     "watch_avg_pct": w[2], "versions": len(db.strategy_param_versions(name))}
    from .judge import ensure_judge_params
    jr = ensure_judge_params(db, s)
    out["judge"] = {"version": jr["version"] if jr else 0, "params": jr["params"] if jr else {}}
    return out


def strategies_text(db: Database, s: Settings, history: int = 5) -> str:
    d = strategies_stats(db, s)
    lines = ["🧠 STRATEGIES (self-improving, guarded)"]
    for name in STRATEGIES:
        x = d[name]
        lines.append(f"• {name} v{x['version']} ({x['versions']} versions) [{', '.join(x['universes'])}] | paper "
                     f"{x['paper']['n']} trades {_usd(x['paper']['pnl_usd'])} | watches closed {x['watches_closed']} "
                     f"({x['watch_wins']} up, avg {_pct(x['watch_avg_pct'])} before fees)")
        for u, p in x["params"].items():
            sl = p.get("sl_pct", p.get("max_sl_pct"))
            lines.append(f"    {u}: score>={p['min_score']} SL {sl}% TP {p['tp_pct']}% trail {p.get('trail_pct')}% "
                         f"after +{p.get('trail_activate_pct')}% time {p['time_stop_min']}m")
    jp = d["judge"]["params"]
    lines.append(f"• judge v{d['judge']['version']}: confidence>={jp.get('min_confidence')} impact<={jp.get('max_impact_pct')}% "
                 f"loss<={jp.get('max_sell_loss_pct')}% liq new>={jp.get('min_liq_new')} large>={jp.get('min_liq_large')}")
    ups = db.strategy_updates(limit=history)
    lines.append(f"Recent updates ({len(ups)}):" if ups else "No self-improvement changes yet (needs enough closed watches).")
    for u in ups:
        lines.append("  " + strategy_update_text(u, short=True))
    return "\n".join(lines)


# ---------------------------------------------------------------- strategy update notifications
KIND_TITLE = {"param_change": "🧠 STRATEGY UPDATE", "rollback": "↩️ STRATEGY ROLLBACK",
              "judge_change": "⚖️ JUDGE UPDATE", "judge_rollback": "↩️ JUDGE ROLLBACK"}


def _metric_line(m: dict) -> str:
    if not m:
        return ""
    if "train" in m:
        te, tr = m.get("test") or {}, m.get("train") or {}
        return (f"test {_pct(te.get('score'))} on {te.get('n')} (train {_pct(tr.get('score'))} on {tr.get('n')})")
    return f"v{m.get('version')} {_pct(m.get('score'))} on {m.get('n')}"


def strategy_update_text(u: dict, short: bool = False) -> str:
    ch = ", ".join(f"{k} {a} → {b}" for k, (a, b) in u["changes"].items()) or "-"
    m = u["metrics"]
    head = f"{KIND_TITLE.get(u['kind'], u['kind'])} {u['target']} v{u['from_version']} → v{u['to_version']}"
    if short:
        return f"{_t(u['ts'])} {head}: {ch}"
    return "\n".join([head, f"Changed: {ch}", f"Why: {u['reason']}",
                      f"Before: {_metric_line(m.get('before'))}", f"After: {_metric_line(m.get('after'))}",
                      "(net % per opportunity after fees + slippage; paper only)"])


def pending_strategy_updates(db: Database) -> list[tuple[int, str]]:
    return [(u["id"], strategy_update_text(u)) for u in db.strategy_updates(unsent_only=True, limit=100)]


def mark_strategy_updates_sent(db: Database, ids: list[int]) -> None:
    db.mark_strategy_updates_sent(ids)


def strategy_update_history(db: Database, limit: int = 20, target: str | None = None) -> list[dict]:
    return db.strategy_updates(target=target, limit=limit)


__all__ = ["paper_stats", "paper_text", "positions_text", "history_text", "report_stats", "report_text",
           "strategies_stats", "strategies_text", "strategy_update_text", "pending_strategy_updates",
           "mark_strategy_updates_sent", "strategy_update_history", "trade_stats"]
