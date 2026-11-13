"""SQLite storage.

PHASE 7 builds the full schema (signals, prices, paper trades, positions). Phase 2 added the
migration framework and `seen_tokens` (de-dup across restarts); phase 3 adds token_market,
price_snapshots, candles_1m and collector_drops (schema v2) plus retention cleanup.
Schema changes are numbered migrations tracked in PRAGMA user_version, so later phases only append
to MIGRATIONS and existing databases upgrade in place without losing data.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from dataclasses import asdict, fields

from .models import AMM, BONDING, MIGRATED, Candle, MarketData, TokenEvent

PHASE = 7
IMPLEMENTED = False  # partial: migrations, seen_tokens, market data, snapshots, candles

MIGRATIONS: list[str] = [
    # v1 (phase 2): de-dup table for the scanner
    """
    CREATE TABLE IF NOT EXISTS seen_tokens (
        mint            TEXT PRIMARY KEY,
        name            TEXT,
        symbol          TEXT,
        source          TEXT NOT NULL,
        first_seen_ts   REAL NOT NULL,
        launch_ts       REAL,
        creator         TEXT,
        dev_buy_sol     REAL,
        dev_buy_tokens  REAL,
        initial_mcap_sol REAL,
        status          TEXT NOT NULL,
        launchpad       TEXT,
        pool            TEXT,
        pool_address    TEXT,
        migrated_ts     REAL,
        raw_json        TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_seen_first ON seen_tokens(first_seen_ts);
    CREATE INDEX IF NOT EXISTS idx_seen_status ON seen_tokens(status);
    """,
    # v2 (phase 3): market data, price snapshots, 1-minute candles, collector drops
    """
    CREATE TABLE IF NOT EXISTS token_market (
        mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, pair_address TEXT, dex_id TEXT, quote_symbol TEXT,
        price_usd REAL, price_native REAL, market_cap REAL, fdv REAL, liquidity_usd REAL, liquidity_source TEXT,
        vol_m5 REAL, vol_h1 REAL, vol_h6 REAL, vol_h24 REAL,
        buys_m5 INTEGER, sells_m5 INTEGER, buys_h1 INTEGER, sells_h1 INTEGER,
        buys_h6 INTEGER, sells_h6 INTEGER, buys_h24 INTEGER, sells_h24 INTEGER,
        price_change_m5 REAL, price_change_h1 REAL, pair_created_ts REAL, launch_ts REAL,
        migration_status TEXT, n_pairs INTEGER, source TEXT, updated_ts REAL,
        first_enriched_ts REAL, tracking INTEGER NOT NULL DEFAULT 0, stop_reason TEXT, stopped_ts REAL
    );
    CREATE INDEX IF NOT EXISTS idx_market_tracking ON token_market(tracking);
    CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY, mint TEXT NOT NULL, ts REAL NOT NULL, price_usd REAL, market_cap REAL,
        liquidity_usd REAL, vol_m5 REAL, vol_h24 REAL, buys_h24 INTEGER, sells_h24 INTEGER, source TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_snap_mint_ts ON price_snapshots(mint, ts);
    CREATE INDEX IF NOT EXISTS idx_snap_ts ON price_snapshots(ts);
    CREATE TABLE IF NOT EXISTS candles_1m (
        mint TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL,
        volume_usd REAL, buys INTEGER, sells INTEGER, n_snapshots INTEGER, source TEXT NOT NULL,
        PRIMARY KEY (mint, ts, source)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles_1m(ts);
    CREATE TABLE IF NOT EXISTS collector_drops (
        mint TEXT PRIMARY KEY, reason TEXT NOT NULL, ts REAL NOT NULL, detail TEXT
    );
    """,
]

MARKET_COLS = ("mint", "symbol", "name", "pair_address", "dex_id", "quote_symbol", "price_usd", "price_native",
               "market_cap", "fdv", "liquidity_usd", "liquidity_source", "vol_m5", "vol_h1", "vol_h6", "vol_h24",
               "buys_m5", "sells_m5", "buys_h1", "sells_h1", "buys_h6", "sells_h6", "buys_h24", "sells_h24",
               "price_change_m5", "price_change_h1", "pair_created_ts", "launch_ts", "migration_status",
               "n_pairs", "source", "updated_ts")

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, timeout=10)
        self.con.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.migrate()

    @property
    def version(self) -> int:
        return self.con.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        v = self.version
        for i in range(v, SCHEMA_VERSION):
            with self.con:
                self.con.executescript(MIGRATIONS[i])
                self.con.execute(f"PRAGMA user_version = {i + 1}")
        return self.version

    def close(self) -> None:
        self.con.close()

    # ---------------------------------------------------------------- seen tokens
    def is_seen(self, mint: str) -> bool:
        return self.con.execute("SELECT 1 FROM seen_tokens WHERE mint=?", (mint,)).fetchone() is not None

    def get_seen(self, mint: str) -> dict | None:
        r = self.con.execute("SELECT * FROM seen_tokens WHERE mint=?", (mint,)).fetchone()
        return dict(r) if r else None

    def mark_new(self, ev: TokenEvent) -> bool:
        """Insert a token the first time it is seen. Returns False if it was already known."""
        with self.con:
            cur = self.con.execute(
                """INSERT OR IGNORE INTO seen_tokens (mint, name, symbol, source, first_seen_ts, launch_ts, creator,
                   dev_buy_sol, dev_buy_tokens, initial_mcap_sol, status, launchpad, pool, pool_address, migrated_ts, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ev.mint, ev.name, ev.symbol, ev.source, ev.seen_ts, ev.launch_ts, ev.creator, ev.dev_buy_sol,
                 ev.dev_buy_tokens, ev.initial_mcap_sol, ev.status, ev.launchpad, ev.pool, ev.pool_address,
                 ev.seen_ts if ev.status == MIGRATED else None, json.dumps(ev.raw)[:4000]),
            )
        return cur.rowcount == 1

    def fill_missing(self, ev: TokenEvent) -> None:
        """A later source may know the name/symbol/creator the first source lacked."""
        with self.con:
            self.con.execute(
                """UPDATE seen_tokens SET name=COALESCE(NULLIF(name,''), ?), symbol=COALESCE(NULLIF(symbol,''), ?),
                   creator=COALESCE(NULLIF(creator,''), ?) WHERE mint=?""",
                (ev.name, ev.symbol, ev.creator, ev.mint))

    def mark_migrated(self, ev: TokenEvent) -> bool:
        """Record a migration. Returns False if this mint was already marked migrated (duplicate)."""
        row = self.get_seen(ev.mint)
        if row is None:
            self.mark_new(ev)
            return True
        if row["status"] == MIGRATED:
            return False
        with self.con:
            self.con.execute(
                "UPDATE seen_tokens SET status=?, pool=?, pool_address=COALESCE(NULLIF(?,''), pool_address), migrated_ts=? WHERE mint=?",
                (MIGRATED, ev.pool, ev.pool_address, ev.seen_ts, ev.mint))
        return True

    def status_of(self, mint: str) -> str | None:
        r = self.con.execute("SELECT status FROM seen_tokens WHERE mint=?", (mint,)).fetchone()
        return r[0] if r else None

    def count_seen(self) -> dict:
        rows = self.con.execute("SELECT status, COUNT(*) FROM seen_tokens GROUP BY status").fetchall()
        d = {r[0]: r[1] for r in rows}
        d["total"] = sum(d.values())
        return d

    def recent(self, n: int = 20) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM seen_tokens ORDER BY first_seen_ts DESC LIMIT ?", (n,))]

    def prune(self, older_than_days: float = 14) -> int:
        """Keep the de-dup table small on a phone (later phases keep their own history)."""
        with self.con:
            return self.con.execute("DELETE FROM seen_tokens WHERE first_seen_ts < ?",
                                    (time.time() - older_than_days * 86400,)).rowcount


    # ---------------------------------------------------------------- phase 3 tables
    def upsert_market(self, m: MarketData, tracking: bool | None = None) -> None:
        d = asdict(m)
        vals = [d[c] for c in MARKET_COLS]
        sets = ", ".join(f"{c}=excluded.{c}" for c in MARKET_COLS if c != "mint")
        track_sql = "" if tracking is None else f", tracking={1 if tracking else 0}"
        with self.con:
            self.con.execute(
                f"INSERT INTO token_market ({', '.join(MARKET_COLS)}, first_enriched_ts, tracking) "
                f"VALUES ({', '.join('?' * len(MARKET_COLS))}, ?, ?) ON CONFLICT(mint) DO UPDATE SET {sets}{track_sql}",
                vals + [m.updated_ts, 1 if tracking else 0])


    def get_market(self, mint: str) -> MarketData | None:
        r = self.con.execute(f"SELECT {', '.join(MARKET_COLS)} FROM token_market WHERE mint=?", (mint,)).fetchone()
        if not r:
            return None
        names = {f.name for f in fields(MarketData)}
        return MarketData(**{k: r[k] for k in r.keys() if k in names and r[k] is not None})


    def set_tracking(self, mint: str, tracking: bool, reason: str = "") -> None:
        with self.con:
            self.con.execute("UPDATE token_market SET tracking=?, stop_reason=?, stopped_ts=? WHERE mint=?",
                             (1 if tracking else 0, reason or None, None if tracking else time.time(), mint))


    def tracked_mints(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT mint FROM token_market WHERE tracking=1")]


    def add_snapshots(self, rows: list[tuple]) -> None:
        """rows: (mint, ts, price_usd, market_cap, liquidity_usd, vol_m5, vol_h24, buys_h24, sells_h24, source)"""
        if rows:
            with self.con:
                self.con.executemany("INSERT INTO price_snapshots (mint, ts, price_usd, market_cap, liquidity_usd, vol_m5,"
                                     " vol_h24, buys_h24, sells_h24, source) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


    def upsert_candles(self, candles: list[Candle]) -> None:
        if candles:
            with self.con:
                self.con.executemany(
                    "INSERT OR REPLACE INTO candles_1m (mint, ts, open, high, low, close, volume_usd, buys, sells,"
                    " n_snapshots, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(c.mint, c.ts, c.open, c.high, c.low, c.close, c.volume_usd, c.buys, c.sells, c.n_snapshots,
                      c.source) for c in candles])


    def get_candles(self, mint: str, since_ts: float = 0, source: str = "local") -> list[Candle]:
        rows = self.con.execute("SELECT * FROM candles_1m WHERE mint=? AND ts>=? AND source=? ORDER BY ts",
                                (mint, since_ts, source)).fetchall()
        return [Candle(**dict(r)) for r in rows]


    def add_drop(self, mint: str, reason: str, detail: str = "") -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO collector_drops (mint, reason, ts, detail) VALUES (?,?,?,?)",
                             (mint, reason, time.time(), detail[:300]))


    def cleanup(self, snapshot_retention_h: float, candle_retention_h: float, market_retention_d: float = 7,
                 seen_retention_d: float = 14, now: float | None = None) -> dict:
        now = now or time.time()
        out = {}
        with self.con:
            out["snapshots"] = self.con.execute("DELETE FROM price_snapshots WHERE ts < ?",
                                                (now - snapshot_retention_h * 3600,)).rowcount
            out["candles"] = self.con.execute("DELETE FROM candles_1m WHERE ts < ?",
                                              (now - candle_retention_h * 3600,)).rowcount
            out["market"] = self.con.execute("DELETE FROM token_market WHERE tracking=0 AND updated_ts < ?",
                                             (now - market_retention_d * 86400,)).rowcount
            out["drops"] = self.con.execute("DELETE FROM collector_drops WHERE ts < ?",
                                            (now - market_retention_d * 86400,)).rowcount
            out["seen"] = self.con.execute("DELETE FROM seen_tokens WHERE first_seen_ts < ?",
                                           (now - seen_retention_d * 86400,)).rowcount
        return out


    def table_counts(self) -> dict:
        return {t: self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("seen_tokens", "token_market", "price_snapshots", "candles_1m", "collector_drops")}




__all__ = ["Database", "MIGRATIONS", "SCHEMA_VERSION", "BONDING", "MIGRATED", "AMM"]
