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

PHASE = 9
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
    return {"schema": SCHEMA, "app": "SHADOW MEME", "version": __version__, "paper_only": True,
            "instance": instance_id(db), "generated_ts": round(now), "tz": tz_name(),
            "status": {"state": ("paused" if paused else "running") if app_status is not None else "offline",
                       "paused": paused, "source": "live" if app_status is not None else "database"},
            "account": account, "equity_curve": curve, "positions": positions, "history": history,
            "signals": {"today": counts(since), "last24h": counts(now - 86400), "recent": recent},
            "judge": judge, "strategies": strategies, "judge_params": judge_params, "updates": updates,
            "scanner": scanner}


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
