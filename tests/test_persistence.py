"""Phase 7: SQLite persistence hardening. WAL + busy_timeout, single writer (tx + lock file), crash safety
(a real kill -9 + restart), retention + size cap, VACUUM/optimize, daily backups + rotation, PKT day rollover,
indexes, CSV export and the `--debug db` CLI."""
import csv
import gzip
import io
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from shadow import timeutil
from shadow.config import load_settings
from shadow.db import MIGRATIONS, SCHEMA_VERSION, Database, WriterLocked
from shadow.export import daily_csv, daily_rows, export_all, trades_csv
from shadow.maintenance import (Maintenance, backup_now, consistency, file_bytes, integrity_check, list_backups,
                                projection, rotate_backups, table_sizes, verify_backup)
from shadow.paper import PaperEngine, day_start
from shadow.safety import PASS, SafetyReport

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "crash_worker.py"
NOW = 1_800_000_000.0  # 2027-01-15 13:20 PKT


def settings(tmp_path, **kw):
    s = load_settings(env={"BACKUP_DIR": str(tmp_path / "backups")})
    return replace(s, **kw) if kw else s


class Chk:
    def __init__(self):
        self.reps = {}

    def get_report(self, m):
        return self.reps.get(m)


def rep(m, now):
    return SafetyReport(m, now, now + 900, 90, PASS, [], [], {"quote": {"sellable": True, "impact_pct": 1.0}}, quote_ts=now)


def sig(m, sid=None, price=1.0):
    return {"id": sid, "mint": m, "symbol": m[:3], "universe": "new", "strategy": "early_entry", "params_version": 1,
            "alert_type": "BUY_WATCH", "judge_verdict": "APPROVE", "judge_confidence": 0.7, "score": 75, "price": price,
            "entry": price, "sl": 0.9, "tp": 1.3, "time_stop_min": 20}


# ---------------------------------------------------------------- WAL / busy_timeout / transactions
def test_wal_busy_timeout_autocommit(tmp_path):
    db = Database(tmp_path / "a.db", busy_timeout_ms=7000)
    assert db.con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.con.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    assert db.con.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert db.con.isolation_level is None and db.version == SCHEMA_VERSION == 6


def test_tx_is_reentrant_and_rolls_back_everything(tmp_path):
    db = Database(tmp_path / "a.db")
    with pytest.raises(RuntimeError):
        with db.tx():
            db.con.execute("INSERT INTO meta (key, value) VALUES ('a', '1')")
            with db.tx():  # nested joins the outer transaction
                db.con.execute("INSERT INTO meta (key, value) VALUES ('b', '2')")
            raise RuntimeError("crash in the middle")
    assert db.con.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0 and not db.con.in_transaction
    with db.tx():
        db.meta_set("x", 1)  # meta_set has its own tx() -> joins
    assert db.meta_get("x") == "1"


def test_paper_open_is_atomic_under_failure(tmp_path, monkeypatch):
    """A failure between the position row and the cash debit leaves neither (one transaction)."""
    s = settings(tmp_path)
    db = Database(tmp_path / "a.db")
    chk = Chk()
    pe = PaperEngine(s, db, checker=chk, clock=lambda: NOW)
    m = "A" * 44
    chk.reps[m] = rep(m, NOW)

    def boom(*a, **k):
        raise sqlite3.OperationalError("simulated crash after the position insert")
    monkeypatch.setattr(pe, "_cash_add", boom)
    with pytest.raises(sqlite3.OperationalError):
        pe.open_from_signal(sig(m), chk.reps[m], {})
    assert db.con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0
    assert pe.account()["cash"] == 100 and consistency(db)["ok"]


def test_close_twice_credits_cash_once(tmp_path):
    s = settings(tmp_path)
    db = Database(tmp_path / "a.db")
    chk = Chk()
    pe = PaperEngine(s, db, checker=chk, clock=lambda: NOW)
    m = "B" * 44
    chk.reps[m] = rep(m, NOW)
    pid = pe.open_from_signal(sig(m), chk.reps[m], {})
    pos = pe.get(pid)
    pe.close(pos, 1.2, "take_profit")
    pe.close(pos, 1.2, "take_profit")  # stale copy of the open row (e.g. a SELL signal racing the TP)
    assert consistency(db)["ok"], consistency(db)


# ---------------------------------------------------------------- single writer lock
def test_writer_lock_blocks_second_process_and_dies_with_kill9(tmp_path):
    path = tmp_path / "a.db"
    Database(path).close()
    holder = subprocess.Popen([sys.executable, "-c", (
        f"import sys,time; sys.path.insert(0, {str(ROOT)!r}); from shadow.db import Database; "
        f"d=Database({str(path)!r}); d.acquire_writer_lock(); print('locked', flush=True); time.sleep(60)")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        db = Database(path)
        with pytest.raises(WriterLocked):
            db.acquire_writer_lock()
        ro = Database.open_readonly(path)  # readers (dashboard) still work while the bot writes
        assert ro.con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            ro.con.execute("INSERT INTO meta (key) VALUES ('x')")
        ro.close()
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait()
    db.acquire_writer_lock()  # the kernel released the dead process's lock: no stale lock after a crash
    db.close()


# ---------------------------------------------------------------- the real kill -9 test
def _snapshot(path):
    db = Database(path)
    try:
        c = db.con
        return {"ic": integrity_check(db), "cons": consistency(db),
                "positions": c.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0],
                "closed": c.execute("SELECT COUNT(*) FROM paper_positions WHERE status='closed'").fetchone()[0],
                "updates": c.execute("SELECT COUNT(*) FROM strategy_updates").fetchone()[0],
                "versions": c.execute("SELECT COUNT(*) FROM strategy_params").fetchone()[0],
                "logs": c.execute("SELECT COUNT(*) FROM learning_log WHERE action IN ('activate','rollback')").fetchone()[0]}
    finally:
        db.close()


def test_kill9_mid_run_then_restart_is_consistent(tmp_path):
    """Start a writer that does the bot's multi-row writes in a tight loop, SIGKILL it at a random moment,
    restart, verify, repeat. Paper account == trade rows, one active version per strategy, every
    strategy_update matches a stored version + a change-log row, nothing committed before is lost."""
    path = str(tmp_path / "crash.db")
    prev = None
    rnd = __import__("random").Random(7)
    for round_ in range(8):
        p = subprocess.Popen([sys.executable, str(WORKER), path, str(round_)], stdout=subprocess.PIPE, text=True)
        target = rnd.randint(150, 3000)
        n = 0
        for line in p.stdout:
            n += 1
            if n >= target:
                break
        time.sleep(rnd.uniform(0, 0.05))  # land the kill anywhere inside the next writes
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        assert p.returncode == -signal.SIGKILL
        snap = _snapshot(path)
        assert snap["ic"] == ["ok"], snap["ic"]
        assert snap["cons"]["ok"], snap["cons"]["problems"]
        assert snap["updates"] == snap["logs"], snap  # every notification has its change-log row and vice versa
        assert snap["updates"] > 0 and snap["positions"] > 0
        if prev:  # restart kept everything that was committed before the previous kill
            for k in ("positions", "closed", "updates", "versions"):
                assert snap[k] >= prev[k], (k, prev, snap)
        prev = snap
    # a normal restart picks up the same account and open positions
    s = settings(tmp_path, paper_start_balance=100000.0)
    db = Database(path)
    pe = PaperEngine(s, db, checker=Chk())
    assert pe.account()["cash"] == pytest.approx(prev["cons"]["facts"]["cash"], abs=1e-3)
    assert len(pe.open_positions()) == prev["cons"]["facts"]["n_open"]
    db.close()


# ---------------------------------------------------------------- retention / cap / vacuum / optimize
def _signal(db, ts, alert="REJECT", status=None):
    return db.add_signal({"ts": ts, "mint": "C" * 44, "universe": "new", "strategy": "early_entry", "alert_type": alert,
                          "status": status, "features_json": '{"x": 1}', "detectors_json": "[]"})


def test_signal_retention_rules(tmp_path):
    db = Database(tmp_path / "a.db")
    now = NOW
    old_rej = _signal(db, now - 4 * 86400)
    day_rej = _signal(db, now - 2 * 86400)
    new_rej = _signal(db, now - 3600)
    old_bw = _signal(db, now - 10 * 86400, "BUY_WATCH", "closed")
    ancient_bw = _signal(db, now - 40 * 86400, "BUY_WATCH", "closed")
    open_bw = _signal(db, now - 40 * 86400, "BUY_WATCH", "open")
    traded = _signal(db, now - 40 * 86400, "BUY_WATCH", "closed")
    db.con.execute("INSERT INTO paper_positions (signal_id, mint, status, open_ts) VALUES (?, 'C', 'closed', ?)",
                   (traded, now - 40 * 86400))
    db.cleanup(3, 4, 1, 1, now=now)
    ids = {r[0] for r in db.con.execute("SELECT id FROM signals")}
    assert old_rej not in ids and ancient_bw not in ids
    assert {day_rej, new_rej, old_bw, open_bw, traded} <= ids
    feats = dict(db.con.execute("SELECT id, features_json FROM signals").fetchall())
    assert feats[day_rej] is None and feats[new_rej] is not None and feats[old_bw] is not None


def test_size_cap_emergency_prune_and_vacuum(tmp_path):
    s = settings(tmp_path, db_max_mb=1.0)
    db = Database(tmp_path / "a.db")
    # 3 h of snapshots (normal retention 3 h keeps them all), bigger than the 1 MB cap
    rows = [(f"M{i % 300}", NOW - 3 * 3600 + 60 + i * 0.36, 1.0, 1e5, 1e4, 5.0, 1e5, 1, 1, "x" * 40) for i in range(30000)]
    db.add_snapshots(rows)
    m = Maintenance(s, db, clock=lambda: NOW)
    m.prune()
    assert db.con.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 30000
    before = file_bytes(db)["file"]
    assert file_bytes(db)["used"] > 1e6
    out = m.enforce_cap()
    assert out and m.stats["emergency_prune"] == 1 and m.stats["vacuum"] == 1
    left = db.con.execute("SELECT MIN(ts), COUNT(*) FROM price_snapshots").fetchone()
    assert left[0] >= NOW - 2 * 3600 - 1 and left[1] < 30000 * 0.7  # pruned down to the 2 h floor
    assert file_bytes(db)["file"] < before * 0.75 and out["still_over"] == (file_bytes(db)["used"] > 1e6)
    assert m.enforce_cap() is None or m.stats["emergency_prune"] == 2


def test_size_cap_met_when_possible(tmp_path):
    s = settings(tmp_path, db_max_mb=0.5)
    db = Database(tmp_path / "a.db")
    db.add_snapshots([(f"M{i % 300}", NOW - 2.9 * 3600 + i * 0.1, 1.0, 1, 1, 1, 1, 1, 1, "x" * 40) for i in range(20000)])
    m = Maintenance(s, db, clock=lambda: NOW)
    out = m.enforce_cap()  # all rows are older than the 2 h floor -> can go under the cap
    assert out and out["still_over"] is False and file_bytes(db)["file"] <= 0.5e6


def test_vacuum_only_when_due_and_fragmented(tmp_path):
    s = settings(tmp_path, vacuum_min_free_pct=10.0, vacuum_every_h=24.0)
    db = Database(tmp_path / "a.db")
    db.add_snapshots([(f"M{i}", NOW, 1.0, 1, 1, 1, 1, 1, 1, "y" * 80) for i in range(20000)])
    clock = {"t": NOW}
    m = Maintenance(s, db, clock=lambda: clock["t"])
    assert m.vacuum() is False  # no free pages yet
    db.con.execute("DELETE FROM price_snapshots")
    big = file_bytes(db)["file"]
    assert file_bytes(db)["free_pct"] > 50
    assert m.vacuum() is True and file_bytes(db)["file"] < big / 3
    db.add_snapshots([(f"M{i}", NOW, 1.0, 1, 1, 1, 1, 1, 1, "y" * 80) for i in range(5000)])
    db.con.execute("DELETE FROM price_snapshots")
    clock["t"] += 3600
    assert m.vacuum() is False  # fragmented, but the last VACUUM was < 24 h ago
    assert m.optimize() is True and m.optimize() is False


def test_maintenance_run_once_never_raises(tmp_path, monkeypatch):
    s = settings(tmp_path)
    db = Database(tmp_path / "a.db")
    m = Maintenance(s, db, clock=lambda: NOW)
    monkeypatch.setattr(m, "vacuum", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O")))
    out = m.run_once()
    assert "prune" in out and m.stats["errors"] == 1 and "vacuum" in m.errors[0]
    assert out["backup"] and Path(out["backup"]["path"]).exists()


# ---------------------------------------------------------------- backups
def test_backup_verify_rotate_and_restore(tmp_path):
    s = settings(tmp_path)
    db = Database(tmp_path / "a.db")
    chk = Chk()
    pe = PaperEngine(s, db, checker=chk, clock=lambda: NOW)
    m = "D" * 44
    chk.reps[m] = rep(m, NOW)
    pe.open_from_signal(sig(m), chk.reps[m], {})
    d = tmp_path / "backups"
    paths = [backup_now(db, d, keep=3, now=NOW + i * 86400)["path"] for i in range(5)]
    files = list_backups(d)
    assert [f.name for f in files] == [Path(p).name for p in paths[-3:]]
    assert verify_backup(files[-1]) == "ok"
    (d / "shadow-x.db.gz.tmp").write_bytes(b"half")  # leftover of a backup killed half-way
    rotate_backups(d, 3)
    assert not list(d.glob("*.tmp"))
    # restore = gunzip while the bot is stopped -> same paper account and open position
    restored = tmp_path / "restored.db"
    with gzip.open(files[-1], "rb") as fi:
        restored.write_bytes(fi.read())
    r = Database(restored)
    assert r.con.execute("SELECT cash FROM paper_account").fetchone()[0] == pytest.approx(98.0)
    assert r.con.execute("SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0] == 1


def test_state_backup_keeps_durable_state_only(tmp_path):
    db = Database(tmp_path / "a.db")
    s = settings(tmp_path)
    chk = Chk()
    pe = PaperEngine(s, db, checker=chk, clock=lambda: NOW)
    m = "G" * 44
    chk.reps[m] = rep(m, NOW)
    pe.open_from_signal(sig(m), chk.reps[m], {})
    db.add_snapshots([("S", NOW, 1.0, 1, 1, 1, 1, 1, 1, "x")] * 1000)
    _signal(db, NOW, "REJECT")
    keep_sig = _signal(db, NOW, "BUY_WATCH", "closed")
    db.add_strategy_update("param_change", "early_entry", 1, 2, {"tp_pct": [30, 33]}, "test", {"n": 30})
    st = backup_now(db, tmp_path / "bk", keep=5, now=NOW, mode="state")
    full = backup_now(db, tmp_path / "bk", keep=5, now=NOW, mode="full")  # same second: no overwrite
    assert st["path"] != full["path"] and len(list_backups(tmp_path / "bk")) == 2
    assert st["gz_bytes"] < full["gz_bytes"]
    r = tmp_path / "r.db"
    with gzip.open(st["path"], "rb") as fi:
        r.write_bytes(fi.read())
    c = sqlite3.connect(r)
    assert c.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM strategy_updates").fetchone()[0] == 1
    assert [x[0] for x in c.execute("SELECT id FROM signals")] == [keep_sig]
    c.close()


def test_daily_backup_once_per_pkt_day(tmp_path):
    s = settings(tmp_path, backup_keep=7)
    db = Database(tmp_path / "a.db")
    timeutil.set_timezone("Asia/Karachi")
    base = day_start(NOW) + 23 * 3600 + 50 * 60  # 23:50 PKT
    clock = {"t": base}
    m = Maintenance(s, db, clock=lambda: clock["t"])
    assert m.backup_if_due() is not None
    clock["t"] = base + 5 * 60  # 23:55 same PKT day
    assert m.backup_if_due() is None
    clock["t"] = base + 15 * 60  # 00:05 PKT next day
    assert m.backup_if_due() is not None
    assert len(list_backups(s.path(s.backup_dir))) == 2


# ---------------------------------------------------------------- PKT day rollover
def test_day_start_is_pkt_midnight_whatever_the_phone_tz(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        timeutil.set_timezone("Asia/Karachi")
        # 18:59:59 UTC = 23:59:59 PKT (same day); 19:00 UTC = 00:00 PKT (new day)
        t1 = 1_800_000_000 - (1_800_000_000 % 86400) + 18 * 3600 + 59 * 60 + 59
        assert timeutil.day_key(t1) != timeutil.day_key(t1 + 1)
        assert day_start(t1 + 1) == t1 + 1 and day_start(t1) == t1 + 1 - 86400
        s = settings(tmp_path)
        db = Database(tmp_path / "a.db")
        chk = Chk()
        clock = {"t": t1 - 60}
        pe = PaperEngine(s, db, checker=chk, clock=lambda: clock["t"])
        for i in range(3):
            m = f"E{i}".ljust(44, "e")
            chk.reps[m] = rep(m, clock["t"])
            pe.open_from_signal(sig(m), chk.reps[m], {})
        assert pe.trades_today() == 3
        clock["t"] = t1 + 61  # 00:01 PKT
        assert pe.trades_today() == 0  # the 50-trade counter rolled over at PKT midnight
        days = [r["day"] for r in daily_rows(db)]
        assert len(days) == 1 and days[0] == timeutil.day_key(t1)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_unknown_timezone_falls_back_to_pkt():
    warn = timeutil.set_timezone("Mars/Olympus")
    assert warn and timeutil.local_dt(0).utcoffset().total_seconds() == 5 * 3600
    timeutil.set_timezone("Asia/Karachi")


# ---------------------------------------------------------------- indexes
QUERIES = {
    "trades_today": ("SELECT COUNT(*) FROM paper_positions WHERE open_ts>=?", (0,)),
    "history": ("SELECT * FROM paper_positions WHERE status='closed' AND exit_ts>=? ORDER BY exit_ts DESC LIMIT 5", (0,)),
    "positions": ("SELECT * FROM paper_positions WHERE status='open' ORDER BY open_ts", ()),
    "signal_feed": ("SELECT * FROM signals WHERE ts>=? ORDER BY ts DESC LIMIT 50", (0,)),
    "report_universe": ("SELECT alert_type, COUNT(*) FROM signals WHERE universe=? AND alert_type=? AND ts>=?", ("new", "BUY_WATCH", 0)),
    "judge_acc": ("SELECT * FROM judge_verdicts WHERE verdict='APPROVE' AND outcome_ts>=?", (0,)),
    "strategies_hist": ("SELECT * FROM strategy_updates WHERE target=? ORDER BY ts DESC LIMIT 20", ("early_entry",)),
    "unsent": ("SELECT * FROM strategy_updates WHERE sent_ts IS NULL ORDER BY ts", ()),
    "equity_curve": ("SELECT * FROM paper_equity WHERE ts>=? ORDER BY ts", (0,)),
}


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_phase8_phase9_queries_use_an_index(tmp_path, name):
    db = Database(tmp_path / "a.db")
    q, args = QUERIES[name]
    plan = " | ".join(r[3] for r in db.con.execute("EXPLAIN QUERY PLAN " + q, args))
    assert "USING" in plan and not plan.startswith("SCAN "), plan


def test_v5_database_upgrades_to_v6_keeping_data(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    for i, m in enumerate(MIGRATIONS[:5]):
        con.executescript(f"BEGIN;\n{m}\nPRAGMA user_version = {i + 1};\nCOMMIT;")
    con.execute("INSERT INTO paper_account (id, start_balance, cash, created_ts, updated_ts) VALUES (1, 100, 97.5, 1, 1)")
    con.commit()
    con.close()
    db = Database(path)
    assert db.version == 6 and db.con.execute("SELECT cash FROM paper_account").fetchone()[0] == 97.5
    idx = {r[0] for r in db.con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_paper_open_ts", "idx_paper_closed", "idx_signals_ts", "idx_updates_target"} <= idx


# ---------------------------------------------------------------- sizes / projection
def test_table_sizes_and_projection(tmp_path):
    s = settings(tmp_path)
    db = Database(tmp_path / "a.db")
    now = time.time()
    db.add_snapshots([(f"M{i % 100}", now - 1800 + i * 0.18, 1.0, 1, 1, 1, 1, 1, 1, "x") for i in range(10000)])
    sz = table_sizes(db)
    assert sz["price_snapshots"]["rows"] == 10000 and sz["price_snapshots"]["bytes"] > 0
    pj = projection(db, s, since_ts=now - 1800, now=now)
    row = next(r for r in pj["tables"] if r["table"] == "price_snapshots")
    # 10000 rows in 30 min = 20000/h, kept SNAPSHOT_RETENTION_H=3 h -> 60000 rows at steady state
    assert row["rows_per_h"] == pytest.approx(20000, rel=0.01) and row["steady_rows"] == pytest.approx(60000, rel=0.01)
    assert pj["steady_mb"] > 0


# ---------------------------------------------------------------- CSV export
def test_csv_export_trades_and_daily_by_pkt_day(tmp_path):
    timeutil.set_timezone("Asia/Karachi")
    s = settings(tmp_path)
    db = Database(tmp_path / "a.db")
    chk = Chk()
    midnight = day_start(NOW) + 86400
    clock = {"t": midnight - 1800}  # 23:30 PKT
    pe = PaperEngine(s, db, checker=chk, clock=lambda: clock["t"])
    for i, (t_open, px) in enumerate([(midnight - 1800, 2.0), (midnight + 1800, 0.8)]):
        clock["t"] = t_open
        m = f"F{i}".ljust(44, "f")
        chk.reps[m] = rep(m, t_open)
        pid = pe.open_from_signal(sig(m), chk.reps[m], {})
        clock["t"] = t_open + 600
        pe.close(pe.get(pid), px, "take_profit" if px > 1 else "stop_loss")
    rows = list(csv.DictReader(io.StringIO(trades_csv(db))))
    assert len(rows) == 2 and rows[0]["open_time"].endswith("+05:00") and rows[0]["status"] == "closed"
    assert float(rows[0]["pnl_usd"]) == pytest.approx(float(db.con.execute(
        "SELECT pnl_usd FROM paper_positions WHERE id=1").fetchone()[0]))
    daily = list(csv.DictReader(io.StringIO(daily_csv(db))))
    assert [d["opened"] for d in daily] == ["1", "1"]  # 23:30 and 00:30 PKT are two different days
    assert float(daily[0]["fees_usd"]) == pytest.approx(0.0) and daily[0]["wins"] == "1" and daily[1]["wins"] == "0"
    out = export_all(db, tmp_path / "exp", now=NOW)
    for k in ("trades", "daily_report", "equity", "signals", "judge", "strategy_updates"):
        assert Path(out[k]["path"]).exists()
    assert out["trades"]["rows"] == 2


# ---------------------------------------------------------------- --debug db
def _run_main(tmp_path, *args):
    env = {**os.environ, "DB_PATH": str(tmp_path / "cli.db"), "BACKUP_DIR": str(tmp_path / "bk")}
    return subprocess.run([sys.executable, str(ROOT / "main.py"), "--debug", "db", *args], capture_output=True,
                          text=True, env=env, timeout=60)


def test_debug_db_cli(tmp_path):
    Database(tmp_path / "cli.db").close()
    r = _run_main(tmp_path)
    assert r.returncode == 0 and "integrity_check: ok" in r.stdout and "consistency: OK" in r.stdout, r.stdout + r.stderr
    r = _run_main(tmp_path, "backup")
    assert r.returncode == 0 and "verify: ok" in r.stdout and list((tmp_path / "bk").glob("*.gz"))
    r = _run_main(tmp_path, "prune")
    assert r.returncode == 0 and "pruned" in r.stdout
    r = _run_main(tmp_path, "export", str(tmp_path / "exp"))
    assert r.returncode == 0 and "trades" in r.stdout and list((tmp_path / "exp").glob("trades-*.csv"))
    # while a bot holds the writer lock: info/backup still work read-only, prune is refused
    holder = subprocess.Popen([sys.executable, "-c", (
        f"import sys,time; sys.path.insert(0, {str(ROOT)!r}); from shadow.db import Database; "
        f"d=Database({str(tmp_path / 'cli.db')!r}); d.acquire_writer_lock(); print('locked', flush=True); time.sleep(60)")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        r = _run_main(tmp_path)
        assert r.returncode == 0 and "read-only: the bot is running" in r.stdout
        r = _run_main(tmp_path, "prune")
        assert r.returncode != 0 and "bot is running" in (r.stdout + r.stderr)
        r = _run_main(tmp_path, "backup")
        assert r.returncode == 0 and "verify: ok" in r.stdout
    finally:
        holder.kill()
        holder.wait()
