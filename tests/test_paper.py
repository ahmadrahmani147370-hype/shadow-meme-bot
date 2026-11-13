"""Phase 6 paper engine: accounting with fees + quote slippage, limits, exits, restart survival."""
import asyncio

import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.models import MarketData
from shadow.paper import PaperEngine
from shadow.safety import PASS, SafetyReport

NOW = 1_800_000_000.0


class Src:
    def __init__(self):
        self.mk = {}

    def get_market(self, m):
        return self.mk.get(m)


class Chk:
    def __init__(self):
        self.reps = {}

    def get_report(self, m):
        return self.reps.get(m)


def rep(mint, impact=1.2, age=10, now=NOW):
    return SafetyReport(mint, now - 30, now + 900, 90, PASS, [], [], {"quote": {"sellable": True, "impact_pct": impact}},
                        quote_ts=now - age)


def sig(mint, sid=1, price=1.0, score=75, verdict="APPROVE", sl=0.88, tp=1.30, ts_min=20, strategy="early_entry"):
    return {"id": sid, "mint": mint, "symbol": mint[:4], "universe": "new", "strategy": strategy, "params_version": 1,
            "alert_type": "BUY_WATCH", "judge_verdict": verdict, "judge_confidence": 0.7, "score": score, "price": price,
            "entry": price, "sl": sl, "tp": tp, "time_stop_min": ts_min}


PARAMS = {"trail_pct": 10.0, "trail_activate_pct": 15.0}


def make(tmp_path, **env):
    s = load_settings(env={k.upper(): str(v) for k, v in env.items()})
    db = Database(str(tmp_path / "p.db"))
    clock = {"t": NOW}
    src, chk = Src(), Chk()
    pe = PaperEngine(s, db, src=src, checker=chk, clock=lambda: clock["t"])
    return s, db, pe, src, chk, clock


def test_default_has_no_fees_only_quote_slippage(tmp_path):
    s, db, pe, src, chk, clock = make(tmp_path)
    assert s.paper_buy_fee == 0 and s.paper_sell_fee == 0
    m = "Z" * 44
    chk.reps[m] = rep(m, impact=1.2)
    pid = pe.open_from_signal(sig(m), chk.reps[m], PARAMS)
    assert pe.account()["cash"] == pytest.approx(98.0) and pe.account()["fees_paid"] == 0
    clock["t"] = NOW + 60
    chk.reps[m] = rep(m, impact=2.0, now=clock["t"])
    src.mk[m] = MarketData(m, price_usd=1.35, updated_ts=NOW + 60)
    asyncio.run(pe.manage())
    p = pe.get(pid)
    assert p["pnl_usd"] == pytest.approx(2 / 1.012 * 1.35 * 0.98 - 2) and p["buy_fee"] == 0 and p["sell_fee"] == 0
    assert pe.account()["cash"] == pytest.approx(100 + p["pnl_usd"])


def test_open_close_accounting_with_fees_and_slippage(tmp_path):
    s, db, pe, src, chk, clock = make(tmp_path, paper_buy_fee=0.5, paper_sell_fee=0.5)  # fees still configurable
    m = "A" * 44
    chk.reps[m] = rep(m, impact=1.2)
    pid = pe.open_from_signal(sig(m), chk.reps[m], PARAMS)
    p = pe.get(pid)
    assert p["entry_price"] == pytest.approx(1.012) and p["entry_slippage_pct"] == pytest.approx(1.2)
    assert p["qty"] == pytest.approx(2 / 1.012)
    acc = pe.account()
    assert acc["cash"] == pytest.approx(100 - 2 - 0.5) and acc["fees_paid"] == pytest.approx(0.5)
    # price hits TP -> exit with the quote's impact as exit slippage, both fees in P&L
    src.mk[m] = MarketData(m, price_usd=1.35, updated_ts=NOW + 60)
    clock["t"] = NOW + 60
    chk.reps[m] = rep(m, impact=2.0, now=clock["t"])
    asyncio.run(pe.manage())
    p = pe.get(pid)
    assert p["status"] == "closed" and p["exit_reason"] == "take_profit"
    exit_price = 1.35 * 0.98
    proceeds = 2 / 1.012 * exit_price
    assert p["pnl_usd"] == pytest.approx(proceeds - 2 - 0.5 - 0.5)
    acc = pe.account()
    assert acc["cash"] == pytest.approx(100 + p["pnl_usd"])
    assert acc["realized_pnl"] == pytest.approx(p["pnl_usd"]) and acc["fees_paid"] == pytest.approx(1.0)


def test_only_approved_fresh_quote_limits(tmp_path):
    s, db, pe, src, chk, clock = make(tmp_path, paper_max_open=2)
    m = "B" * 44
    assert pe.open_from_signal(sig(m, verdict="VETO"), rep(m), PARAMS) is None
    assert pe.open_from_signal(sig(m, score=60), rep(m), PARAMS) is None
    assert pe.open_from_signal(sig(m), rep(m, age=999), PARAMS) is None            # stale quote
    assert pe.open_from_signal(sig(m), rep(m), PARAMS)
    assert pe.open_from_signal(sig(m, sid=2), rep(m), PARAMS) is None               # same coin
    assert pe.open_from_signal(sig("C" * 44, sid=3), rep("C" * 44), PARAMS)
    assert pe.open_from_signal(sig("D" * 44, sid=4), rep("D" * 44), PARAMS) is None  # max open 2
    assert sum(pe.skips.values()) == 5 and pe.trades_today() == 2


def test_cash_limit(tmp_path):
    s, db, pe, src, chk, clock = make(tmp_path, paper_start_balance=3, paper_max_open=5)
    assert pe.open_from_signal(sig("E" * 44), rep("E" * 44), PARAMS)
    assert pe.open_from_signal(sig("F" * 44, sid=2), rep("F" * 44), PARAMS) is None


@pytest.mark.parametrize("path,reason", [
    ([0.80], "stop_loss"),
    ([1.20, 1.10, 1.05], "trailing_stop"),     # +20% activates the 10% trail, 1.05 < 1.20*0.9
])
def test_exits(tmp_path, path, reason):
    s, db, pe, src, chk, clock = make(tmp_path)
    m = "G" * 44
    pid = pe.open_from_signal(sig(m), rep(m), PARAMS)
    for i, px in enumerate(path):
        clock["t"] = NOW + 30 * (i + 1)
        src.mk[m] = MarketData(m, price_usd=px, updated_ts=clock["t"])
        asyncio.run(pe.manage())
    p = pe.get(pid)
    assert p["status"] == "closed" and p["exit_reason"] == reason
    assert p["exit_slippage_pct"] == pytest.approx(s.paper_slippage_pct)   # no fresh quote at exit -> conservative 3%


def test_time_stop_sell_signal_and_no_price(tmp_path):
    s, db, pe, src, chk, clock = make(tmp_path)
    a, b, c = "H" * 44, "I" * 44, "J" * 44
    pa = pe.open_from_signal(sig(a, sid=1), rep(a), PARAMS)
    pb = pe.open_from_signal(sig(b, sid=2), rep(b), PARAMS)
    pc = pe.open_from_signal(sig(c, sid=3), rep(c), PARAMS)
    src.mk[a] = MarketData(a, price_usd=1.01, updated_ts=NOW + 1300)
    src.mk[b] = MarketData(b, price_usd=1.02, updated_ts=NOW + 1300)
    pe.close_by_signal(2, "dump_warning")
    clock["t"] = NOW + 21 * 60
    asyncio.run(pe.manage())
    assert pe.get(pa)["exit_reason"] == "time_stop"
    assert pe.get(pb)["exit_reason"] == "sell_signal_dump_warning"
    assert pe.get(pc)["status"] == "open"                 # no price yet, < 30 min
    clock["t"] = NOW + 31 * 60
    asyncio.run(pe.manage())
    assert pe.get(pc)["exit_reason"] == "no_price"


def test_survives_restart(tmp_path):
    s, db, pe, src, chk, clock = make(tmp_path)
    m = "K" * 44
    pid = pe.open_from_signal(sig(m), rep(m), PARAMS)
    cash = pe.account()["cash"]
    db.close()
    db2 = Database(str(tmp_path / "p.db"))
    pe2 = PaperEngine(s, db2, src=src, checker=chk, clock=lambda: clock["t"])
    assert pe2.account()["cash"] == pytest.approx(cash) and [p["id"] for p in pe2.open_positions()] == [pid]
    src.mk[m] = MarketData(m, price_usd=0.5, updated_ts=NOW + 30)
    clock["t"] = NOW + 30
    asyncio.run(pe2.manage())
    assert pe2.get(pid)["exit_reason"] == "stop_loss"
    e = pe2.equity()
    assert e["equity"] == pytest.approx(e["cash"]) and e["n_open"] == 0
    db2.close()


def test_paper_only_lock(tmp_path):
    import shadow
    assert shadow.PAPER_ONLY is True
    s = load_settings(env={})
    assert s.paper_only is True
    with pytest.raises(Exception):
        s.paper_only = False          # frozen settings / constant


def test_report_texts(tmp_path):
    from shadow.reports import history_text, paper_stats, paper_text, positions_text, report_text, strategies_text
    s, db, pe, src, chk, clock = make(tmp_path)
    a, b = "L" * 44, "N" * 44
    pe.open_from_signal(sig(a, sid=1), rep(a), PARAMS)
    pe.open_from_signal(sig(b, sid=2), rep(b), PARAMS)
    src.mk[a] = MarketData(a, price_usd=0.5, updated_ts=NOW + 30)
    src.mk[b] = MarketData(b, price_usd=1.1, updated_ts=NOW + 30)
    clock["t"] = NOW + 30
    asyncio.run(pe.manage())
    d = paper_stats(db, s, clock["t"])
    assert d["closed"]["n"] == 1 and d["n_open"] == 1 and d["fees_paid"] == pytest.approx(0.0)
    assert d["equity"] == pytest.approx(d["cash"] + 2 / 1.012 * 1.1)
    assert "Closed trades 1" in paper_text(db, s, clock["t"]) and "Open 1/5" in paper_text(db, s, clock["t"])
    assert "NNNN" in positions_text(db, clock["t"]) and "LLLL" in history_text(db) and "stop_loss" in history_text(db)
    assert "closed 1" in report_text(db, s, clock["t"])
    assert "early_entry v1" in strategies_text(db, s) and "judge v1" in strategies_text(db, s)


# ---- phase 7 fix: drained pools (real numbers from the 13 Nov 2026 live run, BTCCNDL)
def _btccndl(tmp_path):
    s, db, pe, src, chk, clock = make(tmp_path)
    m = "BN9e4ESnzZhFZwLi7XvC1y1xnZEDuMmTSQGiURrypump"
    chk.reps[m] = rep(m, impact=0.5)
    pid = pe.open_from_signal(sig(m, price=6.298e-05, sl=5.6988e-05, tp=8.5482e-05), chk.reps[m], PARAMS)
    return s, db, pe, src, chk, clock, m, pid


def test_drained_pool_is_a_rug_loss_not_a_take_profit(tmp_path):
    from shadow.maintenance import consistency
    s, db, pe, src, chk, clock, m, pid = _btccndl(tmp_path)
    clock["t"] = NOW + 60
    src.mk[m] = MarketData(m, price_usd=6.775e-05, liquidity_usd=0.11, liquidity_source="dexscreener", updated_ts=NOW + 60)
    asyncio.run(pe.manage())
    p = pe.get(pid)
    assert p["status"] == "closed" and p["exit_reason"] == "liquidity_gone"
    assert p["proceeds"] < 0.055 and p["pnl_usd"] < -1.94  # ~ -$2 of a $2 trade (no fees by default)
    assert consistency(db)["ok"]


def test_dust_price_after_drain_never_books_a_profit(tmp_path):
    s, db, pe, src, chk, clock, m, pid = _btccndl(tmp_path)
    clock["t"] = NOW + 60
    # the live tick: price 95.27 USD (+150,000,000%) with $9.93 liquidity
    src.mk[m] = MarketData(m, price_usd=95.27, liquidity_usd=9.93, liquidity_source="dexscreener", updated_ts=NOW + 60)
    asyncio.run(pe.manage())
    p = pe.get(pid)
    assert p["exit_reason"] == "liquidity_gone" and p["pnl_usd"] < 0
    assert p["proceeds"] <= p["qty"] * 6.298e-05 + 1e-9  # valued at the last trusted price at most


def test_implausible_jump_is_ignored_and_estimated_liquidity_is_no_rug(tmp_path):
    s, db, pe, src, chk, clock, m, pid = _btccndl(tmp_path)
    clock["t"] = NOW + 60
    src.mk[m] = MarketData(m, price_usd=6.298e-05 * 1000, liquidity_usd=50000, liquidity_source="dexscreener",
                           updated_ts=NOW + 60)
    asyncio.run(pe.manage())
    assert pe.get(pid)["status"] == "open" and pe.stats["price_spikes_ignored"] == 1
    clock["t"] = NOW + 120
    # Jupiter's liquidity fill-in for bonding-curve coins (~$100) is an estimate -> not a rug signal
    src.mk[m] = MarketData(m, price_usd=6.4e-05, liquidity_usd=94.0, liquidity_source="jupiter", updated_ts=NOW + 120)
    asyncio.run(pe.manage())
    assert pe.get(pid)["status"] == "open"


def test_pricing_helpers():
    from shadow.pricing import capped_value, drained, implausible_jump, rug_pnl_pct
    assert drained(0.11, 500) and not drained(None, 500) and not drained(94, 500, "jupiter")
    assert implausible_jump(6.775e-05, 95.27, 50) and not implausible_jump(1.0, 1.4, 50)
    assert capped_value(1000.0, 9.93) == pytest.approx(4.965) and capped_value(2.0, None) == 2.0
    from shadow.pricing import amm_out
    v = 2.0 * 6.775e-05 / 6.298e-05
    assert rug_pnl_pct(6.298e-05, 6.775e-05, 2.0, 0.11) == pytest.approx((0.055 * v / (0.055 + v) / 2 - 1) * 100)
    assert amm_out(2.0, 9.93) == pytest.approx(4.965 * 2 / 6.965) and amm_out(2.0, 1e6) == pytest.approx(2.0, rel=1e-5)
