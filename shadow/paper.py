"""PHASE 6 - Paper trading engine (virtual USD only; there is no real order code anywhere).

Opens a paper trade only for a judge-APPROVED BUY WATCH with score >= MIN_TRADE_SCORE and a fresh,
sellable Jupiter quote. Defaults: $100 balance, $2 per trade, $0.50 buy fee + $0.50 sell fee, max 5
open, one position per coin. PAPER_DAILY_TARGET (50) is a target, never a quota: nothing is forced.

Slippage comes from the quote: entry and exit price move against us by the Jupiter price impact for
the trade size (at least PAPER_MIN_SLIPPAGE_PCT). If the coin's quote is older than
PAPER_EXIT_QUOTE_MAX_AGE_SEC at exit, the conservative PAPER_SLIPPAGE_PCT is used instead.
Exits: stop loss, take profit, trailing stop (after +trail_activate_pct, trail_pct below the peak),
time stop, SELL signals (dump warning / exhaustion), or no price for PAPER_NO_PRICE_CLOSE_MIN.
Cash, positions and equity snapshots live in SQLite, so everything survives a restart.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque

from . import PAPER_ONLY
from .config import Settings
from .db import Database
from .pricing import amm_out, capped_value, drained, implausible_jump
from .timeutil import day_start as _tz_day_start

PHASE = 6
IMPLEMENTED = True
log = logging.getLogger("shadow.paper")
DS_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"


def day_start(ts: float) -> float:
    """Midnight in the configured TIMEZONE (PKT by default), independent of the phone's own TZ: the 50-trade
    daily counter rolls over at 00:00 PKT."""
    return _tz_day_start(ts)


class PaperEngine:
    def __init__(self, settings: Settings, db: Database, *, src=None, checker=None, clock=time.time,
                 limiters=None, client=None, on_trade=None):
        assert PAPER_ONLY and settings.paper_only, "paper trading only"
        self.s, self.db, self.src, self.checker, self.clock = settings, db, src, checker, clock
        self.limiters, self._client = limiters, client
        self.on_trade = on_trade
        self.stats: Counter = Counter()
        self.skips: Counter = Counter()
        self.errors: deque = deque(maxlen=50)
        self._last_equity = 0.0
        self._last_fetch = 0.0
        self.account()

    # ---------------------------------------------------------------- account
    def account(self) -> dict:
        r = self.db.con.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if r is None:
            now = self.clock()
            with self.db.tx():
                self.db.con.execute("INSERT INTO paper_account (id, start_balance, cash, created_ts, updated_ts) "
                                    "VALUES (1,?,?,?,?)", (self.s.paper_start_balance, self.s.paper_start_balance, now, now))
            r = self.db.con.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        return dict(r)

    def _cash_add(self, delta: float, fee: float = 0.0, pnl: float | None = None) -> None:
        with self.db.tx():
            self.db.con.execute("UPDATE paper_account SET cash=cash+?, fees_paid=fees_paid+?, "
                                "realized_pnl=realized_pnl+?, updated_ts=? WHERE id=1",
                                (delta, fee, pnl or 0.0, self.clock()))

    def open_positions(self) -> list[dict]:
        return [dict(r) for r in self.db.con.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY open_ts")]

    def closed_positions(self, since_ts: float = 0, limit: int | None = None) -> list[dict]:
        q = "SELECT * FROM paper_positions WHERE status='closed' AND exit_ts>=? ORDER BY exit_ts DESC"
        rows = [dict(r) for r in self.db.con.execute(q + (f" LIMIT {int(limit)}" if limit else ""), (since_ts,))]
        return rows

    def trades_today(self, now: float | None = None) -> int:
        return self.db.con.execute("SELECT COUNT(*) FROM paper_positions WHERE open_ts>=?",
                                   (day_start(now or self.clock()),)).fetchone()[0]

    def equity(self) -> dict:
        acc = self.account()
        opens = self.open_positions()
        value = sum((p["qty"] or 0) * (p["last_price"] or p["market_price"] or 0) for p in opens)
        return {"cash": acc["cash"], "open_value": value, "equity": acc["cash"] + value, "n_open": len(opens),
                "realized_pnl": acc["realized_pnl"], "fees_paid": acc["fees_paid"], "start_balance": acc["start_balance"]}

    # ---------------------------------------------------------------- opening
    def can_open(self, row: dict, report) -> tuple[bool, str]:
        s, now = self.s, self.clock()
        if row.get("alert_type") != "BUY_WATCH" or row.get("judge_verdict") != "APPROVE":
            return False, "not an approved BUY WATCH"
        if getattr(self, "paused", False):
            return False, "paused (/pause)"
        if (row.get("score") or 0) < s.min_trade_score:
            return False, f"score {row.get('score')} < MIN_TRADE_SCORE {s.min_trade_score}"
        q = (report.data.get("quote") if report else None) or {}
        if not report or q.get("sellable") is not True or not report.quote_fresh(s.safety_quote_ttl_sec, now):
            return False, "no fresh sellable Jupiter quote"
        opens = self.open_positions()
        if len(opens) >= s.paper_max_open:
            return False, f"max {s.paper_max_open} open positions"
        if any(p["mint"] == row["mint"] for p in opens):
            return False, "already holding this coin"
        need = s.paper_trade_size + s.paper_buy_fee
        if self.account()["cash"] < need:
            return False, f"cash below ${need:.2f}"
        return True, "ok"

    def open_from_signal(self, row: dict, report, params: dict) -> int | None:
        ok, why = self.can_open(row, report)
        if not ok:
            self.skips[why.split(" ")[0] if why.startswith("score") else why] += 1
            return None
        s, now = self.s, self.clock()
        q = report.data.get("quote") or {}
        slip = max(float(q.get("impact_pct") or 0.0), s.paper_min_slippage_pct)
        market = float(row["price"] or row["entry"])
        entry = market * (1 + slip / 100)
        qty = s.paper_trade_size / entry
        cur = None
        with self.db.tx():
            cur = self.db.con.execute(
                "INSERT INTO paper_positions (signal_id, mint, symbol, universe, strategy, params_version, status, open_ts,"
                " market_price, entry_price, size_usd, qty, buy_fee, entry_slippage_pct, sl, tp, trail_pct,"
                " trail_activate_pct, peak_price, time_stop_min, judge_confidence, score, last_price, last_price_ts,"
                " max_up_pct, max_down_pct) VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
                (row.get("id"), row["mint"], row.get("symbol"), row.get("universe"), row.get("strategy"),
                 row.get("params_version"), now, market, entry, s.paper_trade_size, qty, s.paper_buy_fee, slip,
                 row.get("sl"), row.get("tp"), params.get("trail_pct"), params.get("trail_activate_pct"), market,
                 row.get("time_stop_min"), row.get("judge_confidence"), row.get("score"), market, now))
            # same transaction as the position row: a kill -9 can never leave a position without its cash debit
            self._cash_add(-(s.paper_trade_size + s.paper_buy_fee), fee=s.paper_buy_fee, pnl=-s.paper_buy_fee)
        pid = cur.lastrowid
        self.stats["opened"] += 1
        pos = self.get(pid)
        if self.on_trade:
            self.on_trade("OPEN", pos)
        return pid

    def get(self, pid: int) -> dict | None:
        r = self.db.con.execute("SELECT * FROM paper_positions WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------------- closing
    def _exit_slippage(self, mint: str, now: float) -> float:
        rep = self.checker.get_report(mint) if self.checker else None
        q = (rep.data.get("quote") if rep else None) or {}
        if rep and q.get("sellable") and rep.quote_ts and now - rep.quote_ts <= self.s.paper_exit_quote_max_age_sec:
            return max(float(q.get("impact_pct") or 0.0), self.s.paper_min_slippage_pct)
        return self.s.paper_slippage_pct

    def close(self, pos: dict, market_price: float | None, reason: str, liquidity_usd: float | None = None) -> dict:
        s, now = self.s, self.clock()
        market_price = market_price or pos["last_price"] or pos["market_price"]
        slip = self._exit_slippage(pos["mint"], now)
        exit_price = market_price * (1 - slip / 100)
        if reason == "liquidity_gone":  # drained pool: only its remaining quote side can be pulled out
            proceeds = amm_out(pos["qty"] * exit_price, liquidity_usd)
        else:
            proceeds = capped_value(pos["qty"] * exit_price, liquidity_usd)  # can't sell more than the pool holds
        if pos["qty"]:
            exit_price = proceeds / pos["qty"]
        # P&L includes both fees; the buy fee was already booked at open, so only proceeds - size - sell fee here
        pnl = proceeds - pos["size_usd"] - pos["buy_fee"] - s.paper_sell_fee
        with self.db.tx():
            cur = self.db.con.execute(
                "UPDATE paper_positions SET status='closed', exit_ts=?, exit_market_price=?, exit_price=?, sell_fee=?,"
                " exit_slippage_pct=?, exit_reason=?, proceeds=?, pnl_usd=?, pnl_pct=? WHERE id=? AND status='open'",
                (now, market_price, exit_price, s.paper_sell_fee, slip, reason, proceeds, pnl,
                 pnl / pos["size_usd"] * 100, pos["id"]))
            # credit cash only if this call really closed it (no double credit), in the same transaction
            if cur.rowcount == 1:
                self._cash_add(proceeds - s.paper_sell_fee, fee=s.paper_sell_fee,
                               pnl=proceeds - pos["size_usd"] - s.paper_sell_fee)
        self.stats["closed"] += 1
        self.stats[f"exit_{reason}"] += 1
        out = self.get(pos["id"])
        if self.on_trade:
            self.on_trade("CLOSE", out)
        return out

    def close_by_signal(self, signal_id: int, reason: str) -> dict | None:
        for p in self.open_positions():
            if p["signal_id"] == signal_id:
                md = self.src.get_market(p["mint"]) if self.src else None
                price = md.price_usd if md and md.price_usd else None
                if implausible_jump(p["last_price"] or p["market_price"], price, self.s.paper_max_price_jump):
                    price = None  # bad tick: last trusted price instead
                liq = md.liquidity_usd if md and md.liquidity_source in (None, "", "dexscreener") else None
                return self.close(p, price, f"sell_signal_{reason}", liquidity_usd=liq)
        return None

    def exit_reason(self, p: dict, price: float, now: float) -> str | None:
        if p["sl"] and price <= p["sl"]:
            return "stop_loss"
        if p["tp"] and price >= p["tp"]:
            return "take_profit"
        up_peak = (p["peak_price"] / p["market_price"] - 1) * 100 if p["market_price"] else 0
        if p["trail_pct"] and p["trail_activate_pct"] is not None and up_peak >= p["trail_activate_pct"] \
                and price <= p["peak_price"] * (1 - p["trail_pct"] / 100):
            return "trailing_stop"
        if p["time_stop_min"] and now - p["open_ts"] >= p["time_stop_min"] * 60:
            return "time_stop"
        return None

    # ---------------------------------------------------------------- loop step
    async def _fetch_prices(self, mints: list[str]) -> dict[str, tuple]:
        """Positions whose coin is no longer tracked: one DexScreener batch call (max every 30 s)."""
        if not mints or self._client is None or self.limiters is None or self.clock() - self._last_fetch < 30:
            return {}
        self._last_fetch = self.clock()
        from .collector import group_pairs, market_from_pairs, standard_quote
        from .net import fetch_json
        try:
            pairs = await fetch_json(self._client, self.limiters["dexscreener"], DS_TOKENS + ",".join(mints[:30]))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.errors.append(f"{time.strftime('%H:%M:%S')} price fetch: {type(e).__name__}: {e}")
            return {}
        out = {}
        for m, ps in group_pairs(pairs or [], mints).items():
            good = [x for x in ps if standard_quote(x)]
            if good:
                md = market_from_pairs(m, good, self.clock())
                if md.price_usd:
                    out[m] = (md.price_usd, md.liquidity_usd, md.liquidity_source or "dexscreener")
        self.stats["price_fetches"] += 1
        return out

    async def manage(self) -> int:
        now = self.clock()
        opens = self.open_positions()
        prices: dict[str, tuple] = {}
        missing = []
        for p in opens:
            md = self.src.get_market(p["mint"]) if self.src else None
            if md and md.price_usd and (not md.updated_ts or now - md.updated_ts < 300):
                prices[p["mint"]] = (md.price_usd, md.liquidity_usd, md.liquidity_source)
            else:
                missing.append(p["mint"])
        prices.update(await self._fetch_prices(missing))
        closed = 0
        for p in opens:
            price, liq, liq_src = prices.get(p["mint"]) or (None, None, None)
            if liq_src not in (None, "", "dexscreener"):
                liq = None  # estimated liquidity: no rug rule, no cap
            if price and drained(liq, self.s.paper_rug_liquidity_usd):
                self.stats["rug_exits"] += 1
                # value at the last trusted price (the drained pool's own "price" is dust math), capped by liquidity
                self.close(p, p["last_price"] or p["market_price"], "liquidity_gone", liquidity_usd=liq)
                closed += 1
                continue
            if price and implausible_jump(p["last_price"] or p["market_price"], price, self.s.paper_max_price_jump):
                self.stats["price_spikes_ignored"] += 1
                price = None
            if not price:
                if now - (p["last_price_ts"] or p["open_ts"]) > self.s.paper_no_price_close_min * 60:
                    self.close(p, None, "no_price")
                    closed += 1
                continue
            peak = max(p["peak_price"] or price, price)
            chg = (price / p["market_price"] - 1) * 100
            with self.db.tx():
                self.db.con.execute("UPDATE paper_positions SET last_price=?, last_price_ts=?, peak_price=?, "
                                    "max_up_pct=MAX(COALESCE(max_up_pct,0),?), max_down_pct=MIN(COALESCE(max_down_pct,0),?)"
                                    " WHERE id=?", (price, now, peak, chg, chg, p["id"]))
            p.update(last_price=price, peak_price=peak)
            reason = self.exit_reason(p, price, now)
            if reason:
                self.close(p, price, reason, liquidity_usd=liq)
                closed += 1
        if now - self._last_equity >= self.s.paper_equity_every_sec:
            self._last_equity = now
            e = self.equity()
            with self.db.tx():
                self.db.con.execute("INSERT OR REPLACE INTO paper_equity (ts, cash, open_value, equity, n_open, realized_pnl)"
                                    " VALUES (?,?,?,?,?,?)", (now, e["cash"], e["open_value"], e["equity"], e["n_open"],
                                                             e["realized_pnl"]))
        return closed


def not_ready():  # kept for older imports; phase 6 is implemented
    return None


__all__ = ["PaperEngine", "day_start"]
