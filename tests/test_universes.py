"""Phase 5 universes on real lists recorded 2026-11-13 (tests/fixtures/universe_recorded.json):
GeckoTerminal trending / top 24h volume / top tx pools, DexScreener top boosts and DexScreener pairs."""
import json
from pathlib import Path

from shadow.collector import group_pairs, market_from_pairs, standard_quote
from shadow.config import load_settings
from shadow.db import Database
from shadow.models import MarketData
from shadow.universes import mcap_of, meme_filter, parse_ds_boosts, parse_gt_pools, select_universes

FIX = json.loads((Path(__file__).parent / "fixtures" / "universe_recorded.json").read_text())
S = load_settings(env={})
SOL, USDC, JUP, BONK = ("So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")


def markets():
    mints = FIX["mints"]
    out = []
    for m, ps in group_pairs(FIX["ds_pairs"], mints).items():
        good = [p for p in ps if standard_quote(p)]
        if good:
            out.append(market_from_pairs(m, good, FIX["recorded_at"], launchpad_hint=m.endswith("pump")))
    return out


def test_meme_filter_drops_sol_stables_majors():
    assert meme_filter(SOL, "SOL", "Wrapped SOL", 140) == "not_meme_asset"
    assert meme_filter(USDC, "USDC", "USD Coin", 1.0) == "not_meme_asset"
    assert meme_filter("x" * 44, "USDX", "some dollar", 1.0) == "stablecoin"
    assert meme_filter(JUP, "JUP", "Jupiter", 0.5) == "major"
    assert meme_filter(BONK, "Bonk", "Bonk", 0.00001) == "major"
    assert meme_filter(BONK, "Bonk", "Bonk", 0.00001, allow_majors=True) is None
    assert meme_filter("y" * 44, "NVDAx", "NVIDIA xStock", 180) == "not_meme_asset"
    assert meme_filter("y" * 44, "TSLAx", "Tesla", 400) == "tokenized_stock"
    assert meme_filter("z" * 44, "PEPE2", "pepe two", 0.001) is None
    assert meme_filter("z" * 44, "PEPE2", "pepe two", 0.001, extra_exclude={"z" * 44}) == "excluded_by_config"


def test_parse_recorded_lists():
    tr = parse_gt_pools(FIX["gt_trending"])
    tv = parse_gt_pools(FIX["gt_top_volume"])
    assert len(tr) >= 10 and len(tv) >= 10
    for p in tv:
        assert len(p["mint"]) >= 32 and p["vol_h24"] >= 0
    assert tv[0]["vol_h24"] >= tv[-1]["vol_h24"]            # sorted by 24h volume desc
    boosts = parse_ds_boosts(FIX["ds_boosts_top"])
    assert boosts and all(isinstance(m, str) for m in boosts)


def test_select_universes_on_recorded_markets():
    ms = [m for m in markets() if not meme_filter(m.mint, m.symbol, m.name, m.price_usd)]
    names = {m.symbol for m in markets()} - {m.symbol for m in ms}
    assert "SOL" in names or not any(m.mint == SOL for m in markets())
    mcap, vol, rank_h1 = select_universes(S, ms)
    assert mcap, "recorded data has $1M+ meme coins"
    assert all(mcap_of(m) >= S.universe_mcap_min and m.liquidity_usd >= S.universe_mcap_min_liq for m in mcap)
    assert [mcap_of(m) for m in mcap] == sorted((mcap_of(m) for m in mcap), reverse=True)
    assert len(vol) <= S.universe_volume_max
    top24 = sorted((m for m in ms if m.liquidity_usd and m.liquidity_usd >= S.universe_volume_min_liq
                    and mcap_of(m) >= S.universe_volume_min_mcap), key=lambda m: m.vol_h24, reverse=True)
    assert top24[0].mint in {m.mint for m in vol}            # the #1 24h-volume coin is in TOP VOLUME
    assert all(m.mint in rank_h1 for m in vol)
    assert not any(m.mint in (SOL, USDC, JUP) for m in mcap + vol)


def test_caps_are_configurable():
    ms = [m for m in markets() if not meme_filter(m.mint, m.symbol, m.name, m.price_usd)]
    s = load_settings(env={"UNIVERSE_MCAP_MAX": "3", "UNIVERSE_VOLUME_TOP_N": "2", "UNIVERSE_VOLUME_MAX": "3",
                           "UNIVERSE_MCAP_MIN": "5000000"})
    mcap, vol, _ = select_universes(s, ms)
    assert len(mcap) <= 3 and len(vol) <= 3
    assert all(mcap_of(m) >= 5e6 for m in mcap)


def test_universe_overwrite_fix_and_member_ranks(tmp_path):
    db = Database(str(tmp_path / "u.db"))
    md = MarketData("M" * 44, symbol="AAA", price_usd=1.0, market_cap=2e6, liquidity_usd=1e5, universe="mcap")
    db.upsert_markets([md])
    # the new-launch enrichment (universe='new') must not overwrite a universe membership
    db.upsert_market(MarketData("M" * 44, symbol="AAA", price_usd=1.1, market_cap=2.2e6, liquidity_usd=1e5))
    assert db.con.execute("SELECT universe FROM token_market").fetchone()[0] == "mcap"
    db.upsert_universe_member("M" * 44, "mcap", rank=1, metric=2e6, source="t", now=1.0, rank_h1=4, mcap=2e6,
                              vol_h24=5e5, vol_h1=1e4, liquidity_usd=1e5)
    db.upsert_universe_member("M" * 44, "volume", rank=7, metric=5e5, source="t", now=1.0, rank_h1=4, mcap=2e6,
                              vol_h24=5e5, vol_h1=1e4, liquidity_usd=1e5)
    assert set(db.universes_of("M" * 44)) == {"mcap", "volume"}
    db.deactivate_universe_members("volume", set())
    assert db.universe_mints("volume", active_only=True) == []
    r = db.con.execute("SELECT rank, rank_h1, active FROM universe_members WHERE universe='mcap'").fetchone()
    assert tuple(r) == (1, 4, 1)
    db.close()


def test_volume_needs_real_volume_and_mcap_params_win(tmp_path):
    ms = [m for m in markets() if not meme_filter(m.mint, m.symbol, m.name, m.price_usd)]
    s = load_settings(env={"UNIVERSE_VOLUME_MIN_VOL_H24": "1e12", "UNIVERSE_VOLUME_MIN_VOL_H1": "1e12"})
    assert select_universes(s, ms)[1] == []
    from shadow.universes import UniverseCollector
    db = Database(str(tmp_path / "u.db"))
    uc = UniverseCollector(S, db)
    uc.members = {"mcap": {"A"}, "volume": {"A", "B"}}
    assert uc._universe_of("A") == "mcap" and uc._universe_of("B") == "volume"
    db.close()


def test_gt_backfill_never_overwrites_live_candles(tmp_path):
    import asyncio

    import httpx

    from shadow.models import Candle
    from shadow.net import RateLimiter
    from shadow.universes import UniverseCollector
    # real GeckoTerminal 1m OHLCV rows recorded into candles_recorded.json, served in GT's payload shape
    rec = json.loads((Path(__file__).parent / "fixtures" / "candles_recorded.json").read_text())
    ser = next(v for k, v in rec["series"].items() if k.startswith("gt:"))
    rows = [r[:6] for r in ser["candles"][-60:]]
    ohlcv = {"data": {"attributes": {"ohlcv_list": rows}}}
    start = rows[-10][0]                                  # tracking started 10 minutes before the newest row
    calls = []

    def h(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=ohlcv)
    db = Database(str(tmp_path / "b.db"))
    lims = {"geckoterminal": RateLimiter("g", 6000)}
    uc = UniverseCollector(S, db, client=httpx.AsyncClient(transport=httpx.MockTransport(h)), limiters=lims,
                           clock=lambda: start + 30)
    md = MarketData("P" * 44, symbol="P", price_usd=1.0, pair_address="POOL1", market_cap=5e6, liquidity_usd=1e5,
                    vol_h1=1e4)
    uc.members = {"mcap": {md.mint}, "volume": set()}
    uc._start(md, start + 5)
    live = Candle(md.mint, int(start), 9, 9, 9, 9, 1, 3, 1, 2, "local")
    db.upsert_candles([live])
    n = asyncio.run(uc.backfill_once())
    assert calls == ["/api/v2/networks/solana/pools/POOL1/ohlcv/minute"]
    assert n == sum(1 for r in rows if r[0] < start) and n > 0
    got = {c.ts: c for c in db.get_candles(md.mint, 0, "local")}
    assert got[int(start)].close == 9 and got[int(start)].n_snapshots == 2        # live candle untouched
    assert all(c.n_snapshots == 0 and c.buys == 0 for t, c in got.items() if t < start)
    assert asyncio.run(uc.backfill_once()) == 0                                   # not retried within 30 min
    db.close()
