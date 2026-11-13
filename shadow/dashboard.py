"""PHASE 9 - Dashboard data: sanitized JSON snapshot + compact URL summary + GitHub publisher.

Data path (no VPS, phone has no public URL):
  1. Every DASHBOARD_PUSH_MIN the bot builds a compact, sanitized snapshot (no token, no keys, no error texts,
     no URLs) and - only if GITHUB_TOKEN is set - force-pushes it as `snapshot.json` to the orphan branch
     `dashboard-data` through the GitHub Git Data API (1 commit, history replaced each time: the repo does
     not grow). The Mini App on GitHub Pages (docs/) reads it from raw.githubusercontent.com.
  2. Always (token or not) a ~0.5 KB summary rides inside the Mini App URL (`?s=` base64url JSON) of the
     /dashboard button and the chat menu button, so the headline numbers are fresh even without GitHub.
  3. Nothing configured: the dashboard shows the URL summary and clear empty states (never invented data).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx

from .config import Settings
from .db import Database

PHASE = 9   # + v1.1 insights block (rug filter, kill log, per-universe P&L)
IMPLEMENTED = True
SCHEMA = 1
log = logging.getLogger("shadow.dashboard")

MAX_SYMBOL = 24
MAX_REASON = 140
MAX_SUMMARY_CHARS = 900


def _s(x, n=MAX_REASON):
    if x is None:
        return None
    x = str(x).replace("\n", " ").strip()
    return x[:n]


def _r(x, nd=6):
    if x is None:
        return None
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def instance_id(db: Database) -> str:
    """Random id of this bot install, so the dashboard never mixes a snapshot from another install."""
    v = db.meta_get("instance_id")
    if not v:
        v = secrets.token_hex(6)
        db.meta_set("instance_id", v)
    return v


def downsample(points: list, n: int) -> list:
    if len(points) <= n:
        return list(points)
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


# ====================================================================== snapshot
def build_snapshot(db: Database, s: Settings, app_status: dict | None = None, now: float | None = None,
                   paused: bool | None = None) -> dict:
    """Whitelisted fields only. `app_status` = live scanner/engine state (None = built from the DB alone)."""
    from . import __version__
    from .learning import cost_pct, judge_stats
    from .reports import paper_stats, strategies_stats, strategy_update_history
    from .timeutil import day_start, tz_name

    now = now or time.time()
    since = day_start(now)
    con = db.con
    p = paper_stats(db, s, now)
    c = p["closed"]
    account = {k: _r(p[k], 4) for k in ("start_balance", "cash", "open_value", "equity", "realized_pnl",
                                         "unrealized_pnl", "fees_paid", "return_pct")}
    account.update({"n_open": p["n_open"], "max_open": p["max_open"], "trades_today": p["trades_today"],
                    "daily_target": p["daily_target"], "trade_size": s.paper_trade_size,
                    "closed": {"n": c["n"], "wins": c["wins"], "win_rate": _r(c["win_rate"], 4),
                               "pnl_usd": _r(c["pnl_usd"], 4), "avg_usd": _r(c["avg_usd"], 4)}})
    eq = [(r[0], r[1]) for r in con.execute("SELECT ts, equity FROM paper_equity ORDER BY ts")]
    curve = [[round(t), _r(e, 4)] for t, e in downsample(eq, 240)]
    if curve and (curve[-1][1] != account["equity"] or now - curve[-1][0] > 60):
        curve.append([round(now), account["equity"]])        # current equity closes the curve (real value, not invented)

    positions = []
    for r in con.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY open_ts"):
        r = dict(r)
        px = r["last_price"] or r["market_price"] or 0
        val = (r["qty"] or 0) * px
        positions.append({"id": r["id"], "symbol": _s(r["symbol"], MAX_SYMBOL), "mint": r["mint"],
                          "universe": r["universe"], "strategy": r["strategy"], "open_ts": round(r["open_ts"]),
                          "entry_price": _r(r["entry_price"], 12), "last_price": _r(px, 12),
                          "size_usd": _r(r["size_usd"], 4), "value_usd": _r(val, 4),
                          "pnl_usd": _r(val - (r["size_usd"] or 0), 4),
                          "pnl_pct": _r((val / r["size_usd"] - 1) * 100 if r["size_usd"] else None, 2),
                          "sl": _r(r["sl"], 12), "tp": _r(r["tp"], 12)})
    history = [{"id": r["id"], "symbol": _s(r["symbol"], MAX_SYMBOL), "mint": r["mint"], "universe": r["universe"],
                "strategy": r["strategy"], "open_ts": round(r["open_ts"]), "exit_ts": round(r["exit_ts"] or 0),
                "entry_price": _r(r["entry_price"], 12), "exit_price": _r(r["exit_price"], 12),
                "size_usd": _r(r["size_usd"], 4), "pnl_usd": _r(r["pnl_usd"], 4), "pnl_pct": _r(r["pnl_pct"], 2),
                "exit_reason": _s(r["exit_reason"], 40)}
               for r in con.execute("SELECT * FROM paper_positions WHERE status='closed' ORDER BY exit_ts DESC LIMIT 50")]

    def counts(since_ts):
        out = {}
        for u, a, n in con.execute("SELECT universe, alert_type, COUNT(*) FROM signals WHERE ts>=? GROUP BY 1, 2",
                                   (since_ts,)):
            out.setdefault(u, {})[a] = n
        return out

    recent = [{"ts": round(r["ts"]), "symbol": _s(r["symbol"], MAX_SYMBOL), "mint": r["mint"], "universe": r["universe"],
               "strategy": r["strategy"], "type": r["alert_type"], "score": _r(r["score"], 1),
               "judge": r["judge_verdict"], "judge_conf": _r(r["judge_confidence"], 2), "reason": _s(r["reason"]),
               "outcome": _s(r["exit_reason"], 40), "pnl_pct": _r(r["pnl_pct"], 2)}
              for r in con.execute("SELECT * FROM signals WHERE alert_type IN ('BUY_WATCH','SELL') "
                                   "ORDER BY ts DESC LIMIT 40")]
    j = judge_stats(db, 0, cost_pct(s))
    jt = judge_stats(db, since, cost_pct(s))
    jbr = sorted(j["by_reason"].items(), key=lambda kv: -kv[1]["n"])[:10]
    judge = {"all": {"approved": j["approved"], "vetoed": j["vetoed"], "veto_precision": _r(j["veto_precision"], 4),
                     "edge_pct": _r(j["edge_pct"], 3)},
             "today": {"approved": jt["approved"], "vetoed": jt["vetoed"]},
             "by_reason": [{"code": k, **{kk: (_r(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
                           for k, v in jbr]}
    for blk in (judge["all"], judge["today"]):
        for k in ("approved", "vetoed"):
            blk[k] = {kk: (_r(vv, 4) if isinstance(vv, float) else vv) for kk, vv in blk[k].items()}

    st = strategies_stats(db, s)
    strategies = {}
    for name, x in st.items():
        if name == "judge":
            continue
        strategies[name] = {"version": x["version"], "versions": x["versions"], "universes": x["universes"],
                            "params": {u: {k: _r(v, 4) for k, v in pr.items() if isinstance(v, (int, float))}
                                       for u, pr in x["params"].items()},
                            "paper": {"n": x["paper"]["n"], "wins": x["paper"]["wins"],
                                      "pnl_usd": _r(x["paper"]["pnl_usd"], 4)},
                            "watches_closed": x["watches_closed"], "watch_wins": x["watch_wins"],
                            "watch_avg_pct": _r(x["watch_avg_pct"], 3)}
    judge_params = {"version": st["judge"]["version"],
                    "params": {k: _r(v, 4) for k, v in st["judge"]["params"].items() if isinstance(v, (int, float))}}
    updates = [{"id": u["id"], "ts": round(u["ts"]), "kind": u["kind"], "target": u["target"],
                "from_version": u["from_version"], "to_version": u["to_version"],
                "changes": {k: [_r(a, 6) if isinstance(a, (int, float)) else _s(a, 40),
                                _r(b, 6) if isinstance(b, (int, float)) else _s(b, 40)]
                            for k, (a, b) in (u.get("changes") or {}).items()},
                "reason": _s(u["reason"], 300), "metrics": _metrics(u.get("metrics") or {})}
               for u in strategy_update_history(db, limit=30)]

    tok1h = con.execute("SELECT COUNT(*) FROM seen_tokens WHERE first_seen_ts>=?", (now - 3600,)).fetchone()[0]
    tok24 = con.execute("SELECT COUNT(*) FROM seen_tokens WHERE first_seen_ts>=?", (now - 86400,)).fetchone()[0]
    last_tok = con.execute("SELECT MAX(first_seen_ts) FROM seen_tokens").fetchone()[0]
    a = app_status or {}
    scanner = {"live": app_status is not None, "mode": _s(a.get("scanner_mode"), 40), "uptime_s": a.get("uptime_s"),
               "new_tokens": a.get("new_tokens"), "tracked_new": a.get("tracked_new"),
               "tracked_mcap": a.get("tracked_mcap"), "tracked_volume": a.get("tracked_volume"),
               "safety_checked": a.get("safety_checked"), "errors_recent": a.get("errors_recent"),
               "telegram": _s(a.get("telegram"), 40), "tokens_1h": tok1h, "tokens_24h": tok24,
               "last_token_ts": round(last_tok) if last_tok else None,
               "seen_in_db": con.execute("SELECT COUNT(*) FROM seen_tokens").fetchone()[0]}
    if paused is None:
        paused = db.meta_get("paused", "0") == "1"
    insights = build_insights(db, s, account, positions, now, since)
    return {"schema": SCHEMA, "app": "SHADOW MEME", "version": __version__, "paper_only": True,
            "instance": instance_id(db), "generated_ts": round(now), "tz": tz_name(),
            "status": {"state": ("paused" if paused else "running") if app_status is not None else "offline",
                       "paused": paused, "source": "live" if app_status is not None else "database"},
            "account": account, "equity_curve": curve, "positions": positions, "history": history,
            "signals": {"today": counts(since), "last24h": counts(now - 86400), "recent": recent},
            "judge": judge, "strategies": strategies, "judge_params": judge_params, "updates": updates,
            "scanner": scanner, "insights": insights}


# ====================================================================== insights (v1.1 dashboard design)
MAX_EVENTS = 40
MAX_REASONS = 8


def update_counters(db: Database, now: float, since: float) -> dict:
    with db.tx():
        return _update_counters(db, now, since)


def _update_counters(db: Database, now: float, since: float) -> dict:
    """Cumulative scan / safety counters. safety_reports (3 h) and seen_tokens (24 h) are pruned, so the dashboard
    keeps running totals in DB meta, advanced by a cursor on every snapshot build (builds run every few minutes,
    far inside the retention windows). Real counts only; `since` says from when they were counted."""
    con = db.con
    try:
        c = json.loads(db.meta_get("dash_counters") or "{}")
    except (TypeError, ValueError):
        c = {}
    if not isinstance(c, dict) or "since" not in c:
        c = {"since": round(now), "cur_seen": 0.0, "cur_safety": 0.0, "scanned": 0, "checked": 0, "rugs": 0,
             "reasons": {}, "day": since, "d_scanned": 0, "d_checked": 0, "d_rugs": 0}
    if c.get("day") != since:
        c.update(day=since, d_scanned=0, d_checked=0, d_rugs=0)
    cur_seen, cur_safety = float(c.get("cur_seen") or 0), float(c.get("cur_safety") or 0)
    for (ts,) in con.execute("SELECT first_seen_ts FROM seen_tokens WHERE first_seen_ts>? AND first_seen_ts<=?",
                             (cur_seen, now)):
        c["scanned"] += 1
        c["d_scanned"] += ts >= since
        cur_seen = max(cur_seen, ts)
    con.execute("CREATE TABLE IF NOT EXISTS dash_counted (mint TEXT PRIMARY KEY, ts REAL, rejected INTEGER)")
    for mint, ts, verdict, crit in con.execute(
            "SELECT mint, checked_ts, verdict, critical_json FROM safety_reports WHERE checked_ts>? AND checked_ts<=? "
            "ORDER BY checked_ts", (cur_safety, now)).fetchall():
        cur_safety = max(cur_safety, ts)
        today, rej = ts >= since, verdict == "REJECT"
        prev = con.execute("SELECT rejected FROM dash_counted WHERE mint=?", (mint,)).fetchone()
        if prev is None:                                     # each token counted once, even when re-checked
            c["checked"] += 1
            c["d_checked"] += today
            con.execute("INSERT INTO dash_counted (mint, ts, rejected) VALUES (?,?,?)", (mint, ts, int(rej)))
        elif prev[0] or not rej:
            continue
        else:
            con.execute("UPDATE dash_counted SET rejected=1, ts=? WHERE mint=?", (ts, mint))
        if rej:
            c["rugs"] += 1
            c["d_rugs"] += today
            try:
                codes = json.loads(crit or "[]") or ["low_safety_score"]
            except (TypeError, ValueError):
                codes = ["low_safety_score"]
            for code in codes[:6]:
                code = _s(code, 40)
                if code:
                    c["reasons"][code] = c["reasons"].get(code, 0) + 1
    con.execute("DELETE FROM dash_counted WHERE ts<?", (now - 2 * 86400,))
    if len(c["reasons"]) > 60:                               # keep meta small
        c["reasons"] = dict(sorted(c["reasons"].items(), key=lambda kv: -kv[1])[:40])
    c["cur_seen"], c["cur_safety"] = cur_seen, cur_safety
    db.meta_set("dash_counters", json.dumps(c, separators=(",", ":")))
    return c


def build_insights(db: Database, s: Settings, account: dict, positions: list, now: float, since: float) -> dict:
    con = db.con
    c = update_counters(db, now, since)
    opened = con.execute("SELECT COUNT(*), COALESCE(SUM(open_ts>=?),0) FROM paper_positions", (since,)).fetchone()
    totals = {"since": c["since"], "scanned": c["scanned"], "checked": c["checked"], "rugs": c["rugs"],
              "scanned_today": c["d_scanned"], "checked_today": c["d_checked"], "rugs_today": c["d_rugs"],
              "trades": opened[0], "trades_today": opened[1], "open": account["n_open"],
              "closed": account["closed"]["n"], "wins": account["closed"]["wins"],
              "win_rate": account["closed"]["win_rate"]}
    reasons = [{"code": k, "n": v} for k, v in sorted(c["reasons"].items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_REASONS]]

    unis = {}
    for u, n, w, pnl in con.execute("SELECT universe, COUNT(*), SUM(pnl_usd>0), COALESCE(SUM(pnl_usd),0) "
                                    "FROM paper_positions WHERE status='closed' GROUP BY 1"):
        unis[u or "?"] = {"closed": n, "wins": w or 0, "realized": _r(pnl, 4), "open": 0, "unrealized": 0.0}
    for p in positions:
        x = unis.setdefault(p["universe"] or "?", {"closed": 0, "wins": 0, "realized": 0.0, "open": 0, "unrealized": 0.0})
        x["open"] += 1
        x["unrealized"] = _r(x["unrealized"] + (p["pnl_usd"] or 0), 4)
    for x in unis.values():
        x["pnl_usd"] = _r((x["realized"] or 0) + (x["unrealized"] or 0), 4)

    base_row = con.execute("SELECT equity FROM paper_equity WHERE ts<? ORDER BY ts DESC LIMIT 1", (since,)).fetchone()
    base = base_row[0] if base_row else account["start_balance"]
    eq = account["equity"]
    today = {"base": _r(base, 4), "pnl_usd": _r(eq - base, 4) if eq is not None and base else None,
             "pnl_pct": _r((eq / base - 1) * 100, 3) if eq is not None and base else None}

    ev = []
    for r in con.execute("SELECT id, open_ts, symbol, mint, universe, strategy, size_usd FROM paper_positions "
                         "ORDER BY open_ts DESC LIMIT 15"):
        ev.append({"ts": round(r[1]), "kind": "BUY", "symbol": _s(r[2], MAX_SYMBOL), "mint": r[3], "universe": r[4],
                   "reason": _s(f"paper buy ${r[6] or 0:.2f} · {r[5] or ''}", 80), "pnl_usd": None, "pnl_pct": None})
    for r in con.execute("SELECT exit_ts, symbol, mint, universe, exit_reason, pnl_usd, pnl_pct FROM paper_positions "
                         "WHERE status='closed' ORDER BY exit_ts DESC LIMIT 15"):
        ev.append({"ts": round(r[0] or 0), "kind": "SELL", "symbol": _s(r[1], MAX_SYMBOL), "mint": r[2], "universe": r[3],
                   "reason": _s(r[4], 40), "pnl_usd": _r(r[5], 4), "pnl_pct": _r(r[6], 2)})
    for r in con.execute("SELECT s.checked_ts, t.symbol, s.mint, s.universe, s.critical_json FROM safety_reports s "
                         "LEFT JOIN seen_tokens t ON t.mint=s.mint WHERE s.verdict='REJECT' "
                         "ORDER BY s.checked_ts DESC LIMIT 20"):
        try:
            codes = json.loads(r[4] or "[]") or ["low_safety_score"]
        except (TypeError, ValueError):
            codes = ["low_safety_score"]
        ev.append({"ts": round(r[0]), "kind": "KILL", "symbol": _s(r[1], MAX_SYMBOL), "mint": r[2], "universe": r[3],
                   "reason": _s("rug filter: " + ", ".join(str(x) for x in codes[:4]), 100), "pnl_usd": None, "pnl_pct": None})
    for r in con.execute("SELECT ts, symbol, mint, universe, reason FROM signals WHERE alert_type='REJECT' "
                         "ORDER BY ts DESC LIMIT 15"):
        ev.append({"ts": round(r[0]), "kind": "KILL", "symbol": _s(r[1], MAX_SYMBOL), "mint": r[2], "universe": r[3],
                   "reason": _s("signal: " + (r[4] or "rejected"), 100), "pnl_usd": None, "pnl_pct": None})
    ev.sort(key=lambda e: -e["ts"])
    return {"totals": totals, "reject_reasons": reasons, "universes": unis, "today": today, "events": ev[:MAX_EVENTS],
            "hourly": hourly_series(db, now), "perf": perf_stats(db)}


HOURS = 12


def hourly_series(db: Database, now: float, hours: int = HOURS) -> dict:
    """v1.2 dashboard mini bar charts - real counts per clock hour, oldest first, the last bucket = this hour so
    far: unique tokens checked / rejected by the rug filter (dash_counted, 2 days kept) and paper trades opened."""
    con = db.con
    start = (int(now) // 3600 - (hours - 1)) * 3600
    rugs, checked, trades = [0] * hours, [0] * hours, [0] * hours
    try:
        for ts, rej in con.execute("SELECT ts, rejected FROM dash_counted WHERE ts>=?", (start,)):
            i = int((ts - start) // 3600)
            if 0 <= i < hours:
                checked[i] += 1
                rugs[i] += 1 if rej else 0
    except Exception:  # noqa: BLE001  table created on the first counter update
        pass
    for (ts,) in con.execute("SELECT open_ts FROM paper_positions WHERE open_ts>=?", (start,)):
        i = int((ts - start) // 3600)
        if 0 <= i < hours:
            trades[i] += 1
    return {"start": start, "step": 3600, "rugs": rugs, "checked": checked, "trades": trades}


def perf_stats(db: Database) -> dict:
    """Closed paper trades of the current era: n, wins, losses, average P&L ($ and %)."""
    n, w, avg_usd, avg_pct, tot = db.con.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl_usd>0),0), AVG(pnl_usd), AVG(pnl_pct), COALESCE(SUM(pnl_usd),0) "
        "FROM paper_positions WHERE status='closed'").fetchone()
    return {"closed": n, "wins": w, "losses": n - w, "avg_pnl_usd": _r(avg_usd, 4), "avg_pnl_pct": _r(avg_pct, 2),
            "pnl_usd": _r(tot, 4), "win_rate": _r(w / n, 4) if n else None}


def _metrics(m: dict) -> dict:
    out = {}
    for side in ("before", "after"):
        x = m.get(side)
        if isinstance(x, dict):
            out[side] = {k: (_r(v, 4) if isinstance(v, (int, float)) else
                             ({kk: _r(vv, 4) for kk, vv in v.items() if isinstance(vv, (int, float))}
                              if isinstance(v, dict) else None))
                         for k, v in x.items() if k in ("version", "score", "n", "train", "test", "win_rate")}
    return out


def snapshot_json(snap: dict) -> bytes:
    return json.dumps(snap, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


# ====================================================================== URL summary (fallback, always sent)
def summary(snap: dict) -> dict:
    a, sig = snap["account"], snap["signals"]["today"]
    j = snap["judge"]["today"]
    out = {"v": 1, "i": snap["instance"], "t": snap["generated_ts"], "st": snap["status"]["state"][0],
           "eq": _r(a["equity"], 2), "c": _r(a["cash"], 2), "sb": _r(a["start_balance"], 2),
           "rp": _r(a["realized_pnl"], 2), "up": _r(a["unrealized_pnl"], 2), "o": a["n_open"],
           "td": a["trades_today"], "cl": a["closed"]["n"], "w": a["closed"]["wins"],
           "sg": {u: [v.get("BUY_WATCH", 0), v.get("SELL", 0), v.get("REJECT", 0)] for u, v in sig.items()},
           "ja": j["approved"]["n"], "jv": j["vetoed"]["n"], "ver": snap["version"],
           "k": [snap["insights"]["totals"][x] for x in ("scanned", "rugs", "trades")],
           "tp": snap["insights"]["today"]["pnl_usd"],
           "ec": [round(e * 100) for _, e in downsample(snap["equity_curve"], 24) if e is not None]}
    if len(encode_summary(out)) > MAX_SUMMARY_CHARS:
        out.pop("ec")
    return out


def encode_summary(d: dict) -> str:
    raw = json.dumps(d, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_summary(s: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def dashboard_url(s: Settings, snap: dict | None = None) -> str:
    base = s.dashboard_url.rstrip("/") + "/"
    if snap is None:
        return base
    return f"{base}?s={encode_summary(summary(snap))}"


def write_local(snap: dict, path: str | Path) -> Path:
    """Atomic write of the latest snapshot (data/dashboard/snapshot.json) - handy for /export-style debugging."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(snapshot_json(snap))
    os.replace(tmp, path)
    return path


# ====================================================================== GitHub publisher (optional)
class GitHubError(RuntimeError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class GitHubPublisher:
    """Force-pushes one orphan commit with snapshot.json to `branch` (3 API calls; old snapshots become
    unreferenced, so the repo does not grow). Needs a token with Contents: read & write on this repo only."""

    README = ("Data branch for the SHADOW MEME dashboard (paper trading only). Overwritten by the bot every few "
              "minutes; history is not kept. No secrets are ever written here.\n")

    def __init__(self, token: str, repo: str, branch: str = "dashboard-data", client: httpx.AsyncClient | None = None,
                 api: str = "https://api.github.com"):
        if not token:
            raise ValueError("GITHUB_TOKEN is empty")
        self._token, self.repo, self.branch, self.api = token, repo, branch, api.rstrip("/")
        self._client, self._own = client, client is None
        self.pushes = 0
        self.failures = 0
        self.last_error = ""
        self.last_push_ts = 0.0
        self.disabled = False

    def redact(self, t) -> str:
        return str(t).replace(self._token, "<token>")

    async def _req(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return await self._client.request(method, f"{self.api}/repos/{self.repo}{path}", json=body, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "shadow-meme-bot"})

    def _check(self, r: httpx.Response, what: str) -> dict:
        if r.status_code in (200, 201):
            return r.json()
        try:
            msg = r.json().get("message", "")
        except ValueError:
            msg = r.text[:120]
        raise GitHubError(self.redact(f"{what}: HTTP {r.status_code} {msg}"), r.status_code)

    async def publish(self, content: bytes, message: str = "dashboard snapshot") -> str:
        if self.disabled:
            raise GitHubError("publisher disabled after an authorization error (check GITHUB_TOKEN)", 401)
        try:
            tree = self._check(await self._req("POST", "/git/trees", {"tree": [
                {"path": "snapshot.json", "mode": "100644", "type": "blob", "content": content.decode("utf-8")},
                {"path": "README.md", "mode": "100644", "type": "blob", "content": self.README}]}), "create tree")
            commit = self._check(await self._req("POST", "/git/commits", {"message": message, "tree": tree["sha"],
                                                                          "parents": []}), "create commit")
            r = await self._req("PATCH", f"/git/refs/heads/{self.branch}", {"sha": commit["sha"], "force": True})
            if r.status_code in (404, 422):
                self._check(await self._req("POST", "/git/refs", {"ref": f"refs/heads/{self.branch}",
                                                                   "sha": commit["sha"]}), "create branch")
            else:
                self._check(r, "update branch")
        except GitHubError as e:
            self.failures += 1
            self.last_error = str(e)
            if e.code in (401, 403):
                self.disabled = e.code == 401
            raise
        except httpx.HTTPError as e:
            self.failures += 1
            self.last_error = self.redact(f"{type(e).__name__}")
            raise GitHubError(self.last_error) from None
        self.pushes += 1
        self.last_push_ts = time.time()
        return commit["sha"]

    async def close(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None


class GhCliPublisher(GitHubPublisher):
    """Same 3 Git Data API calls, made through the GitHub CLI (`gh api`) with gh's own stored login, so no token has
    to be copied into .env (DASHBOARD_PUBLISHER=gh; for a PC/box where `gh auth login` was done)."""

    def __init__(self, repo: str, branch: str = "dashboard-data", gh: str = "gh"):
        self._token, self.repo, self.branch, self.gh = "", repo, branch, gh
        self.pushes = self.failures = 0
        self.last_error, self.last_push_ts, self.disabled = "", 0.0, False

    def redact(self, t) -> str:
        return str(t)

    async def _gh(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        proc = await asyncio.create_subprocess_exec(
            self.gh, "api", "-X", method, f"repos/{self.repo}{path}", "--input", "-", "-i",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(body).encode()), timeout=60)
        text = out.decode("utf-8", "replace")
        head, _, payload = text.replace("\r\n", "\n").partition("\n\n")
        try:
            code = int(head.split()[1])
        except (IndexError, ValueError):
            raise GitHubError(f"gh api {path}: {err.decode()[:160].strip() or 'no response'}") from None
        try:
            data = json.loads(payload) if payload.strip() else {}
        except ValueError:
            data = {}
        return code, data

    async def publish(self, content: bytes, message: str = "dashboard snapshot") -> str:
        try:
            code, tree = await self._gh("POST", "/git/trees", {"tree": [
                {"path": "snapshot.json", "mode": "100644", "type": "blob", "content": content.decode("utf-8")},
                {"path": "README.md", "mode": "100644", "type": "blob", "content": self.README}]})
            if code != 201:
                raise GitHubError(f"create tree: HTTP {code} {tree.get('message', '')}", code)
            code, commit = await self._gh("POST", "/git/commits", {"message": message, "tree": tree["sha"], "parents": []})
            if code != 201:
                raise GitHubError(f"create commit: HTTP {code} {commit.get('message', '')}", code)
            code, r = await self._gh("PATCH", f"/git/refs/heads/{self.branch}", {"sha": commit["sha"], "force": True})
            if code in (404, 422):
                code, r = await self._gh("POST", "/git/refs", {"ref": f"refs/heads/{self.branch}", "sha": commit["sha"]})
                if code != 201:
                    raise GitHubError(f"create branch: HTTP {code} {r.get('message', '')}", code)
            elif code != 200:
                raise GitHubError(f"update branch: HTTP {code} {r.get('message', '')}", code)
        except (GitHubError, OSError, asyncio.TimeoutError) as e:
            self.failures += 1
            self.last_error = str(e) if isinstance(e, GitHubError) else f"gh: {type(e).__name__}"
            raise GitHubError(self.last_error, getattr(e, "code", None)) from None
        self.pushes += 1
        self.last_push_ts = time.time()
        return commit["sha"]

    async def close(self) -> None:
        return None


def make_publisher(s: Settings):
    """DASHBOARD_PUBLISHER: auto (GITHUB_TOKEN if set, else off) | token | gh | off."""
    mode = (s.dashboard_publisher or "auto").lower()
    if mode == "off":
        return None
    if mode == "gh":
        return GhCliPublisher(s.github_repo, s.dashboard_branch)
    if s.github_token:
        return GitHubPublisher(s.github_token, s.github_repo, s.dashboard_branch)
    return None


def raw_url(s: Settings) -> str:
    return f"https://raw.githubusercontent.com/{s.github_repo}/{s.dashboard_branch}/snapshot.json"


__all__ = ["build_snapshot", "snapshot_json", "summary", "encode_summary", "decode_summary", "dashboard_url",
           "write_local", "GitHubPublisher", "GhCliPublisher", "make_publisher", "GitHubError", "instance_id", "downsample", "raw_url"]
