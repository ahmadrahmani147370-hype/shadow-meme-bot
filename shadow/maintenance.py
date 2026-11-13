"""Database maintenance (PHASE 7): integrity + consistency checks, retention/pruning with a hard size cap,
PRAGMA optimize, VACUUM, WAL checkpoints, daily gzip backups with rotation, table sizes and a growth
projection. Everything runs inside the single bot process (single writer) via `Maintenance.run_once`.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
import sqlite3
import time
from collections import Counter
from pathlib import Path

from .config import Settings
from .db import Database
from .timeutil import day_key, fmt

PHASE = 7
IMPLEMENTED = True
log = logging.getLogger("shadow.maintenance")
EPS = 0.005  # $ tolerance for float sums


# ---------------------------------------------------------------- checks
def integrity_check(db: Database, quick: bool = False) -> list[str]:
    rows = db.con.execute("PRAGMA quick_check" if quick else "PRAGMA integrity_check").fetchall()
    return [r[0] for r in rows]


def consistency(db: Database) -> dict:
    """Cross-table invariants that must hold after any crash (they hold because every multi-row change is
    one transaction): paper cash/realized/fees == what the trade rows imply; one active version per strategy;
    every strategy_update points at stored versions, and the newest one per target is the active version."""
    problems: list[str] = []
    con = db.con
    acc = con.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    n_pos = con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
    facts: dict = {"positions": n_pos}
    if acc is None:
        if n_pos:
            problems.append(f"{n_pos} paper positions but no paper_account row")
    else:
        r = con.execute(
            "SELECT COALESCE(SUM(size_usd + buy_fee), 0) AS spent, "
            "COALESCE(SUM(CASE WHEN status='closed' THEN proceeds - sell_fee END), 0) AS back, "
            "COALESCE(SUM(buy_fee), 0) AS bfees, COALESCE(SUM(CASE WHEN status='closed' THEN sell_fee END), 0) AS sfees, "
            "COALESCE(SUM(CASE WHEN status='closed' THEN proceeds - size_usd - sell_fee END), 0) AS closed_gain, "
            "SUM(status='open') AS n_open, SUM(status='closed') AS n_closed FROM paper_positions").fetchone()
        exp_cash = acc["start_balance"] - r["spent"] + r["back"]
        exp_real = -r["bfees"] + r["closed_gain"]
        exp_fees = r["bfees"] + r["sfees"]
        facts.update(cash=round(acc["cash"], 4), expected_cash=round(exp_cash, 4), realized=round(acc["realized_pnl"], 4),
                     expected_realized=round(exp_real, 4), fees=round(acc["fees_paid"], 4), n_open=r["n_open"] or 0,
                     n_closed=r["n_closed"] or 0)
        if abs(acc["cash"] - exp_cash) > EPS:
            problems.append(f"paper cash {acc['cash']:.4f} != {exp_cash:.4f} implied by trades")
        if abs(acc["realized_pnl"] - exp_real) > EPS:
            problems.append(f"realized P&L {acc['realized_pnl']:.4f} != {exp_real:.4f} implied by trades")
        if abs(acc["fees_paid"] - exp_fees) > EPS:
            problems.append(f"fees {acc['fees_paid']:.4f} != {exp_fees:.4f} implied by trades")
    bad = con.execute("SELECT COUNT(*) FROM paper_positions WHERE (status='closed' AND (exit_ts IS NULL OR pnl_usd IS NULL "
                      "OR proceeds IS NULL)) OR (status='open' AND exit_ts IS NOT NULL) OR status NOT IN ('open','closed')"
                      ).fetchone()[0]
    if bad:
        problems.append(f"{bad} paper positions with a half-written state")
    dup = con.execute("SELECT mint, COUNT(*) c FROM paper_positions WHERE status='open' GROUP BY mint HAVING c > 1").fetchall()
    if dup:
        problems.append(f"duplicate open positions: {[d[0] for d in dup]}")
    active = {r[0]: r[1] for r in con.execute(
        "SELECT strategy, SUM(active) FROM strategy_params GROUP BY strategy")}
    facts["strategies"] = {}
    for name, n_act in active.items():
        if n_act != 1:
            problems.append(f"{name}: {n_act} active param versions (must be 1)")
    versions = {(r[0], r[1]) for r in con.execute("SELECT strategy, version FROM strategy_params")}
    for u in con.execute("SELECT id, target, from_version, to_version FROM strategy_updates"):
        for v in (u["from_version"], u["to_version"]):
            if v is not None and (u["target"], v) not in versions:
                problems.append(f"strategy_update #{u['id']} ({u['target']}) points at missing version v{v}")
    for r in con.execute("SELECT target, to_version FROM strategy_updates s WHERE id = "
                         "(SELECT MAX(id) FROM strategy_updates WHERE target=s.target)"):
        act = con.execute("SELECT version FROM strategy_params WHERE strategy=? AND active=1", (r[0],)).fetchone()
        facts["strategies"][r[0]] = act[0] if act else None
        if act is None or act[0] != r[1]:
            problems.append(f"{r[0]}: newest strategy_update says v{r[1]} but active is v{act[0] if act else None}")
    facts["strategy_updates"] = con.execute("SELECT COUNT(*) FROM strategy_updates").fetchone()[0]
    return {"ok": not problems, "problems": problems, "facts": facts}


# ---------------------------------------------------------------- sizes
def file_bytes(db: Database) -> dict:
    ps = db.con.execute("PRAGMA page_size").fetchone()[0]
    pc = db.con.execute("PRAGMA page_count").fetchone()[0]
    fl = db.con.execute("PRAGMA freelist_count").fetchone()[0]
    wal = Path(db.path + "-wal")
    return {"file": pc * ps, "used": (pc - fl) * ps, "free": fl * ps, "free_pct": 100.0 * fl / pc if pc else 0.0,
            "wal": wal.stat().st_size if wal.exists() else 0}


def table_sizes(db: Database) -> dict[str, dict]:
    """bytes per table (table b-tree + its indexes) and rows. Uses the dbstat virtual table when the SQLite
    build has it, otherwise splits the used size by an estimate from column lengths."""
    owner = {r[0]: r[1] for r in db.con.execute("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
    tables = [r[0] for r in db.con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    out = {t: {"rows": db.con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0], "bytes": 0} for t in tables}
    try:
        for name, size, unused in db.con.execute("SELECT name, SUM(pgsize), SUM(unused) FROM dbstat GROUP BY name"):
            t = owner.get(name, name)
            if t in out:
                out[t]["bytes"] += size
                # used bytes (without the empty part of half-filled pages), x1.25 for steady-state page fill
                out[t]["used_bytes"] = out[t].get("used_bytes", 0) + (size - (unused or 0)) * 1.25
        method = "dbstat"
    except sqlite3.Error:
        method = "estimate"
        used = file_bytes(db)["used"]
        est = {}
        for t in tables:
            cols = [r[1] for r in db.con.execute(f'PRAGMA table_info("{t}")')]
            expr = " + ".join(f'COALESCE(LENGTH("{c}"), 1)' for c in cols) or "0"
            est[t] = (db.con.execute(f'SELECT SUM({expr}) FROM "{t}"').fetchone()[0] or 0) * 1.6
        tot = sum(est.values()) or 1
        for t in tables:
            out[t]["bytes"] = int(used * est[t] / tot)
    for t in out.values():
        t["bytes_per_row"] = min(t.get("used_bytes", t["bytes"]), t["bytes"] * 1.25) / t["rows"] if t["rows"] else 0.0
    out["_method"] = method  # type: ignore[assignment]
    return out


def retention_specs(s: Settings) -> list[dict]:
    """(table, time column, retention hours, optional WHERE, optional column stripped after N hours)."""
    return [
        {"table": "price_snapshots", "col": "ts", "ret_h": s.snapshot_retention_h},
        {"table": "candles_1m", "col": "ts", "ret_h": s.candle_retention_h},
        {"table": "rugcheck_cache", "col": "ts", "ret_h": 1},
        {"table": "safety_reports", "col": "checked_ts", "ret_h": s.snapshot_retention_h},
        {"table": "seen_tokens", "col": "first_seen_ts", "ret_h": s.seen_retention_d * 24, "strip": ("raw_json",), "strip_h": s.seen_raw_retention_h},
        {"table": "token_market", "col": "updated_ts", "ret_h": s.market_retention_d * 24},
        {"table": "collector_drops", "col": "ts", "ret_h": s.market_retention_d * 24},
        {"table": "signals", "col": "ts", "where": "alert_type='REJECT'", "ret_h": s.signal_reject_retention_d * 24,
         "strip": ("features_json", "detectors_json"), "strip_h": s.signal_detail_retention_h},
        {"table": "signals", "col": "ts", "where": "alert_type!='REJECT'", "ret_h": s.signal_retention_d * 24},
        {"table": "judge_verdicts", "col": "ts", "ret_h": s.signal_retention_d * 24},
        {"table": "paper_equity", "col": "ts", "ret_h": s.paper_equity_retention_d * 24},
        {"table": "universe_runs", "col": "ts", "ret_h": 7 * 24},
        {"table": "creator_blacklist", "col": "ts", "ret_h": s.creator_blacklist_retention_d * 24},
        {"table": "learning_log", "col": "ts", "ret_h": 90 * 24},
        # kept forever (small): one row per paper trade / per strategy change
        {"table": "paper_positions", "col": "open_ts", "ret_h": float("inf"), "forever": True},
        {"table": "strategy_updates", "col": "ts", "ret_h": float("inf"), "forever": True},
    ]


HORIZONS_D = (1, 3, 7, 30, 90)


def projection(db: Database, s: Settings, since_ts: float, now: float | None = None) -> dict:
    """Database size after N days of running: insert rate measured since `since_ts` x min(retention, N days)
    x bytes per row (dbstat, indexes included); columns that pruning strips later are subtracted for the
    stripped part. Rolling tables stop growing at their retention; 'forever' tables (paper trades,
    strategy updates) keep growing slowly. `steady_mb` = size after 90 days (every rolling table full).
    An estimate from a short window, not a promise: market activity changes the rates."""
    now = now or time.time()
    hours = max((now - since_ts) / 3600, 1e-6)
    sizes = table_sizes(db)
    rows_out = []
    at = {d: 0.0 for d in HORIZONS_D}
    forever_year = 0.0
    for sp in retention_specs(s):
        t = sp["table"]
        info = sizes.get(t)
        if not isinstance(info, dict) or not info["rows"]:
            continue
        w = f" AND {sp['where']}" if sp.get("where") else ""
        n_new = db.con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{sp["col"]}" >= ?{w}', (since_ts,)).fetchone()[0]
        rate = n_new / hours
        if t == "paper_positions":  # can't exceed the daily trade target
            rate = min(rate, s.paper_daily_target / 24)
        bpr = info["bytes_per_row"]
        avg_strip = 0.0
        if sp.get("strip"):
            expr = " + ".join(f'COALESCE(LENGTH("{c}"), 0)' for c in sp["strip"])
            avg_strip = db.con.execute(f'SELECT AVG({expr}) FROM "{t}" WHERE "{sp["col"]}" >= ?{w}',
                                       (since_ts,)).fetchone()[0] or 0.0
        ret_h = float("inf") if sp.get("forever") else sp["ret_h"]

        def size(h: float) -> float:
            kept = min(ret_h, h)
            save = rate * max(kept - sp.get("strip_h", kept), 0) * avg_strip
            return max(rate * kept * bpr - save, 0.0)
        for d in HORIZONS_D:
            at[d] += size(d * 24)
        if sp.get("forever"):
            forever_year += size(365 * 24)
        rows_out.append({"table": t + (f" [{sp['where']}]" if sp.get("where") else ""), "rows_per_h": round(rate, 1),
                         "retention_h": ret_h, "bytes_per_row": round(bpr), "steady_rows": int(rate * min(ret_h, 90 * 24)),
                         "steady_mb": round(size(90 * 24) / 1e6, 2), "forever": bool(sp.get("forever"))})
    covered = {sp["table"] for sp in retention_specs(s)}
    other = sum(v["bytes"] for k, v in sizes.items() if isinstance(v, dict) and k not in covered)
    rows_out.sort(key=lambda r: -r["steady_mb"])
    size_at = {d: round((v + other) / 1e6, 1) for d, v in at.items()}
    return {"hours": hours, "tables": rows_out, "other_mb": round(other / 1e6, 2), "size_at_days": size_at,
            "steady_mb": size_at[90], "forever_mb_per_year": round(forever_year / 1e6, 1), "method": sizes["_method"]}


# ---------------------------------------------------------------- backup
# market data the bot rebuilds by itself within minutes after a restore: left out of "state" backups
TRANSIENT = ("price_snapshots", "candles_1m", "rugcheck_cache", "safety_reports", "collector_drops", "seen_tokens",
             "universe_runs")


def _strip_transient(con: sqlite3.Connection) -> None:
    for t in TRANSIENT:
        con.execute(f'DELETE FROM "{t}"')
    con.execute("DELETE FROM token_market WHERE mint NOT IN (SELECT mint FROM paper_positions WHERE status='open')")
    con.execute("DELETE FROM signals WHERE alert_type='REJECT'")
    con.commit()
    con.execute("VACUUM")


def backup_now(db: Database, backup_dir: str | Path, keep: int = 7, now: float | None = None, tag: str = "",
               mode: str = "state") -> dict:
    """Online backup (SQLite backup API, consistent even while the bot writes), verified with quick_check on the
    copy, gzip-compressed, written atomically (tmp + rename), then rotated to the newest `keep` files.
    mode='state' drops the rebuildable market data from the copy (paper account/trades, strategy versions,
    strategy updates, learning log, judge verdicts, non-REJECT signals, blacklist, meta all stay)."""
    now = now or time.time()
    d = Path(backup_dir)
    d.mkdir(parents=True, exist_ok=True)
    base = f"shadow-{fmt(now, '%Y%m%d-%H%M%S')}-{mode}{('-' + tag) if tag else ''}"
    name, i = base + ".db", 1
    while (d / (name + ".gz")).exists():
        i += 1
        name = f"{base}-{i}.db"
    raw = d / (name + ".tmp")
    dst = sqlite3.connect(raw)
    try:
        db.con.backup(dst)
        if mode == "state":
            _strip_transient(dst)
        check = dst.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        dst.close()
    if check != "ok":
        raw.unlink(missing_ok=True)
        raise RuntimeError(f"backup copy failed quick_check: {check}")
    gz = d / (name + ".gz")
    tmp_gz = d / (name + ".gz.tmp")
    with open(raw, "rb") as fi, gzip.open(tmp_gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    raw_size = raw.stat().st_size
    raw.unlink()
    os.replace(tmp_gz, gz)
    removed = rotate_backups(d, keep)
    return {"path": str(gz), "db_bytes": raw_size, "gz_bytes": gz.stat().st_size, "removed": removed, "mode": mode}


def list_backups(backup_dir: str | Path) -> list[Path]:
    d = Path(backup_dir)
    return sorted(d.glob("shadow-*.db.gz")) if d.exists() else []


def rotate_backups(backup_dir: str | Path, keep: int) -> list[str]:
    files = list_backups(backup_dir)
    old = files[:-keep] if keep > 0 else files
    for f in old:
        f.unlink(missing_ok=True)
    for f in Path(backup_dir).glob("*.tmp"):  # leftovers of a backup killed half-way
        f.unlink(missing_ok=True)
    return [f.name for f in old]


def verify_backup(path: str | Path) -> str:
    """Decompress to a temp file and run integrity_check on it."""
    path = Path(path)
    tmp = path.with_suffix(".verify.tmp")
    try:
        with gzip.open(path, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        con = sqlite3.connect(tmp)
        try:
            return con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- maintenance loop
class Maintenance:
    def __init__(self, settings: Settings, db: Database, clock=time.time):
        self.s, self.db, self.clock = settings, db, clock
        self.stats: Counter = Counter()
        self.last: dict = {}
        self.errors: list[str] = []

    def _since(self, key: str) -> float:
        v = self.db.meta_get(key)
        return self.clock() - float(v) if v else float("inf")

    def prune(self, factor: float = 1.0) -> dict:
        """Normal retention prune; factor < 1 shrinks the short (hours-scale) retentions for an emergency."""
        s = self.s
        f = max(min(factor, 1.0), 0.05)
        return self.db.cleanup(max(s.snapshot_retention_h * f, 2.0), max(s.candle_retention_h * f, 2.5),
                               s.market_retention_d * f, max(s.seen_retention_d * f, 1.0), now=self.clock(),
                               signal_retention_d=max(s.signal_retention_d * f, 7),
                               signal_reject_retention_d=max(s.signal_reject_retention_d * f, 0.25),
                               signal_detail_retention_h=max(s.signal_detail_retention_h * f, 1),
                               paper_equity_retention_d=max(s.paper_equity_retention_d * f, 7),
                               creator_blacklist_retention_d=max(s.creator_blacklist_retention_d * f, 7),
                               seen_raw_retention_h=s.seen_raw_retention_h * f)

    def enforce_cap(self) -> dict | None:
        """DB_MAX_MB hard cap on the used size: prune harder (x0.5, x0.25, x0.125) until under, then VACUUM."""
        cap = self.s.db_max_mb * 1e6
        if file_bytes(self.db)["used"] <= cap:
            return None
        rounds = []
        for f in (0.5, 0.25, 0.125):
            rounds.append({"factor": f, **self.prune(f)})
            if file_bytes(self.db)["used"] <= cap * 0.9:
                break
        self.vacuum(force=True)
        self.stats["emergency_prune"] += 1
        after = file_bytes(self.db)
        # floors (2 h snapshots, 2.5 h candles, 1 d de-dup, ...) keep what the live signals need, so a cap
        # smaller than the live working set can't be met: reported, never "fixed" by breaking the bot
        out = {"rounds": rounds, "after_mb": round(after["file"] / 1e6, 2), "still_over": after["used"] > cap}
        log.warning("DB over DB_MAX_MB=%s: emergency prune %s", self.s.db_max_mb, out)
        return out

    def vacuum(self, force: bool = False) -> bool:
        fb = file_bytes(self.db)
        if not force and (self._since("last_vacuum_ts") < self.s.vacuum_every_h * 3600
                          or fb["free_pct"] < self.s.vacuum_min_free_pct):
            return False
        self.db.con.execute("VACUUM")
        self.db.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.meta_set("last_vacuum_ts", self.clock())
        self.stats["vacuum"] += 1
        return True

    def optimize(self, force: bool = False) -> bool:
        if not force and self._since("last_optimize_ts") < self.s.optimize_every_h * 3600:
            return False
        self.db.con.execute("PRAGMA optimize")
        self.db.meta_set("last_optimize_ts", self.clock())
        self.stats["optimize"] += 1
        return True

    def backup_if_due(self, force: bool = False) -> dict | None:
        today = day_key(self.clock())
        if not force and self.db.meta_get("last_backup_day") == today:
            return None
        out = backup_now(self.db, self.s.path(self.s.backup_dir), self.s.backup_keep, now=self.clock(),
                         mode=self.s.backup_mode)
        self.db.meta_set("last_backup_day", today)
        self.db.meta_set("last_backup_path", out["path"])
        self.stats["backup"] += 1
        return out

    def run_once(self) -> dict:
        out: dict = {}
        for name, fn in (("prune", self.prune), ("cap", self.enforce_cap), ("optimize", self.optimize),
                         ("vacuum", self.vacuum), ("backup", self.backup_if_due)):
            try:
                out[name] = fn()
            except Exception as e:  # maintenance must never kill the bot
                msg = f"{name}: {type(e).__name__}: {str(e)[:160]}"
                self.errors.append(msg)
                self.stats["errors"] += 1
                log.warning("maintenance %s", msg)
        try:
            self.db.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.stats["runs"] += 1
        self.last = out
        return out

    async def run(self, stop: asyncio.Event, initial_delay: float = 30.0) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=initial_delay)
            return
        except asyncio.TimeoutError:
            pass
        while not stop.is_set():
            self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.maintenance_sec)
            except asyncio.TimeoutError:
                pass


__all__ = ["Maintenance", "integrity_check", "consistency", "file_bytes", "table_sizes", "projection",
           "backup_now", "list_backups", "rotate_backups", "verify_backup", "retention_specs"]
