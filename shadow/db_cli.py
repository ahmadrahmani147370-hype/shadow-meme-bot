"""`python main.py --debug db [...]` (PHASE 7).

    (no args)            integrity_check, consistency, file/WAL size, table sizes + row counts, backups, meta
    prune                retention prune now (+ size cap)          [needs the writer lock: bot stopped]
    vacuum | optimize    VACUUM / PRAGMA optimize now               [needs the writer lock: bot stopped]
    backup [full]        verified gzip backup now + rotation        (works while the bot runs)
    verify [file]        integrity_check of a backup (newest by default)
    projection [hours]   steady-state size from the insert rate of the last N hours (default 1)
    export [dir]         CSV: trades, daily report, equity, signals, judge, strategy updates (default data/exports)
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import Settings
from .db import Database, WriterLocked
from .maintenance import (Maintenance, backup_now, consistency, file_bytes, integrity_check, list_backups, projection,
                          table_sizes, verify_backup)
from .timeutil import fmt, tz_name

WRITE_CMDS = {"prune", "vacuum", "optimize"}


def _open(s: Settings, cmd: str) -> tuple[Database, bool]:
    path = s.path(s.db_path)
    if not Path(path).exists():
        raise SystemExit(f"no database at {path} yet (run the bot once)")
    # take the writer lock first (without opening a writer connection) to see whether the bot is running
    probe = Database.__new__(Database)
    probe.path, probe._lock_fd = str(path), None
    try:
        probe.acquire_writer_lock()
    except WriterLocked:
        if cmd in WRITE_CMDS:
            raise SystemExit(f"the bot is running on {path}: '{cmd}' runs automatically every MAINTENANCE_SEC="
                             f"{s.maintenance_sec}s inside it (single writer). Stop the bot to run it by hand.")
        return Database.open_readonly(path, s.db_busy_timeout_ms), False
    db = Database(path, busy_timeout_ms=s.db_busy_timeout_ms)
    db._lock_fd, probe._lock_fd = probe._lock_fd, None
    return db, True


def report(db: Database, s: Settings, writer: bool) -> str:
    fb = file_bytes(db)
    ic = integrity_check(db)
    cons = consistency(db)
    sizes = table_sizes(db)
    method = sizes.pop("_method")
    lines = [f"DB {db.path} (schema v{db.version}, {'writer' if writer else 'read-only: the bot is running'}, TZ {tz_name()})",
             f"  file {fb['file'] / 1e6:.2f} MB | used {fb['used'] / 1e6:.2f} MB | free {fb['free_pct']:.1f}% | "
             f"WAL {fb['wal'] / 1e6:.2f} MB | cap DB_MAX_MB={s.db_max_mb:g}",
             f"  integrity_check: {', '.join(ic[:5])}",
             f"  consistency: {'OK' if cons['ok'] else 'PROBLEMS ' + '; '.join(cons['problems'])} {cons['facts']}",
             f"  tables (bytes incl. indexes via {method}):"]
    for t, v in sorted(sizes.items(), key=lambda kv: -kv[1]["bytes"]):
        lines.append(f"    {t:<20} {v['rows']:>9} rows {v['bytes'] / 1e6:>8.2f} MB")
    bks = list_backups(s.path(s.backup_dir))
    lines.append(f"  backups ({len(bks)}/{s.backup_keep} kept in {s.backup_dir}):")
    for b in bks[-s.backup_keep:]:
        lines.append(f"    {b.name} {b.stat().st_size / 1e6:.2f} MB")
    try:
        meta = {r[0]: r[1] for r in db.con.execute("SELECT key, value FROM meta")}
    except Exception:
        meta = {}
    for k in ("last_vacuum_ts", "last_optimize_ts"):
        if k in meta:
            meta[k] = fmt(float(meta[k]), "%d %b %H:%M")
    lines.append(f"  maintenance: {meta or 'never ran yet'}")
    return "\n".join(lines)


def run_db(s: Settings, rest: list[str]) -> int:
    cmd = rest[0] if rest else "info"
    db, writer = _open(s, cmd)
    try:
        if cmd == "info":
            print(report(db, s, writer))
        elif cmd == "prune":
            m = Maintenance(s, db)
            before = file_bytes(db)["used"]
            out = m.prune()
            cap = m.enforce_cap()
            m.vacuum()
            print(f"pruned {out} | cap {cap or 'ok'} | used {before / 1e6:.2f} -> {file_bytes(db)['used'] / 1e6:.2f} MB")
        elif cmd == "vacuum":
            before = file_bytes(db)["file"]
            Maintenance(s, db).vacuum(force=True)
            print(f"VACUUM done: file {before / 1e6:.2f} -> {file_bytes(db)['file'] / 1e6:.2f} MB")
        elif cmd == "optimize":
            Maintenance(s, db).optimize(force=True)
            print("PRAGMA optimize done")
        elif cmd == "backup":
            mode = "full" if "full" in rest else s.backup_mode
            out = backup_now(db, s.path(s.backup_dir), s.backup_keep, tag="manual", mode=mode)
            print(f"{mode} backup {out['path']} ({out['db_bytes'] / 1e6:.2f} MB -> {out['gz_bytes'] / 1e6:.2f} MB gz), "
                  f"verify: {verify_backup(out['path'])}, rotated out: {out['removed'] or 'none'}")
            if writer:
                db.meta_set("last_backup_path", out["path"])
        elif cmd == "verify":
            files = [Path(rest[1])] if len(rest) > 1 else list_backups(s.path(s.backup_dir))[-1:]
            if not files:
                print("no backups yet")
                return 1
            for f in files:
                print(f"{f}: {verify_backup(f)}")
        elif cmd == "projection":
            h = float(rest[1]) if len(rest) > 1 else 1.0
            pj = projection(db, s, since_ts=time.time() - h * 3600)
            print(f"size after N days (rates of the last {h:g} h, sizes via {pj['method']}): "
                  + ", ".join(f"{d}d {v} MB" for d, v in pj["size_at_days"].items())
                  + f" | kept-forever tables +{pj['forever_mb_per_year']} MB/year")
            for r in pj["tables"]:
                ret = "forever" if r["forever"] else f"{r['retention_h']:g}h"
                print(f"  {r['table']:<36} {r['rows_per_h']:>9}/h x {ret:>7} x {r['bytes_per_row']:>5} B "
                      f"= {r['steady_mb']:>7} MB at 90 days")
        elif cmd == "export":
            from .export import export_all
            out = export_all(db, s.path(rest[1] if len(rest) > 1 else "data/exports"))
            for k, v in out.items():
                if not k.startswith("_"):
                    print(f"  {k:<17} {v['rows']:>6} rows -> {v['path']}")
            print(f"  times in {tz_name()}")
        else:
            print(__doc__)
            return 2
    finally:
        db.close()
    return 0


__all__ = ["run_db", "report"]
