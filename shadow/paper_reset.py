"""v1.1.1 - Reset the paper account to a fresh balance WITHOUT losing history.

Order (all or nothing for the DB part):
  1. full verified backup of the whole database (data/archive/shadow-...-full-pre-reset.db.gz, never rotated)
  2. open positions are closed as `reset_archived` at their last known price (no fake profit: pnl = last mark)
  3. every paper trade, the equity curve and the account row are COPIED into archive tables
     (paper_archive_positions / paper_archive_equity / paper_resets) tagged with an era label, and also
     written to CSV files in data/archive/
  4. only then the live paper tables are emptied and the account restarts at the new balance
Signals, watches, judge verdicts, strategy versions and the learning history are NOT touched - the
self-learning keeps all evidence. Run with the bot stopped (the DB writer lock enforces this):
    python3 tools/reset_paper.py --balance 100
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .db import Database
from .maintenance import backup_now
from .timeutil import fmt


def _cols(db: Database, table: str) -> list[str]:
    return [r[1] for r in db.con.execute(f"PRAGMA table_info({table})")]


def archive_and_reset(db: Database, balance: float = 100.0, *, archive_dir: str | Path = "data/archive",
                      now: float | None = None, label: str | None = None, backup: bool = True) -> dict:
    now = now or time.time()
    era = label or "era-" + fmt(now, "%Y%m%d-%H%M%S")
    d = Path(archive_dir)
    d.mkdir(parents=True, exist_ok=True)
    bk = backup_now(db, d, keep=100000, now=now, tag="pre-reset", mode="full") if backup else None

    acc = db.con.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    acc = dict(acc) if acc else None
    pcols, ecols = _cols(db, "paper_positions"), _cols(db, "paper_equity")
    with db.tx():
        c = db.con
        c.execute(f"CREATE TABLE IF NOT EXISTS paper_archive_positions (era TEXT NOT NULL, archived_ts REAL NOT NULL, "
                  f"{', '.join(pcols)})")
        c.execute(f"CREATE TABLE IF NOT EXISTS paper_archive_equity (era TEXT NOT NULL, {', '.join(ecols)})")
        have = set(_cols(db, "paper_archive_positions"))        # schema grew (e.g. v1.1.2 partial_*): add columns
        for col in pcols:
            if col not in have:
                c.execute(f"ALTER TABLE paper_archive_positions ADD COLUMN {col}")
        c.execute("CREATE TABLE IF NOT EXISTS paper_resets (id INTEGER PRIMARY KEY, ts REAL NOT NULL, era TEXT NOT NULL, "
                  "old_start_balance REAL, old_cash REAL, old_realized_pnl REAL, n_trades INTEGER, n_wins INTEGER, "
                  "n_closed_at_reset INTEGER, new_balance REAL NOT NULL, backup_path TEXT, account_json TEXT)")
        # 2. open positions -> closed at the last mark (honest: whatever they were worth, no fill invented)
        n_open = 0
        for p in c.execute("SELECT * FROM paper_positions WHERE status='open'").fetchall():
            p = dict(p)
            px = p["last_price"] or p["entry_price"] or 0.0
            rest = (p["qty"] or 0.0) * px
            proceeds = rest + (p.get("partial_proceeds") or 0.0)      # a v1.1.2 partial sale was credited already
            pnl = proceeds - (p["size_usd"] or 0.0) - (p["buy_fee"] or 0.0)
            c.execute("UPDATE paper_positions SET status='closed', exit_ts=?, exit_market_price=?, exit_price=?, "
                      "sell_fee=0, exit_slippage_pct=0, exit_reason='reset_archived', proceeds=?, pnl_usd=?, pnl_pct=? "
                      "WHERE id=?", (now, px, px, proceeds, pnl,
                                     pnl / p["size_usd"] * 100 if p["size_usd"] else 0.0, p["id"]))
            if acc:
                c.execute("UPDATE paper_account SET cash=cash+?, realized_pnl=realized_pnl+?, updated_ts=? WHERE id=1",
                          (rest, proceeds - (p["size_usd"] or 0.0), now))
            n_open += 1
        acc = dict(c.execute("SELECT * FROM paper_account WHERE id=1").fetchone() or {}) or None
        n, wins = c.execute("SELECT COUNT(*), COALESCE(SUM(pnl_usd > 0), 0) FROM paper_positions "
                            "WHERE status='closed'").fetchone()
        # 3. copy everything into the archive (same columns + era)
        c.execute(f"INSERT INTO paper_archive_positions (era, archived_ts, {', '.join(pcols)}) "
                  f"SELECT ?, ?, {', '.join(pcols)} FROM paper_positions", (era, now))
        c.execute(f"INSERT INTO paper_archive_equity (era, {', '.join(ecols)}) SELECT ?, {', '.join(ecols)} "
                  f"FROM paper_equity", (era,))
        c.execute("INSERT INTO paper_resets (ts, era, old_start_balance, old_cash, old_realized_pnl, n_trades, n_wins, "
                  "n_closed_at_reset, new_balance, backup_path, account_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (now, era, acc and acc["start_balance"], acc and acc["cash"], acc and acc["realized_pnl"], n, wins,
                   n_open, balance, bk and bk["path"], json.dumps(acc)))
        archived = c.execute("SELECT COUNT(*) FROM paper_archive_positions WHERE era=?", (era,)).fetchone()[0]
        if archived != c.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]:
            raise RuntimeError("archive copy count mismatch - nothing was reset")
        # 4. fresh account
        c.execute("DELETE FROM paper_positions")
        c.execute("DELETE FROM paper_equity")
        c.execute("DELETE FROM paper_account")
        c.execute("INSERT INTO paper_account (id, start_balance, cash, realized_pnl, fees_paid, created_ts, updated_ts) "
                  "VALUES (1, ?, ?, 0, 0, ?, ?)", (balance, balance, now, now))
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('paper_era', ?)", (era,))
    # CSV copies (outside the DB, for humans)
    files = []
    for table, name in (("paper_archive_positions", "positions"), ("paper_archive_equity", "equity")):
        rows = db.con.execute(f"SELECT * FROM {table} WHERE era=?", (era,)).fetchall()
        f = d / f"paper_{name}_{era}.csv"
        with open(f, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(_cols(db, table))
            w.writerows([tuple(r) for r in rows])
        files.append(str(f))
    return {"era": era, "backup": bk and bk["path"], "trades_archived": archived, "wins": wins,
            "closed_at_reset": n_open, "old_cash": acc and acc["cash"], "old_realized_pnl": acc and acc["realized_pnl"],
            "new_balance": balance, "csv": files}


__all__ = ["archive_and_reset"]
