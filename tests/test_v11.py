"""v1.1: $1M trade floor + liquidity gates, daily coin cap, buy-only Telegram, shrinkage learning,
fast price lane, v1.1 strategy migration. These tests use the REAL v1.1 defaults."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from shadow.config import load_settings
from shadow.db import Database
from shadow.fastprice import FastPricer
from shadow.learning import shrink
from shadow.limits import DailyCap
from shadow.models import MarketData
from shadow.net import make_limiters
from shadow.paper import PaperEngine
from shadow.safety import PASS, SafetyReport
from shadow.signals import SignalEngine, Sources
from shadow.strategies import DEFAULT_PARAMS, load_strategies, migrate_v11
from shadow.telegram_bot import TelegramBot, buy_text

pytestmark = pytest.mark.v11
NOW = 1_800_000_000.0
SOL = "So11111111111111111111111111111111111111112"


def S(**env):
    return load_settings(env={"DB_PATH": "x.db", **{k.upper(): str(v) for k, v in env.items()}})


# ------------------------------------------------------------------ settings
def test_v11_defaults():
    s = S()
    assert s.trade_min_mcap_usd == 1e6 and s.eval_min_mcap_usd == 750_000 and s.coins_per_day == 1000
    assert s.tg_notify == "trades" and s.fast_refresh_sec == 1.0 and s.fast_max_mints == 30
    assert s.trade_min_liq_usd == 50_000 and s.trade_min_liq_mcap == 0.015 and s.trade_min_age_min == 10
    assert s.learn_min_trades == 12 and s.learn_shrink_k == 20 and s.shadow_track
    assert S(coins_per_day=0).coins_per_day == 0 and S(telegram_notify="ALL").tg_notify == "all"
    assert S(trade_min_mcap_usd=2_000_000).trade_min_mcap_usd == 2e6


# ------------------------------------------------------------------ daily cap
def test_daily_cap_counts_unique_coins_per_pkt_day_and_survives_restart(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    t = {"now": NOW}
    cap = DailyCap(db, 2, clock=lambda: t["now"])
    assert cap.admit("A") and cap.admit("B") and cap.admit("A")       # same coin again is free
    assert not cap.admit("C") and cap.used == 2 and cap.stats["refused_today"] == 1
    cap2 = DailyCap(db, 2, clock=lambda: t["now"])                    # restart: today's coins remembered
    assert cap2.used == 2 and not cap2.admit("D") and cap2.admit("B")
    t["now"] += 86400                                                 # next PKT day: fresh budget
    assert cap2.admit("C") and cap2.admit("D") and cap2.used == 2
    assert DailyCap(db, 0, clock=lambda: t["now"]).admit("E")         # 0 = unlimited
    db.close()


# ------------------------------------------------------------------ gates
class Col:
    def __init__(self, mds):
        self.mds = mds
        self.tracked = {m: SimpleNamespace(market=md) for m, md in mds.items()}

    def tracked_mints(self):
        return list(self.tracked)

    def get_market(self, m):
        return self.mds.get(m)

    def get_candles(self, m, minutes=60):
        return []


def engine(tmp_path, mds, cap=None, **env):
    s = S(**env)
    db = Database(str(tmp_path / "e.db"))
    eng = SignalEngine(s, db, Sources(Col(mds)), checker=None, judge=None, clock=lambda: NOW, cap=cap)
    return s, db, eng


def ev(mcap, liq, age=60.0, score=80):
    return SimpleNamespace(features={"mcap": mcap, "liquidity": liq, "age_min": age}, score=score)


def test_eval_gate_mcap_floor_rug_dust_and_cap(tmp_path):
    mds = {"small": MarketData("small", price_usd=1, market_cap=50_000, liquidity_usd=20_000),
           "big": MarketData("big", price_usd=1, market_cap=3e6, liquidity_usd=200_000),
           "dust": MarketData("dust", price_usd=1, market_cap=40e6, liquidity_usd=100),   # drained pool, fake mcap
           "big2": MarketData("big2", price_usd=1, market_cap=2e6, liquidity_usd=90_000)}
    s, db, eng = engine(tmp_path, mds)
    eng.cap = DailyCap(db, 1, clock=lambda: NOW)
    assert not eng.eval_allowed("small", mds["small"]) and not eng.eval_allowed("dust", mds["dust"])
    assert eng.eval_allowed("big", mds["big"]) and not eng.eval_allowed("big2", mds["big2"])    # cap 1/day
    assert eng.stats["skip_below_eval_mcap"] == 2 and eng.stats["skip_daily_cap"] == 1
    eng.open[1] = {"mint": "small"}                       # an open watch is always managed
    assert eng.eval_allowed("small", mds["small"])
    db.close()


def test_trade_gate(tmp_path):
    s, db, eng = engine(tmp_path, {})
    assert eng.trade_gate(ev(900_000, 100_000))[0] == "silent"          # below $1M: never traded, no REJECT spam
    assert eng.trade_gate(ev(2e6, 30_000))[0] == "reject"               # thin absolute liquidity
    k, why = eng.trade_gate(ev(50e6, 100_000))                           # fake-supply launch: liq/mcap 0.2%
    assert k == "reject" and "liquidity/mcap" in why
    k, why = eng.trade_gate(ev(2e6, 100_000, age=4))
    assert k == "reject" and "age" in why
    assert eng.trade_gate(ev(2e6, 100_000, age=30)) == (None, "")
    db.close()


def test_paper_refuses_coins_below_one_million(tmp_path):
    s = S()
    db = Database(str(tmp_path / "p.db"))
    pe = PaperEngine(s, db, src=SimpleNamespace(get_market=lambda m: None), checker=None, clock=lambda: NOW)
    rep = SafetyReport("M" * 44, NOW - 30, NOW + 900, 90, PASS, [], [], {"quote": {"sellable": True, "impact_pct": 1}},
                       quote_ts=NOW - 5)
    row = {"id": 1, "mint": "M" * 44, "symbol": "M", "universe": "new", "strategy": "early_entry", "params_version": 1,
           "alert_type": "BUY_WATCH", "judge_verdict": "APPROVE", "judge_confidence": .7, "score": 80, "price": 1.0,
           "entry": 1.0, "sl": .9, "tp": 1.2, "time_stop_min": 20, "mcap": 400_000, "liquidity": 60_000}
    ok, why = pe.can_open(row, rep)
    assert not ok and "TRADE_MIN_MCAP" in why
    ok, _ = pe.can_open(dict(row, mcap=2e6, liquidity=20_000), rep)
    assert not ok
    ok, why = pe.can_open(dict(row, mcap=2e6, liquidity=100_000), rep)
    assert ok, why
    db.close()


# ------------------------------------------------------------------ learning
def test_shrinkage_needs_more_evidence_on_small_samples():
    assert shrink(None, 10, 20) is None and shrink(5.0, 10, 0) == 5.0
    assert shrink(3.0, 12, 20) == pytest.approx(3.0 * 12 / 32)
    assert shrink(3.0, 60, 20) > shrink(3.0, 12, 20)
    assert shrink(-2.0, 5, 20) == pytest.approx(-0.4)


# ------------------------------------------------------------------ strategies + migration
def test_migration_upgrades_untouched_defaults_once(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    old = json.loads(json.dumps(DEFAULT_PARAMS["early_entry"]["params"]))
    old["new"]["min_score"] = 65                                           # what v1.0 seeded
    db.save_strategy_params("early_entry", 1, old, ["new"], "default", note="phase 5 defaults")
    st = load_strategies(db)
    assert st["early_entry"].version == 2 and st["early_entry"].p("new")["min_score"] == 70
    assert "momentum_breakout" in st and st["momentum_breakout"].applies("mcap")
    ups = db.strategy_updates(target="early_entry")
    assert len(ups) == 1 and ups[0]["changes"]["new.min_score"] == [65, 70]
    from shadow import reports
    assert "min_score 65 → 70" in "\n".join(x for _, x in reports.pending_strategy_updates(db))
    assert migrate_v11(db) == [] and load_strategies(db)["early_entry"].version == 2       # idempotent
    db.close()


def test_migration_never_overwrites_learned_versions(tmp_path):
    db = Database(str(tmp_path / "m2.db"))
    p = json.loads(json.dumps(DEFAULT_PARAMS["early_entry"]["params"]))
    p["new"]["min_score"] = 66
    db.save_strategy_params("early_entry", 3, p, ["new"], "learned")
    assert load_strategies(db)["early_entry"].p("new")["min_score"] == 66
    db.close()


def test_momentum_breakout_entry():
    from shadow.signals import Det, DETECTORS
    det = lambda *n: {k: Det(k, 1, k in n, .8 if k in n else 0) for k in DETECTORS}  # noqa: E731
    db = Database(":memory:")
    mb = load_strategies(db)["momentum_breakout"]
    assert mb.entry_conditions("mcap", det("breakout", "volume_increase"), {})[0]
    assert not mb.entry_conditions("mcap", det("breakout"), {})[0]                       # needs confirmation
    assert not mb.entry_conditions("mcap", det("breakout", "volume_increase", "pump_exhaustion"), {})[0]
    assert not mb.entry_conditions("mcap", det("volume_increase", "buy_sell_pressure"), {})[0]


# ------------------------------------------------------------------ Telegram buy-only
class API:
    reachable = True
    calls = errors = retries = rate_limited = 0

    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    def redact(self, s):
        return s


def test_buy_only_pushes_nothing_but_paper_buys(tmp_path):
    s = load_settings(env={"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "7922990181", "TELEGRAM_NOTIFY": "buy"})
    assert s.tg_notify == "buy"
    db = Database(str(tmp_path / "t.db"))
    bot = TelegramBot(s, db, API(), clock=lambda: NOW)
    assert bot.buy_only
    for a in ("BUY_WATCH", "SELL", "HOLD", "REJECT", "SHADOW"):
        bot.on_signal({"alert_type": a, "mint": "M" * 44, "symbol": "MM", "universe": "mcap", "score": 82,
                       "mcap": 3e6, "liquidity": 150_000}, None)
    assert not bot.queue
    pos = {"id": 7, "mint": "M" * 44, "symbol": "MM", "universe": "mcap", "strategy": "momentum_breakout",
           "params_version": 1, "size_usd": 2.0, "entry_price": 1.01, "entry_slippage_pct": 1.0, "sl": .93, "tp": 1.1}
    bot.on_trade("OPEN", pos)
    bot.on_trade("CLOSE", dict(pos, pnl_usd=.1, exit_reason="take_profit"))
    assert len(bot.queue) == 1 and bot.queue[0].text.startswith("🟢 BUY - MM") and "MCAP $3" in bot.queue[0].text
    db.add_strategy_update("params", "early_entry", 1, 2, {}, "x", {})
    bot.queue_strategy_updates()
    assert len(bot.queue) == 1 and db.strategy_updates(unsent_only=True) == []    # kept for /strategies, not pushed
    db.meta_set("tg_daily_report_day", "2000-01-01")
    assert bot.check_daily_report() is False and len(bot.queue) == 1
    bot.check_digest()
    assert len(bot.queue) == 1
    db.close()


def test_buy_text_without_signal_row():
    t = buy_text({"id": 1, "mint": "Q" * 44, "universe": "volume", "strategy": "first_pullback", "entry_price": 2.0,
                  "sl": 1.8, "tp": 2.4, "size_usd": 2})
    assert "🟢 BUY" in t and "SL" in t and "-10.0%" in t and "PAPER" in t.upper()


# ------------------------------------------------------------------ fast price lane
def ds_pair(mint, price, liq):
    return {"chainId": "solana", "baseToken": {"address": mint, "symbol": mint[:3]}, "quoteToken": {"address": SOL},
            "priceUsd": str(price), "liquidity": {"usd": liq}, "marketCap": 2e6, "dexId": "raydium", "pairAddress": "p"}


def test_fast_pricer_refreshes_hot_coins_and_measures_interval(tmp_path):
    a, b, c = "A" * 44, "B" * 44, "C" * 44
    mds = {m: MarketData(m, price_usd=1.0, market_cap=2e6, liquidity_usd=100_000, updated_ts=NOW - 20) for m in (a, b, c)}
    col = Col(mds)
    prices = {"p": 1.0}
    seen = []

    def handler(req):
        if "price/v3" in str(req.url):
            return httpx.Response(200, json={})                 # Jupiter has no price: DexScreener's is used
        mints = req.url.path.rsplit("/", 1)[-1].split(",")
        seen.append(mints)
        prices["p"] += 0.01
        return httpx.Response(200, json=[ds_pair(m, prices["p"], 90_000) for m in mints])

    ticks = []
    t = {"now": NOW}
    eng = SimpleNamespace(paper=None, open={1: {"mint": a, "approved": True}}, proximity={b: (70, NOW - 10), c: (60, NOW - 999)})

    async def fast_tick():
        ticks.append(t["now"])
    eng.fast_tick = fast_tick

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cl:
            fp = FastPricer(S(), Sources(col), eng, client=cl, limiters=make_limiters(), clock=lambda: t["now"])
            for _ in range(5):
                await fp.tick()
                t["now"] += 1.0
            return fp
    fp = asyncio.run(go())
    assert seen[0] == [a, b]                               # open watch first, near-trigger coin; stale proximity out
    assert len(ticks) == 5 and mds[a].price_usd == pytest.approx(1.05) and mds[a].liquidity_usd == 90_000
    assert mds[a].updated_ts == NOW + 4
    st = fp.stats()
    assert st["calls"] == 5 and st["refresh_s_p50"] == 1.0 and st["hot"] == 2 and st["price_change_s_p50"] == 1.0


def test_fast_pricer_survives_errors_and_429(tmp_path):
    def handler(req):
        return httpx.Response(429) if handler.n % 2 else httpx.Response(500)
    handler.n = 0
    eng = SimpleNamespace(paper=None, open={1: {"mint": "A" * 44}}, proximity={})

    async def fast_tick():
        raise AssertionError("no tick without prices")
    eng.fast_tick = fast_tick

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cl:
            fp = FastPricer(S(), Sources(Col({})), eng, client=cl, limiters=make_limiters(), clock=lambda: NOW)
            assert await fp.tick() == 0
            handler.n = 1
            assert await fp.tick() == 0
            return fp
    fp = asyncio.run(go())
    assert fp.counters["errors"] == 1 and fp.counters["rate_limited"] == 1


def test_fast_lane_has_its_own_budget_under_dexscreener_limit():
    from shadow.net import DEFAULT_BUDGETS
    assert DEFAULT_BUDGETS["dexscreener"] + DEFAULT_BUDGETS["dexscreener_fast"] <= 300
    assert DEFAULT_BUDGETS["dexscreener_fast"] >= 60                     # room for 1 call / second


# ------------------------------------------------------------------ engine: gate + shadow tracking
class Chk:
    def __init__(self, rep):
        self.rep, self.prio = rep, []

    def get_report(self, m):
        return self.rep

    def prioritize(self, m):
        self.prio.append(m)

    def request_quote(self, m, priority=False):
        pass


def test_engine_gate_shadow_and_reject(tmp_path):
    from shadow.signals import Evaluation
    m = "S" * 44
    md = MarketData(m, symbol="SS", price_usd=1.0, market_cap=3e6, liquidity_usd=150_000, updated_ts=NOW)
    rep = SafetyReport(m, NOW - 30, NOW + 900, 90, PASS, [], [], {"quote": {"sellable": True, "impact_pct": 1}},
                       quote_ts=NOW - 5)
    s = S()
    db = Database(str(tmp_path / "g.db"))
    out = []
    eng = SignalEngine(s, db, Sources(Col({m: md})), checker=Chk(rep), judge=None, clock=lambda: NOW,
                       on_signal=lambda r, v: out.append(r))
    st = eng.strategies["momentum_breakout"]
    feats = {"price": 1.0, "mcap": 3e6, "liquidity": 150_000, "age_min": 600.0}
    mk = lambda score, **f: Evaluation(m, "mcap", {**feats, **f}, {}, score, {}, "Weak")  # noqa: E731
    asyncio.run(eng.handle_candidate(mk(40.0, mcap=500_000), st, "x"))            # below $1M: silent
    assert out == [] and eng.stats["gate_below_min_mcap"] == 1
    asyncio.run(eng.handle_candidate(mk(40.0), st, "breakout"))                   # near miss (min 50, margin 15)
    rows = [tuple(r) for r in db.con.execute("SELECT alert_type, status FROM signals")]
    assert out == [] and rows == [("SHADOW", "open")] and eng.stats["shadow_tracked"] == 1
    assert any(o.get("shadow") for o in eng.open.values()) and eng.hot(m)
    m2 = "T" * 44
    eng.src.collectors[0].mds[m2] = MarketData(m2, price_usd=1.0, market_cap=40e6, liquidity_usd=100_000)
    asyncio.run(eng.handle_candidate(Evaluation(m2, "mcap", {**feats, "mcap": 40e6, "liquidity": 100_000}, {}, 90.0,
                                                {}, "Strong"), st, "x"))          # fake-supply coin: thin pool
    assert out and out[-1]["alert_type"] == "REJECT" and "liquidity/mcap" in out[-1]["reason"]
    db.close()


def test_fast_pricer_uses_fresher_jupiter_price_for_watches(tmp_path):
    a = "A" * 44
    md = MarketData(a, price_usd=1.0, market_cap=2e6, liquidity_usd=100_000, updated_ts=NOW - 20)
    col = Col({a: md})
    hits = {"jup": 0}

    def handler(req):
        if "price/v3" in str(req.url):
            hits["jup"] += 1
            return httpx.Response(200, json={a: {"usdPrice": 1.07}})
        return httpx.Response(200, json=[ds_pair(a, 1.02, 95_000)])
    t = {"now": NOW}
    eng = SimpleNamespace(paper=None, open={1: {"mint": a, "approved": True}}, proximity={})

    async def fast_tick():
        pass
    eng.fast_tick = fast_tick

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cl:
            fp = FastPricer(S(), Sources(col), eng, client=cl, limiters=make_limiters(), clock=lambda: t["now"])
            for _ in range(6):
                await fp.tick()
                t["now"] += 1.0
            return fp
    fp = asyncio.run(go())
    assert hits["jup"] == 2                                  # every 5 s, not every second (keyless 30/min budget)
    assert md.price_usd == pytest.approx(1.07) and md.liquidity_usd == 95_000   # Jupiter price, DexScreener liquidity
    fp.jup[a] = (1.07, NOW - 100)                            # stale Jupiter price -> DexScreener again
    fp._jup_last = t["now"]
    asyncio.run(_one(fp, handler))
    assert md.price_usd == pytest.approx(1.02)


async def _one(fp, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cl:
        fp._client = cl
        await fp.tick()
