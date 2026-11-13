"""v1.1 - Daily cap on unique coins evaluated (Ahmed's rule: up to COINS_PER_DAY coins per day).

"Evaluated" = admitted to the expensive part of the pipeline: safety check (RugCheck + RPC + quote) and
strategy scoring for entries. Scanning (seeing new launches, cheap DexScreener market batches) is not
counted, so a launch can still be watched until it crosses EVAL_MIN_MCAP_USD. Coins with an open paper
position or an open watch are always allowed (exits must never be blocked). The day rolls over at 00:00 in
TIMEZONE (PKT). Admissions survive restarts (table `coin_day`).
"""
from __future__ import annotations

import time
from collections import Counter, deque

from .db import Database
from .timeutil import day_key, day_start


class DailyCap:
    def __init__(self, db: Database, cap: int, clock=time.time):
        self.db, self.cap, self.clock = db, int(cap), clock
        self.stats: Counter = Counter()
        self._day: str | None = None
        self._today: set[str] = set()
        self.recent: deque = deque(maxlen=5000)       # admission times (pace / projection)
        with db.tx():
            db.con.execute("CREATE TABLE IF NOT EXISTS coin_day (day TEXT NOT NULL, mint TEXT NOT NULL, ts REAL, "
                           "PRIMARY KEY (day, mint))")
        self._roll()
        # v1.1.3: the last hour's admissions survive a restart (projection / rate)
        self.recent.extend(r[0] for r in db.con.execute(
            "SELECT ts FROM coin_day WHERE ts >= ? ORDER BY ts", (self.clock() - 3600,)))

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
        self.recent.append(self.clock())
        return True

    def snapshot(self) -> dict:
        return {"day": self._day, "used": self.used, "cap": self.cap,
                "refused_today": self.stats["refused_today"]}


class PaceController:
    """v1.1.1 - guarantee at least COINS_PER_DAY_MIN unique coins evaluated per PKT day (max COINS_PER_DAY).

    Target pace = MIN * (fraction of the PKT day elapsed). Every minute:
      * behind pace -> the EVALUATION market-cap floor steps down one tier (EVAL_PACE_FLOORS, e.g.
        750k -> 250k -> 100k -> 50k -> 25k -> 10k -> 5k; v1.1.3 added the last two: at 25k only ~16
        uncounted coins/hour were left, so the minimum could not be reached) and, at the lowest tier, discovery is widened (more GeckoTerminal
        pages / lists, DexScreener latest boosts + profiles)
      * comfortably ahead (target + 25) -> one tier back up, discovery back to normal
    Coins admitted through a lowered floor ("pace fill") are capped at PACE_FILL_PER_MIN and only while the
    count is below target, so the remaining slots up to the max stay free for coins >= $750k.
    TRADING IS NOT AFFECTED: the $1M trade floor and liquidity gates still apply to every trade.
    """

    JUMP_DEFICIT = 50       # coins behind target -> jump to the lowest floor at once

    def __init__(self, cap: DailyCap, minimum: int, floors: list[float], fill_per_min: int, clock=time.time):
        self.cap, self.clock = cap, clock
        self.min = min(int(minimum), cap.cap) if cap.cap > 0 else int(minimum)     # min can't exceed the max
        self.floors = sorted({float(f) for f in floors if f >= 0}, reverse=True) or [0.0]
        self.fill_per_min = max(1, int(fill_per_min))
        self.tier = 0
        self.widen = False
        self._last_update = 0.0
        self._fill_win: deque = deque()
        self._fills: deque = deque(maxlen=5000)
        self.stats: Counter = Counter()

    def target(self, now: float | None = None) -> float:
        now = now or self.clock()
        frac = min(1.0, max(0.0, (now - day_start(now)) / 86400.0))
        return self.min * frac

    @property
    def floor(self) -> float:
        return self.floors[self.tier]

    def behind(self, now: float | None = None) -> bool:
        return self.min > 0 and self.cap.used < self.target(now)

    def update(self, now: float | None = None) -> None:
        now = now or self.clock()
        if now - self._last_update < 60:
            return
        self._last_update = now
        tgt, used = self.target(now), self.cap.used
        if self.min > 0 and used < tgt - self.JUMP_DEFICIT and self.tier < len(self.floors) - 1:
            # v1.1.3: far behind (e.g. right after a restart, or a late start in the day): straight to the
            # lowest evaluation floor instead of one tier per minute
            self.stats["floor_jump"] += 1
            self.tier, self.widen = len(self.floors) - 1, True
        elif self.min > 0 and used < tgt:
            if self.tier < len(self.floors) - 1:
                self.tier += 1
                self.stats["floor_down"] += 1
            else:
                self.widen = True
        elif used >= tgt + 25:
            self.widen = False
            if self.tier > 0:
                self.tier -= 1
                self.stats["floor_up"] += 1

    def allow_fill(self, now: float | None = None) -> bool:
        """May one more coin be admitted through the lowered floor right now?"""
        now = now or self.clock()
        if not self.behind(now) or (self.cap.cap > 0 and self.cap.used >= self.cap.cap):
            return False
        while self._fill_win and now - self._fill_win[0] > 60:
            self._fill_win.popleft()
        if len(self._fill_win) >= self.fill_per_min:
            return False
        self._fill_win.append(now)
        self._fills.append(now)
        self.stats["fill_admitted"] += 1
        return True

    def projection(self, now: float | None = None) -> int:
        """Today's count projected to 23:59 PKT from the real admission rate of the last hour (pace fills
        included while behind - they stop at the target), bounded by the max. v1.1.3: no longer reports the
        minimum as reached just because fills are happening."""
        now = now or self.clock()
        used = self.cap.used
        fills = [t for t in self._fills if now - t <= 3600]
        allx = sum(1 for t in self.cap.recent if now - t <= 3600)
        n = allx if self.behind(now) else max(0, allx - len(fills))
        first = self.cap.recent[0] if self.cap.recent else now
        span_h = min(1.0, max(1 / 60, (now - first) / 3600.0))
        left_h = (day_start(now) + 86400 - now) / 3600.0
        proj = used + n / span_h * left_h
        return int(min(self.cap.cap, proj) if self.cap.cap > 0 else proj)

    def rate_per_h(self, now: float | None = None) -> int:
        """Coins admitted per hour over the last hour (shorter if the bot started less than an hour ago)."""
        now = now or self.clock()
        n = sum(1 for t in self.cap.recent if now - t <= 3600)
        first = self.cap.recent[0] if self.cap.recent else now
        return int(n / min(1.0, max(1 / 60, (now - first) / 3600.0)))

    def snapshot(self, now: float | None = None) -> dict:
        now = now or self.clock()
        return {"used": self.cap.used, "min": self.min, "max": self.cap.cap, "target_now": round(self.target(now)),
                "eval_floor": self.floor, "widen": self.widen, "projected_eod": self.projection(now),
                "fill_admitted": self.stats["fill_admitted"], "rate_per_h": self.rate_per_h(now)}


__all__ = ["DailyCap", "PaceController"]
