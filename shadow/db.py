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
IMPLEMENTED = False  # partial: migrations, seen_tokens, market data, snapshots, candles, safety reports

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
    # v3 (phase 4): safety reports, cached RugCheck reports, creator blacklist
    """
    CREATE TABLE IF NOT EXISTS safety_reports (
        mint TEXT PRIMARY KEY, checked_ts REAL NOT NULL, expires_ts REAL NOT NULL, score INTEGER NOT NULL,
        verdict TEXT NOT NULL, critical_json TEXT, flags_json TEXT, data_json TEXT,
        rpc_ts REAL, rc_ts REAL, quote_ts REAL, partial INTEGER NOT NULL DEFAULT 0,
        universe TEXT NOT NULL DEFAULT 'new'
    );
    CREATE INDEX IF NOT EXISTS idx_safety_expires ON safety_reports(expires_ts);
    CREATE TABLE IF NOT EXISTS rugcheck_cache (
        mint TEXT PRIMARY KEY, ts REAL NOT NULL, report_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS creator_blacklist (
        creator TEXT PRIMARY KEY, reason TEXT NOT NULL, n_tokens INTEGER, ts REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_seen_creator ON seen_tokens(creator);
    -- universes: new launches / market cap >= $1M / top volume (see shadow.models.UNIVERSES)
    ALTER TABLE seen_tokens ADD COLUMN universe TEXT NOT NULL DEFAULT 'new';
    ALTER TABLE token_market ADD COLUMN universe TEXT NOT NULL DEFAULT 'new';
    ALTER TABLE collector_drops ADD COLUMN universe TEXT NOT NULL DEFAULT 'new';
    CREATE INDEX IF NOT EXISTS idx_market_universe ON token_market(universe, tracking);
    -- a coin can be in several lists at once (e.g. top volume AND mcap >= $1M)
    CREATE TABLE IF NOT EXISTS universe_members (
        mint TEXT NOT NULL, universe TEXT NOT NULL, first_ts REAL NOT NULL, last_ts REAL NOT NULL,
        rank INTEGER, metric REAL, source TEXT, PRIMARY KEY (mint, universe)
    );
    CREATE INDEX IF NOT EXISTS idx_universe_last ON universe_members(universe, last_ts);
    """,
    # v4 (phase 5): signals, judge verdicts, versioned strategy parameters, universe bookkeeping
    """
    -- pre-release v3 databases may lack universe_members
    CREATE TABLE IF NOT EXISTS universe_members (
        mint TEXT NOT NULL, universe TEXT NOT NULL, first_ts REAL NOT NULL, last_ts REAL NOT NULL,
        rank INTEGER, metric REAL, source TEXT, PRIMARY KEY (mint, universe)
    );
    CREATE INDEX IF NOT EXISTS idx_universe_last ON universe_members(universe, last_ts);
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, mint TEXT NOT NULL, symbol TEXT, universe TEXT NOT NULL,
        strategy TEXT, alert_type TEXT NOT NULL, score REAL, class TEXT, params_version INTEGER,
        price REAL, mcap REAL, liquidity REAL, entry REAL, sl REAL, tp REAL, time_stop_min REAL,
        detectors_json TEXT, features_json TEXT, safety_verdict TEXT, safety_score INTEGER, critical_json TEXT,
        judge_verdict TEXT, judge_confidence REAL, parent_id INTEGER, reason TEXT,
        status TEXT, exit_ts REAL, exit_price REAL, exit_reason TEXT, pnl_pct REAL, max_up_pct REAL, max_down_pct REAL
    );
    CREATE INDEX IF NOT EXISTS idx_signals_mint ON signals(mint, ts);
    CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy, ts);
    CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
    CREATE TABLE IF NOT EXISTS judge_verdicts (
        id INTEGER PRIMARY KEY, signal_id INTEGER NOT NULL, ts REAL NOT NULL, mint TEXT NOT NULL, universe TEXT,
        strategy TEXT, verdict TEXT NOT NULL, confidence REAL, reasons_json TEXT, inputs_json TEXT,
        judge_version TEXT, outcome TEXT, outcome_pnl_pct REAL, outcome_ts REAL
    );
    CREATE INDEX IF NOT EXISTS idx_judge_signal ON judge_verdicts(signal_id);
    CREATE INDEX IF NOT EXISTS idx_judge_ts ON judge_verdicts(ts);
    CREATE TABLE IF NOT EXISTS strategy_params (
        strategy TEXT NOT NULL, version INTEGER NOT NULL, created_ts REAL NOT NULL, active INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL, parent_version INTEGER, universes_json TEXT NOT NULL, params_json TEXT NOT NULL,
        note TEXT, PRIMARY KEY (strategy, version)
    );
    ALTER TABLE universe_members ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE universe_members ADD COLUMN rank_h1 INTEGER;
    ALTER TABLE universe_members ADD COLUMN mcap REAL;
    ALTER TABLE universe_members ADD COLUMN vol_h24 REAL;
    ALTER TABLE universe_members ADD COLUMN vol_h1 REAL;
    ALTER TABLE universe_members ADD COLUMN liquidity_usd REAL;
    CREATE TABLE IF NOT EXISTS universe_runs (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, candidates INTEGER, mcap_members INTEGER, volume_members INTEGER,
        filtered_json TEXT, calls INTEGER
    );
    """,
]

MARKET_COLS = ("mint", "symbol", "name", "pair_address", "dex_id", "quote_symbol", "price_usd", "price_native",
               "market_cap", "fdv", "liquidity_usd", "liquidity_source", "vol_m5", "vol_h1", "vol_h6", "vol_h24",
               "buys_m5", "sells_m5", "buys_h1", "sells_h1", "buys_h6", "sells_h6", "buys_h24", "sells_h24",
               "price_change_m5", "price_change_h1", "pair_created_ts", "launch_ts", "migration_status",
               "n_pairs", "source", "updated_ts", "universe")

SCHEMA_VERSION = len(MIGRATIONS)


def _market_sets() -> str:
    """ON CONFLICT update. The universe is never downgraded to 'new' by the new-launch collector:
    a refresh with universe='new' keeps whatever universe the row already has (the overwrite fix);
    the universe collector (mcap/volume) sets its own value."""
    parts = [f"{c}=excluded.{c}" for c in MARKET_COLS if c not in ("mint", "universe")]
    parts.append("universe=CASE WHEN excluded.universe='new' THEN token_market.universe ELSE excluded.universe END")
    return ", ".join(parts)


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
            # one transaction per step: a failing step leaves the database untouched at the old version
            try:
                self.con.executescript(f"BEGIN;\n{MIGRATIONS[i]}\nPRAGMA user_version = {i + 1};\nCOMMIT;")
            except Exception:
                if self.con.in_transaction:
                    self.con.execute("ROLLBACK")
                raise
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
        sets = _market_sets()
        track_sql = "" if tracking is None else f", tracking={1 if tracking else 0}"
        with self.con:
            self.con.execute(
                f"INSERT INTO token_market ({', '.join(MARKET_COLS)}, first_enriched_ts, tracking) "
                f"VALUES ({', '.join('?' * len(MARKET_COLS))}, ?, ?) ON CONFLICT(mint) DO UPDATE SET {sets}{track_sql}",
                vals + [m.updated_ts, 1 if tracking else 0])


    def upsert_markets(self, ms: list[MarketData], tracking: bool | None = None) -> None:
        """Many rows in one transaction (200 tracked tokens every 15 s on a phone)."""
        if not ms:
            return
        sets = _market_sets()
        track_sql = "" if tracking is None else f", tracking={1 if tracking else 0}"
        rows = []
        for m in ms:
            d = asdict(m)
            rows.append([d[c] for c in MARKET_COLS] + [m.updated_ts, 1 if tracking else 0])
        with self.con:
            self.con.executemany(
                f"INSERT INTO token_market ({', '.join(MARKET_COLS)}, first_enriched_ts, tracking) "
                f"VALUES ({', '.join('?' * len(MARKET_COLS))}, ?, ?) ON CONFLICT(mint) DO UPDATE SET {sets}{track_sql}",
                rows)

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


    def add_drop(self, mint: str, reason: str, detail: str = "", universe: str = "new") -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO collector_drops (mint, reason, ts, detail, universe) "
                             "VALUES (?,?,?,?,?)", (mint, reason, time.time(), detail[:300], universe))


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
            out["safety"] = self.con.execute(
                "DELETE FROM safety_reports WHERE checked_ts < ? AND mint NOT IN "
                "(SELECT mint FROM token_market WHERE tracking=1)", (now - snapshot_retention_h * 3600,)).rowcount
            out["rc_cache"] = self.con.execute("DELETE FROM rugcheck_cache WHERE ts < ?", (now - 3600,)).rowcount
            out["signals"] = self.con.execute("DELETE FROM signals WHERE ts < ? AND COALESCE(status, '') != 'open'",
                                              (now - 30 * 86400,)).rowcount
            self.con.execute("DELETE FROM judge_verdicts WHERE ts < ?", (now - 30 * 86400,))
            self.con.execute("DELETE FROM universe_runs WHERE ts < ?", (now - 7 * 86400,))
            out["blacklist"] = self.con.execute("DELETE FROM creator_blacklist WHERE ts < ?",
                                                (now - 30 * 86400,)).rowcount
            self.con.execute("UPDATE seen_tokens SET raw_json=NULL WHERE raw_json IS NOT NULL AND first_seen_ts < ?",
                             (now - 6 * 3600,))
            out["seen"] = self.con.execute("DELETE FROM seen_tokens WHERE first_seen_ts < ?",
                                           (now - seen_retention_d * 86400,)).rowcount
        return out


    def table_counts(self) -> dict:
        return {t: self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("seen_tokens", "token_market", "price_snapshots", "candles_1m", "collector_drops",
                          "safety_reports", "rugcheck_cache", "creator_blacklist", "universe_members", "signals",
                          "judge_verdicts", "strategy_params", "universe_runs")}

    # ---------------------------------------------------------------- phase 4 tables
    def save_safety(self, row: dict) -> None:
        cols = ("mint", "checked_ts", "expires_ts", "score", "verdict", "critical_json", "flags_json", "data_json",
                "rpc_ts", "rc_ts", "quote_ts", "partial", "universe")
        with self.con:
            self.con.execute(f"INSERT OR REPLACE INTO safety_reports ({', '.join(cols)}) VALUES "
                             f"({', '.join('?' * len(cols))})", [row.get(c) for c in cols])

    def get_safety(self, mint: str) -> dict | None:
        r = self.con.execute("SELECT * FROM safety_reports WHERE mint=?", (mint,)).fetchone()
        return dict(r) if r else None

    def put_rugcheck(self, mint: str, report: dict, ts: float | None = None) -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO rugcheck_cache (mint, ts, report_json) VALUES (?,?,?)",
                             (mint, ts or time.time(), json.dumps(report)))

    def get_rugcheck(self, mint: str, max_age_sec: float, now: float | None = None) -> dict | None:
        r = self.con.execute("SELECT ts, report_json FROM rugcheck_cache WHERE mint=?", (mint,)).fetchone()
        if not r or (now or time.time()) - r[0] > max_age_sec:
            return None
        return json.loads(r[1])

    def creator_token_count(self, creator: str, since_ts: float) -> int:
        if not creator:
            return 0
        return self.con.execute("SELECT COUNT(*) FROM seen_tokens WHERE creator=? AND first_seen_ts>=?",
                                (creator, since_ts)).fetchone()[0]

    def blacklist_creator(self, creator: str, reason: str, n_tokens: int | None = None) -> None:
        if creator:
            with self.con:
                self.con.execute("INSERT OR REPLACE INTO creator_blacklist (creator, reason, n_tokens, ts) "
                                 "VALUES (?,?,?,?)", (creator, reason[:200], n_tokens, time.time()))

    def creator_blacklisted(self, creator: str) -> dict | None:
        if not creator:
            return None
        r = self.con.execute("SELECT * FROM creator_blacklist WHERE creator=?", (creator,)).fetchone()
        return dict(r) if r else None

    def upsert_universe_member(self, mint: str, universe: str, rank: int | None = None, metric: float | None = None,
                               source: str = "", now: float | None = None, rank_h1: int | None = None,
                               mcap: float | None = None, vol_h24: float | None = None, vol_h1: float | None = None,
                               liquidity_usd: float | None = None) -> None:
        now = now or time.time()
        with self.con:
            self.con.execute(
                "INSERT INTO universe_members (mint, universe, first_ts, last_ts, rank, metric, source, active, rank_h1,"
                " mcap, vol_h24, vol_h1, liquidity_usd) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?) "
                "ON CONFLICT(mint, universe) DO UPDATE SET last_ts=excluded.last_ts, rank=excluded.rank, "
                "metric=excluded.metric, source=excluded.source, active=1, rank_h1=excluded.rank_h1, "
                "mcap=excluded.mcap, vol_h24=excluded.vol_h24, vol_h1=excluded.vol_h1, liquidity_usd=excluded.liquidity_usd",
                (mint, universe, now, now, rank, metric, source, rank_h1, mcap, vol_h24, vol_h1, liquidity_usd))

    def deactivate_universe_members(self, universe: str, keep: set) -> int:
        rows = [r[0] for r in self.con.execute("SELECT mint FROM universe_members WHERE universe=? AND active=1",
                                               (universe,)) if r[0] not in keep]
        with self.con:
            self.con.executemany("UPDATE universe_members SET active=0 WHERE mint=? AND universe=?",
                                 [(m, universe) for m in rows])
        return len(rows)

    def universe_mints(self, universe: str, since_ts: float = 0, active_only: bool = True) -> list[str]:
        q = "SELECT mint FROM universe_members WHERE universe=? AND last_ts>=?" + (" AND active=1" if active_only else "")
        return [r[0] for r in self.con.execute(q + " ORDER BY rank", (universe, since_ts))]

    def universes_of(self, mint: str, active_only: bool = True) -> list[str]:
        q = "SELECT universe FROM universe_members WHERE mint=?" + (" AND active=1" if active_only else "")
        return [r[0] for r in self.con.execute(q, (mint,))]

    def add_universe_run(self, row: dict) -> None:
        with self.con:
            self.con.execute("INSERT INTO universe_runs (ts, candidates, mcap_members, volume_members, filtered_json, calls)"
                             " VALUES (?,?,?,?,?,?)", (row["ts"], row.get("candidates"), row.get("mcap_members"),
                                                       row.get("volume_members"), json.dumps(row.get("filtered") or {}),
                                                       row.get("calls")))

    # ---------------------------------------------------------------- phase 5 tables
    SIGNAL_COLS = ("ts", "mint", "symbol", "universe", "strategy", "alert_type", "score", "class", "params_version",
                   "price", "mcap", "liquidity", "entry", "sl", "tp", "time_stop_min", "detectors_json", "features_json",
                   "safety_verdict", "safety_score", "critical_json", "judge_verdict", "judge_confidence", "parent_id",
                   "reason", "status")

    def add_signal(self, row: dict) -> int:
        with self.con:
            cur = self.con.execute(f"INSERT INTO signals ({', '.join(self.SIGNAL_COLS)}) VALUES "
                                   f"({', '.join('?' * len(self.SIGNAL_COLS))})", [row.get(c) for c in self.SIGNAL_COLS])
        return cur.lastrowid

    def update_signal(self, sid: int, **kw) -> None:
        if kw:
            with self.con:
                self.con.execute(f"UPDATE signals SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", [*kw.values(), sid])

    def get_signal(self, sid: int) -> dict | None:
        r = self.con.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def open_signals(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM signals WHERE status='open' ORDER BY ts")]

    def closed_signals(self, strategy: str | None = None, since_ts: float = 0) -> list[dict]:
        q, a = "SELECT * FROM signals WHERE status='closed' AND ts>=?", [since_ts]
        if strategy:
            q, a = q + " AND strategy=?", a + [strategy]
        return [dict(r) for r in self.con.execute(q + " ORDER BY exit_ts", a)]

    def add_judge_verdict(self, row: dict) -> int:
        cols = ("signal_id", "ts", "mint", "universe", "strategy", "verdict", "confidence", "reasons_json",
                "inputs_json", "judge_version")
        with self.con:
            cur = self.con.execute(f"INSERT INTO judge_verdicts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                   [row.get(c) for c in cols])
        return cur.lastrowid

    def set_judge_outcome(self, signal_id: int, outcome: str, pnl_pct: float | None, ts: float) -> None:
        with self.con:
            self.con.execute("UPDATE judge_verdicts SET outcome=?, outcome_pnl_pct=?, outcome_ts=? WHERE signal_id=?",
                             (outcome, pnl_pct, ts, signal_id))

    def judge_history(self, since_ts: float = 0) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT verdict, outcome, outcome_pnl_pct, strategy, universe FROM judge_verdicts WHERE ts>=? "
            "AND outcome IS NOT NULL", (since_ts,))]

    def save_strategy_params(self, strategy: str, version: int, params: dict, universes: list, source: str,
                             parent_version: int | None = None, note: str = "", active: bool = True) -> None:
        with self.con:
            if active:
                self.con.execute("UPDATE strategy_params SET active=0 WHERE strategy=?", (strategy,))
            self.con.execute("INSERT OR REPLACE INTO strategy_params (strategy, version, created_ts, active, source, "
                             "parent_version, universes_json, params_json, note) VALUES (?,?,?,?,?,?,?,?,?)",
                             (strategy, version, time.time(), 1 if active else 0, source, parent_version,
                              json.dumps(universes), json.dumps(params), note))

    def active_strategy_params(self, strategy: str) -> dict | None:
        r = self.con.execute("SELECT * FROM strategy_params WHERE strategy=? AND active=1 ORDER BY version DESC LIMIT 1",
                             (strategy,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["params"], d["universes"] = json.loads(d.pop("params_json")), json.loads(d.pop("universes_json"))
        return d

    def strategy_param_versions(self, strategy: str) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT strategy, version, created_ts, active, source, parent_version, note FROM strategy_params "
            "WHERE strategy=? ORDER BY version", (strategy,))]

    def snapshot_extremes(self, mint: str) -> dict | None:
        """First/peak/latest price and liquidity from retained snapshots (rug-pull warning)."""
        r = self.con.execute(
            "SELECT MAX(price_usd) AS peak, COUNT(*) AS n, MAX(liquidity_usd) AS liq_peak FROM price_snapshots "
            "WHERE mint=?", (mint,)).fetchone()
        if not r or not r["n"]:
            return None
        last = self.con.execute("SELECT price_usd, liquidity_usd FROM price_snapshots WHERE mint=? ORDER BY ts DESC "
                                "LIMIT 1", (mint,)).fetchone()
        return {"n": r["n"], "peak": r["peak"], "liq_peak": r["liq_peak"], "last": last["price_usd"],
                "liq_last": last["liquidity_usd"]}




__all__ = ["Database", "MIGRATIONS", "SCHEMA_VERSION", "BONDING", "MIGRATED", "AMM"]
