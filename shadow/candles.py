"""Build 1-minute OHLCV candles from polled price snapshots.

Volume and buy/sell counts come from DexScreener's h24 counters. Every tracked token is younger
than MAX_TOKEN_AGE_MIN (default 120 min, far below 24 h), so for these tokens the "h24" window still
contains every trade since launch and behaves like a cumulative counter: the difference between two
snapshots is exactly the volume / buys / sells in between. If a counter ever goes down (window
rolled, or a different pair was chosen) the delta is 0 and the baseline resets.
The first snapshot only sets the baseline (trades before tracking started are not invented).
"""
from __future__ import annotations

from .models import Candle


class CandleBuilder:
    def __init__(self, mint: str, max_gap_fill: int = 10):
        self.mint = mint
        self.max_gap_fill = max_gap_fill
        self.cur: Candle | None = None
        self.last_close: float | None = None
        self._vol = self._buys = self._sells = None  # cumulative baselines

    @staticmethod
    def _delta(prev, now):
        if now is None:
            return 0, prev           # no counter in this snapshot (e.g. Jupiter): keep baseline
        if prev is None or now < prev:
            return 0, now            # first value, or counter went backwards -> reset baseline
        return now - prev, now

    def add(self, ts: float, price: float | None, vol_cum=None, buys_cum=None, sells_cum=None) -> list[Candle]:
        """Feed one snapshot. Returns candles that are now complete (older minutes)."""
        dv, self._vol = self._delta(self._vol, vol_cum)
        db, self._buys = self._delta(self._buys, buys_cum)
        ds, self._sells = self._delta(self._sells, sells_cum)
        if price is None or price <= 0:
            if self.cur:  # still count trades in the current minute
                self.cur.volume_usd += dv
                self.cur.buys += db
                self.cur.sells += ds
            return []
        bucket = int(ts // 60) * 60
        done: list[Candle] = []
        if self.cur is not None and bucket < self.cur.ts:
            return []  # out-of-order snapshot: ignore
        if self.cur is not None and bucket > self.cur.ts:
            done.append(self.cur)
            self.last_close = self.cur.close
            gap = (bucket - self.cur.ts) // 60 - 1
            if 0 < gap <= self.max_gap_fill:
                for i in range(1, gap + 1):
                    c = self.last_close
                    done.append(Candle(self.mint, self.cur.ts + 60 * i, c, c, c, c))
            self.cur = None
        if self.cur is None:
            o = self.last_close if self.last_close is not None else price
            self.cur = Candle(self.mint, bucket, o, max(o, price), min(o, price), price)
        else:
            self.cur.high = max(self.cur.high, price)
            self.cur.low = min(self.cur.low, price)
            self.cur.close = price
        self.cur.volume_usd += dv
        self.cur.buys += db
        self.cur.sells += ds
        self.cur.n_snapshots += 1
        return done

    def partial(self) -> Candle | None:
        return self.cur


def parse_gt_ohlcv(mint: str, payload: dict) -> list[Candle]:
    """GeckoTerminal /ohlcv/minute -> candles (oldest first). Rows are [ts, o, h, l, c, volume_usd]."""
    rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = []
    for r in rows:
        try:
            ts, o, h, l, c, v = r[:6]
            out.append(Candle(mint, int(ts), float(o), float(h), float(l), float(c), float(v or 0), 0, 0, 1,
                              "geckoterminal"))
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x.ts)
