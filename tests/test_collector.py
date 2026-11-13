import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from shadow import collector as co
from shadow.candles import CandleBuilder, parse_gt_ohlcv
from shadow.config import load_settings
from shadow.db import SCHEMA_VERSION, Database
from shadow.models import AMM, BONDING, MIGRATED, MIGRATION, NEW_TOKEN, UNKNOWN, MarketData, TokenEvent
from shadow.net import RateLimiter

FIX = Path(__file__).parent / "fixtures"
DS = json.loads((FIX / "dexscreener_tokens_batch.json").read_text())      # recorded 2026-11-13
GTO = json.loads((FIX / "gt_ohlcv_minute.json").read_text())
JUP = json.loads((FIX / "jupiter_price_v3.json").read_text())
NOW = DS["recorded_at"]
FOUND = sorted({p["baseToken"]["address"] for p in DS["pairs"]})
NOT_FOUND = [m for m in DS["requested"] if m not in FOUND]


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def run(c):
    return asyncio.run(c)


def ds_handler(log, pairs=None, status=200):
    pairs = DS["pairs"] if pairs is None else pairs

    def h(req: httpx.Request):
        log.append(str(req.url))
        host = req.url.host
        if "dexscreener" in host:
            if status != 200:
                return httpx.Response(status)
            want = req.url.path.rsplit("/", 1)[-1].split(",")
            return httpx.Response(200, json=[p for p in pairs if p["baseToken"]["address"] in want])
        if "jup.ag" in host:
            return httpx.Response(200, json=JUP["response"])
        if "geckoterminal" in host:
            return httpx.Response(200, json=GTO)
        return httpx.Response(404)
    return h


def fast_limiters():
    return {k: RateLimiter(k, 60000) for k in ("dexscreener", "jupiter", "geckoterminal")}


@pytest.fixture
def env(tmp_path):
    def make(handler=None, clock=None, **cfg):
        s = load_settings(env={"DB_PATH": str(tmp_path / "c.db"), **{k.upper(): str(v) for k, v in cfg.items()}})
        db = Database(tmp_path / "c.db")
        log = []
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler or ds_handler(log)))
        c = co.Collector(s, db, client=client, limiters=fast_limiters(), clock=clock or Clock())
        return c, db, log
    return make


def ev(mint, source="geckoterminal", launch=None, **kw):
    return TokenEvent(NEW_TOKEN, mint, source, launch_ts=launch, **kw)


# ---------------------------------------------------------------- pure helpers
def test_chunks():
    assert [len(c) for c in co.chunks(list(range(65)), 30)] == [30, 30, 5]


def test_group_pairs_and_main_pair_on_recorded_batch():
    g = co.group_pairs(DS["pairs"], DS["requested"])
    assert sorted(g) == FOUND and len(FOUND) == 19 and len(NOT_FOUND) == 10
    bonk = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    fake_sol = dict(g[bonk][0], quoteToken={"address": "X", "symbol": "NVDAx"}, liquidity={"usd": 9e9})
    real = dict(g[bonk][0], quoteToken={"address": co.QUOTE_MINTS.copy().pop(), "symbol": "SOL"}, liquidity={"usd": 10})
    assert co.pick_main_pair([fake_sol, real]) is real  # standard quote beats bigger liquidity


@pytest.mark.parametrize("dexes,hint,expected", [
    (["pumpfun"], False, BONDING), (["pumpfun", "pumpswap"], False, MIGRATED), (["pumpswap"], True, MIGRATED),
    (["raydium"], False, AMM), (["meteoradbc"], False, BONDING), ([], False, UNKNOWN)])
def test_derive_status(dexes, hint, expected):
    assert co.derive_status([{"dexId": d} for d in dexes], hint) == expected


def test_market_from_recorded_pumpfun_pair():
    p = next(x for x in DS["pairs"] if x["dexId"] == "pumpfun")
    md = co.market_from_pairs(p["baseToken"]["address"], [p], NOW)
    assert md.price_usd == float(p["priceUsd"]) and md.market_cap == p["marketCap"] and md.fdv == p["fdv"]
    assert md.vol_m5 == p["volume"]["m5"] and md.buys_h24 == p["txns"]["h24"]["buys"]
    assert md.sells_h1 == p["txns"]["h1"]["sells"] and md.migration_status == BONDING
    assert md.liquidity_usd is None and md.pair_address == p["pairAddress"]  # DexScreener has no liquidity here
    assert md.pair_created_ts == p["pairCreatedAt"] / 1000 and 0 <= md.age_min(NOW) < 120


# ---------------------------------------------------------------- enrichment
def test_batches_of_30(env):
    c, db, log = env(min_track_txns=0)
    for i in range(65):
        c.submit(ev(f"Fake{i:03d}"))
    for _ in range(3):
        run(c.enrich_once())
    ds_calls = [u for u in log if "dexscreener" in u]
    assert [len(u.rsplit("/", 1)[-1].split(",")) for u in ds_calls] == [30, 30, 5]
    assert c.stats["ds_calls"] == 3


def test_real_batch_enriches_found_and_retries_missing(env):
    clock = Clock()
    c, db, log = env(clock=clock, min_track_txns=0, max_tracked=100)
    for m in DS["requested"]:
        c.submit(ev(m, "rugcheck"))
    run(c.enrich_once())
    old_bonk = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    assert c.drops["too_old"] == 1 and old_bonk not in c.tracked     # real age from the oldest pair
    assert c.stats["enriched"] + c.drops["not_meme"] + c.drops["too_old"] == len(FOUND)
    assert set(c.pending) == set(NOT_FOUND)                           # no pair yet -> retry later
    clock.t += 30
    run(c.enrich_once())
    assert set(c.pending) == set(NOT_FOUND)
    clock.t += 400                                                    # past NO_PAIR_TTL_SEC=300
    run(c.enrich_once())
    assert c.drops["no_pair"] == len(NOT_FOUND) and not c.pending
    assert db.table_counts()["collector_drops"] == sum(c.drops.values())


def test_old_launch_dropped_without_api_call(env):
    c, db, log = env()
    c.submit(ev("OldMint", "rugcheck", launch=NOW - 3 * 3600))
    assert c.drops["too_old"] == 1 and not c.pending
    run(c.enrich_once())
    assert log == []


def test_max_age_is_configurable(env):
    c, db, log = env(max_token_age_min=1)
    m = next(p for p in DS["pairs"] if p["dexId"] == "pumpfun" and NOW - p["pairCreatedAt"] / 1000 > 60)
    c.submit(ev(m["baseToken"]["address"]))
    run(c.enrich_once())
    assert c.drops["too_old"] == 1


def test_not_meme_when_only_odd_quotes(env):
    p = dict(DS["pairs"][0], quoteToken={"address": "NVDAxMint", "symbol": "NVDAx"})
    mint = p["baseToken"]["address"]
    c, db, log = env(handler=ds_handler([], pairs=[p]))
    c.submit(ev(mint, "rugcheck"))
    run(c.enrich_once())
    assert c.drops["not_meme"] == 1 and "NVDAx" in db.con.execute("SELECT detail FROM collector_drops").fetchone()[0]


def test_quiet_tokens_rechecked_then_dropped_inactive(env):
    clock = Clock()
    p = dict(DS["pairs"][0], txns={w: {"buys": 1, "sells": 0} for w in ("m5", "h1", "h6", "h24")})
    c, db, log = env(handler=ds_handler([], pairs=[p]), clock=clock, min_track_txns=5)
    c.submit(ev(p["baseToken"]["address"]))
    run(c.enrich_once())
    assert c.stats["enriched"] == 1 and c.stats["quiet_rechecks"] == 1 and not c.tracked
    clock.t += 301
    run(c.enrich_once())
    assert c.drops["inactive"] == 1 and c.stats["enriched"] == 1


def test_tracking_cap_eviction_and_min_hold(env):
    clock = Clock()
    c, db, log = env(clock=clock, max_tracked=2, min_hold_sec=120)
    mk = lambda m, v: MarketData(m, vol_m5=v, launch_ts=NOW, buys_h24=10)  # noqa: E731
    assert c._start_tracking(mk("A", 10), NOW) and c._start_tracking(mk("B", 20), NOW)
    assert not c._start_tracking(mk("C", 999), NOW)           # A/B still within min hold
    assert c.drops["tracking_full"] == 1
    clock.t += 200
    assert c._start_tracking(mk("D", 50), clock.t)             # evicts weakest (A)
    assert set(c.tracked) == {"B", "D"} and c.stats["stopped_evicted"] == 1
    assert not c._start_tracking(mk("E", 1), clock.t)          # quieter than everyone


def test_migration_derived_from_dexid(env, tmp_path):
    base = next(p for p in DS["pairs"] if p["dexId"] == "pumpfun")
    amm = dict(base, dexId="pumpswap", pairAddress="AMMPAIR", liquidity={"usd": 5000})
    c, db, log = env(handler=ds_handler([], pairs=[base, amm]), min_track_txns=0)
    mint = base["baseToken"]["address"]
    db.mark_new(TokenEvent(NEW_TOKEN, mint, "pumpportal", status=BONDING))
    c.submit(ev(mint, "pumpportal", status=BONDING, launchpad="pump"))
    run(c.enrich_once())
    md = c.get_market(mint)
    assert md.migration_status == MIGRATED and md.pair_address == "AMMPAIR" and md.liquidity_source == "dexscreener"
    assert db.status_of(mint) == MIGRATED and c.stats["migrations_derived"] == 1


# ---------------------------------------------------------------- candles
def test_candle_builder_ohlc_and_deltas():
    b = CandleBuilder("M")
    t0 = 1_800_000_000 - 1_800_000_000 % 60
    assert b.add(t0 + 1, 1.0, 100, 10, 5) == []                 # baseline only
    b.add(t0 + 20, 1.5, 150, 14, 6)
    b.add(t0 + 40, 0.8, 170, 15, 9)
    done = b.add(t0 + 61, 1.2, 200, 18, 9)
    assert len(done) == 1
    c = done[0]
    assert (c.ts, c.open, c.high, c.low, c.close) == (t0, 1.0, 1.5, 0.8, 0.8)
    assert (c.volume_usd, c.buys, c.sells, c.n_snapshots) == (70, 5, 4, 3)
    cur = b.partial()
    assert cur.open == 0.8 and cur.close == 1.2 and cur.volume_usd == 30 and cur.buys == 3


def test_candle_gap_fill_reset_and_out_of_order():
    b = CandleBuilder("M", max_gap_fill=10)
    t0 = 1_800_000_000 - 1_800_000_000 % 60
    b.add(t0, 1.0, 100, 1, 1)
    done = b.add(t0 + 240, 2.0, 50, 2, 2)                      # 3 empty minutes, counter went down
    assert [c.ts - t0 for c in done] == [0, 60, 120, 180]
    assert all(c.n_snapshots == 0 and c.close == 1.0 for c in done[1:])
    assert b.partial().volume_usd == 0                         # reset, no negative volume
    assert b.add(t0 + 10, 9.0) == [] and b.partial().high == 2.0   # out of order ignored
    b.add(t0 + 250, None, 80, 3, 2)                            # no price: still counts trades
    assert b.partial().volume_usd == 30 and b.partial().buys == 2  # 1 at t0+240 (1->2) + 1 now


def test_parse_recorded_gt_ohlcv():
    cs = parse_gt_ohlcv("M", GTO)
    rows = GTO["data"]["attributes"]["ohlcv_list"]
    assert len(cs) == len(rows) and cs[0].source == "geckoterminal"
    assert [c.ts for c in cs] == sorted(r[0] for r in rows)
    assert parse_gt_ohlcv("M", {}) == []


# ---------------------------------------------------------------- tracking loop
def test_track_once_snapshots_candles_and_stops(env):
    clock = Clock()
    c, db, log = env(clock=clock, min_track_txns=0, max_tracked=100, dead_after_min=10)
    for m in FOUND:
        c.submit(ev(m))
    run(c.enrich_once())
    n = len(c.tracked)
    assert n > 5
    for _ in range(5):                                         # 5 polls, 15 s apart -> crosses a minute
        clock.t += 15
        run(c.track_once())
    assert c.stats["snapshots"] == n * 6
    assert db.table_counts()["price_snapshots"] == n * 6
    assert c.stats["candles_local"] >= n
    assert len(c.get_candles(FOUND[0], 10)) >= 1
    clock.t += 11 * 60                                         # nothing traded since -> dead
    run(c.track_once())
    assert c.stats["stopped_dead"] == n and not c.tracked
    assert db.tracked_mints() == []


def test_aged_out(env):
    clock = Clock()
    c, db, log = env(clock=clock, min_track_txns=0, max_token_age_min=120)
    for m in FOUND:
        c.submit(ev(m))
    run(c.enrich_once())
    m = next(iter(c.tracked))
    clock.t = c.tracked[m].market.launch_ts + 121 * 60
    for t in c.tracked.values():
        t.last_active_ts = clock.t
    c.check_stops()
    assert c.stats["stopped_aged_out"] >= 1 and m not in c.tracked


def test_lost_pair_uses_jupiter_fallback_then_stops(env):
    clock = Clock()
    jmint = next(m for m in JUP["requested"] if (JUP["response"].get(m) or {}).get("usdPrice"))
    c, db, log = env(handler=ds_handler([], pairs=[]), clock=clock)
    c.tracked[jmint] = co.Tracked(MarketData(jmint, launch_ts=NOW), CandleBuilder(jmint), last_active_ts=NOW)
    for _ in range(4):
        clock.t += 15
        c.tracked[jmint].last_active_ts = clock.t if jmint in c.tracked else 0
        run(c.track_once())
    assert c.stats["snapshots_jupiter"] >= 3
    assert c.stats["stopped_lost"] == 1


def test_jupiter_fills_missing_liquidity(env):
    c, db, log = env()
    jm = [m for m in JUP["requested"] if (JUP["response"].get(m) or {}).get("liquidity")]
    for m in jm:
        c.tracked[m] = co.Tracked(MarketData(m, launch_ts=NOW), CandleBuilder(m), last_active_ts=NOW)
    n = run(c.jup_once())
    assert n == len(jm) and all(c.tracked[m].market.liquidity_source == "jupiter" for m in jm)
    assert sum("jup.ag" in u for u in log) == 1                 # one batched call (<= 50 mints)


def test_hot_set_gets_gt_ohlcv(env):
    c, db, log = env(hot_size=2)
    for i, v in enumerate([5, 50, 500]):
        m = f"H{i}"
        c.tracked[m] = co.Tracked(MarketData(m, vol_m5=v, pair_address=f"P{i}", launch_ts=NOW), CandleBuilder(m),
                                  last_active_ts=NOW)
    assert c.hot_mints() == ["H2", "H1"]
    n = run(c.hot_once())
    assert n == 2 * len(GTO["data"]["attributes"]["ohlcv_list"]) and c.stats["gt_calls"] == 2
    assert sum("geckoterminal" in u for u in log) == 2
    assert len(c.get_candles("H2", minutes=10**6, source="geckoterminal")) >= 1


# ---------------------------------------------------------------- rate limits + errors
def test_dexscreener_429_pauses_queue_and_reschedules(env):
    log = []
    c, db, _ = env(handler=ds_handler(log, status=429))
    c.submit(ev("X1"))
    run(c.enrich_once())
    assert c.stats["errors"] == 1 and c.limiters["dexscreener"].hits_429 == 1
    assert c.pending["X1"].next_ts > NOW                        # retried later, not dropped


def test_requests_go_through_shared_limiter(env):
    c, db, log = env(min_track_txns=0)
    for m in DS["requested"]:
        c.submit(ev(m))
    run(c.enrich_once())
    run(c.track_once())
    assert c.limiters["dexscreener"].calls == c.stats["ds_calls"] == 2


def test_default_budgets_respect_documented_limits():
    from shadow.net import DEFAULT_BUDGETS
    assert DEFAULT_BUDGETS["dexscreener"] <= 300 and DEFAULT_BUDGETS["jupiter"] <= 30
    assert DEFAULT_BUDGETS["geckoterminal"] <= 10 and DEFAULT_BUDGETS["rugcheck"] <= 60


# ---------------------------------------------------------------- db: retention, restore, schema
def test_retention_cleanup(tmp_path):
    db = Database(tmp_path / "r.db")
    now = 2_000_000_000
    db.add_snapshots([("M", now - 7 * 3600, 1, 0, 0, 0, 0, 0, 0, "dexscreener"),
                      ("M", now - 60, 1, 0, 0, 0, 0, 0, 0, "dexscreener")])
    from shadow.models import Candle
    db.upsert_candles([Candle("M", now - 49 * 3600, 1, 1, 1, 1), Candle("M", now - 120, 1, 1, 1, 1)])
    db.upsert_market(MarketData("OLD", updated_ts=now - 8 * 86400), tracking=False)
    db.upsert_market(MarketData("LIVE", updated_ts=now - 8 * 86400), tracking=True)
    out = db.cleanup(6, 48, now=now)
    assert out["snapshots"] == 1 and out["candles"] == 1 and out["market"] == 1
    assert db.table_counts()["price_snapshots"] == 1 and db.get_market("LIVE") is not None


def test_restore_tracking_after_restart(env, tmp_path):
    c, db, log = env(min_track_txns=0)
    for m in FOUND:
        c.submit(ev(m))
    run(c.enrich_once())
    before = set(c.tracked)
    c2 = co.Collector(c.s, Database(tmp_path / "c.db"), client=c._client, limiters=fast_limiters(), clock=Clock())
    assert c2.restore() == len(before) and set(c2.tracked) == before
    m = next(iter(before))
    assert c2.get_market(m).price_usd == c.get_market(m).price_usd


def test_schema_upgrade_v1_to_v2_keeps_seen_tokens(tmp_path):
    from shadow.db import MIGRATIONS
    p = tmp_path / "v1.db"
    con = sqlite3.connect(p)
    con.executescript(MIGRATIONS[0])
    con.execute("INSERT INTO seen_tokens (mint, source, first_seen_ts, status) VALUES ('KEEP','pumpportal',1,'bonding')")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    db = Database(p)
    assert db.version == SCHEMA_VERSION == 5 and db.is_seen("KEEP")
    assert db.table_counts()["candles_1m"] == 0


def test_candles_df_and_partial(env):
    pd = pytest.importorskip("pandas")
    clock = Clock()
    c, db, log = env(clock=clock, min_track_txns=0)
    for m in FOUND:
        c.submit(ev(m))
    run(c.enrich_once())
    m = next(iter(c.tracked))
    df = c.candles_df(m)
    assert isinstance(df, pd.DataFrame) and len(df) == 1 and {"open", "close", "volume_usd"} <= set(df.columns)


def test_submit_migration_updates_tracked(env):
    c, db, log = env()
    c.tracked["T"] = co.Tracked(MarketData("T", migration_status=BONDING), CandleBuilder("T"), last_active_ts=NOW)
    c.submit(TokenEvent(MIGRATION, "T", "pumpportal", status=MIGRATED))
    assert c.tracked["T"].market.migration_status == MIGRATED and c.stats["migrations_tracked"] == 1


# ---------------------------------------------------------------- phase 4 collector changes
def test_bags_is_a_bonding_launchpad():
    # Bags launches on Meteora DBC: dexId "bags" without liquidity, then a meteora damm-v2 pair after migrating
    assert co.derive_status([{"dexId": "bags"}]) == BONDING
    assert co.derive_status([{"dexId": "bags"}, {"dexId": "meteora"}]) == MIGRATED


def test_default_max_tracked_is_200():
    assert load_settings(env={}).max_tracked == 200


def test_tracking_full_is_deferred_and_requeued_when_a_slot_frees(env):
    clock = Clock()
    c, db, log = env(clock=clock, max_tracked=2, min_hold_sec=120)
    mk = lambda m, v, launch=NOW: MarketData(m, vol_m5=v, launch_ts=launch, buys_h24=10)  # noqa: E731
    assert c._start_tracking(mk("A", 10), NOW) and c._start_tracking(mk("B", 20), NOW)
    assert not c._start_tracking(mk("C", 5), NOW) and not c._start_tracking(mk("OLD", 9, NOW - 7000), NOW)
    assert set(c.deferred) == {"C", "OLD"} and c.requeue_deferred(NOW) == 0   # no free slot yet
    c.stop_tracking("A", "dead")
    assert c.requeue_deferred(NOW + 300) == 1                                  # OLD aged out (>120 min)
    assert "C" in c.pending and c.pending["C"].enriched and not c.deferred
    assert c.stats["deferred_expired"] == 1 and c.stats["requeued_from_full"] == 1


def test_migration_is_atomic_and_upgrades_prerelease_v3(tmp_path):
    import sqlite3

    from shadow.db import MIGRATIONS, SCHEMA_VERSION, Database
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    for i, m in enumerate(MIGRATIONS[:3]):           # a v3 database created before universe_members existed
        con.executescript(m.split("-- a coin can be in several lists")[0] if i == 2 else m)
    con.execute("PRAGMA user_version = 3")
    con.close()
    db = Database(str(p))
    assert db.version == SCHEMA_VERSION
    cols = [r[1] for r in db.con.execute("PRAGMA table_info(universe_members)")]
    assert "rank_h1" in cols and "active" in cols
    db.close()
    # a failing step is rolled back completely
    bad = tmp_path / "bad.db"
    con = sqlite3.connect(bad)
    con.execute("CREATE TABLE universe_members (mint TEXT, universe TEXT, first_ts REAL, last_ts REAL, rank INTEGER,"
                " metric REAL, source TEXT, active INTEGER)")     # 'active' already there -> v4 ALTER fails midway
    con.execute("PRAGMA user_version = 3")
    con.commit()
    con.close()
    import pytest
    with pytest.raises(sqlite3.OperationalError):
        Database(str(bad))
    con = sqlite3.connect(bad)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM sqlite_master WHERE name='signals'").fetchone()[0] == 0
    con.close()
