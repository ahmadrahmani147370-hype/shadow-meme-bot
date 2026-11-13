"""v1.1 - Daily cap on unique coins evaluated (Ahmed's rule: up to COINS_PER_DAY coins per day).

"Evaluated" = admitted to the expensive part of the pipeline: safety check (RugCheck + RPC + quote) and
strategy scoring for entries. Scanning (seeing new launches, cheap DexScreener market batches) is not
counted, so a launch can still be watched until it crosses EVAL_MIN_MCAP_USD. Coins with an open paper
position or an open watch are always allowed (exits must never be blocked). The day rolls over at 00:00 in
TIMEZONE (PKT). Admissions survive restarts (table `coin_day`).
"""
from __future__ import annotations

import time
from collections import Counter

from .db import Database
from .timeutil import day_key, day_start


class DailyCap:
    def __init__(self, db: Database, cap: int, clock=time.time):
        self.db, self.cap, self.clock = db, int(cap), clock
        self.stats: Counter = Counter()
        self._day: str | None = None
        self._today: set[str] = set()
        with db.tx():
            db.con.execute("CREATE TABLE IF NOT EXISTS coin_day (day TEXT NOT NULL, mint TEXT NOT NULL, ts REAL, "
                           "PRIMARY KEY (day, mint))")
        self._roll()

    def _roll(self) -> None:
        now = self.clock()
        d = day_key(day_start(now))
        if d == self._day:
            return
        self._day = d
        self._today = {r[0] for r in self.db.con.execute("SELECT mint FROM coin_day WHERE day=?", (d,))}
        self.stats["refused_today"] = 0
        with self.db.tx():
            self.db.con.execute("DELETE FROM coin_day WHERE ts < ?", (now - 3 * 86400,))

    @property
    def used(self) -> int:
        self._roll()
        return len(self._today)

    def seen_today(self, mint: str) -> bool:
        self._roll()
        return mint in self._today

    def admit(self, mint: str) -> bool:
        """True if the coin may be evaluated today (already admitted, or a slot is free)."""
        self._roll()
        if mint in self._today:
            return True
        if self.cap > 0 and len(self._today) >= self.cap:
            self.stats["refused_today"] += 1
            self.stats["refused_total"] += 1
            return False
        self._today.add(mint)
        with self.db.tx():
            self.db.con.execute("INSERT OR IGNORE INTO coin_day (day, mint, ts) VALUES (?,?,?)",
                                (self._day, mint, self.clock()))
        self.stats["admitted_total"] += 1
        return True

    def snapshot(self) -> dict:
        return {"day": self._day, "used": self.used, "cap": self.cap,
                "refused_today": self.stats["refused_today"]}


__all__ = ["DailyCap"]
