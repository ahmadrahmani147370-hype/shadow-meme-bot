"""Phase 4 safety checker tests on real responses recorded 2026-11-13 (tests/fixtures/safety_recorded.json).

Known-bad cases: USDC (active mint + freeze authority), PYUSD (Token-2022 permanent delegate, transfer hook
authority, transfer-fee authority), "risky" pump token (87% single real holder, unlocked LP, $0.03 liquidity,
insider networks), "rc_new" (RugCheck: creator history of rugged tokens, serial creator) and "no_route"
(Jupiter: No routes found). "fresh_pump" shows the pump.fun mayhem wallet + bonding curve being excluded.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from shadow import safety as sf
from shadow.config import load_settings
from shadow.db import Database
from shadow.models import AMM, BONDING, MIGRATED, MarketData, TokenEvent
from shadow.net import RateLimiter

FIX = json.loads((Path(__file__).parent / "fixtures" / "safety_recorded.json").read_text())
T = FIX["tokens"]
NOW = FIX["recorded_at"]
ORDER = ["bonk", "usdc", "pyusd", "risky", "fresh_pump", "rc_new"]
RPC = {T[k]: v for k, v in zip(ORDER, FIX["rpc"]["result"]["value"])}
S = load_settings(env={})


def info(name):
    return sf.parse_mint_account(RPC[T[name]])


def rc(name):
    return sf.trim_rugcheck(FIX["rugcheck"][name])


def quote(name):
    q = FIX["jupiter_quote"][name]
    return sf.parse_quote(q["status"], q["body"])


def market(mint, liq=20000, mcap=100000, status=BONDING, **kw):
    return MarketData(mint, price_usd=0.0001, liquidity_usd=liq, market_cap=mcap, migration_status=status,
                      vol_h1=5000, buys_h1=60, sells_h1=40, buys_h24=60, sells_h24=40, **kw)


def ev(name, **kw):
    return sf.evaluate(S, T[name], now=NOW, mint_info=info(name), rc=rc(name), **kw)


def codes(rep):
    return {f.code for f in rep.flags}


# ---------------------------------------------------------------- parsers
def test_parse_rpc_accounts():
    assert info("bonk")["mint_authority"] is None and info("bonk")["freeze_authority"] is None
    assert info("bonk")["decimals"] == 5 and info("bonk")["program"] == sf.SPL_TOKEN
    assert info("usdc")["mint_authority"] and info("usdc")["freeze_authority"]
    py = info("pyusd")
    assert py["program"] == sf.TOKEN_2022 and {"permanentDelegate", "transferHook", "transferFeeConfig"} <= set(py["extensions"])
    assert set(info("fresh_pump")["extensions"]) == {"metadataPointer", "tokenMetadata"}
    assert sf.parse_mint_account(None) is None


def test_extension_rules_on_pyusd():
    fl = {f.code: f.severity for f in sf.extension_flags(info("pyusd")["extensions"])}
    assert fl["permanent_delegate"] == sf.CRITICAL
    assert fl["transfer_hook_authority"] == sf.CRITICAL        # strict: programId null but authority can add one
    assert fl["transfer_fee_authority"] == sf.CRITICAL         # strict: fee 0 bps, authority can raise it
    assert fl["mint_close_authority"] == sf.WARN and fl["confidential_transfers"] == sf.WARN


@pytest.mark.parametrize("ext,code", [
    ({"transferFeeConfig": {"newerTransferFee": {"transferFeeBasisPoints": 500}}}, "transfer_fee"),
    ({"nonTransferable": {}}, "non_transferable"),
    ({"transferHook": {"authority": None, "programId": "Hook111"}}, "transfer_hook"),
    ({"defaultAccountState": {"accountState": "frozen"}}, "default_frozen"),
    ({"pausableConfig": {"authority": "Auth", "paused": False}}, "pausable"),
])
def test_each_dangerous_extension_is_critical(ext, code):
    fl = sf.extension_flags(ext)
    assert [(f.code, f.severity) for f in fl] == [(code, sf.CRITICAL)]


def test_harmless_metadata_extensions_have_no_flags():
    assert sf.extension_flags(info("rc_new")["extensions"]) == []


def test_holder_concentration_excludes_pools_and_mayhem_wallet():
    hc = sf.holder_concentration(rc("fresh_pump"))
    names = [n for n, _ in hc["excluded"]]
    assert "pump.fun mayhem agent" in names and "Pump Fun" in names     # 50.24% + bonding curve 49.54%
    assert hc["top1_pct"] < 1 and hc["mayhem_pct"] == pytest.approx(50.24, abs=0.01)
    risky = sf.holder_concentration(rc("risky"))
    assert risky["top1_pct"] == pytest.approx(87.52, abs=0.01)          # a real wallet, not a pool


def test_insider_stats_risky():
    st = sf.insider_stats(rc("risky"))
    assert st["largest_network"] == 267 and st["graph_insiders"] == 311 and st["insider_pct"] > 15


def test_parse_quotes():
    assert quote("bonk")["sellable"] and quote("bonk")["impact_pct"] == pytest.approx(0.09, abs=0.01)
    assert quote("rc_new")["impact_pct"] == pytest.approx(1.315, abs=0.01)
    nr = quote("no_route")
    assert nr["sellable"] is False and "NO_ROUTES_FOUND" in nr["error"]
    assert sf.parse_quote(500, {"error": "boom"})["sellable"] is None
    assert sf.quote_amount_raw(2.0, 0.0001, 6) == 20000 * 10**6
    assert sf.quote_amount_raw(2.0, None, 6) is None


# ---------------------------------------------------------------- verdicts on real tokens
def test_bonk_passes():
    r = ev("bonk", market=market(T["bonk"], liq=1_465_964, mcap=1.4e9, status=AMM, universe="mcap"), quote=quote("bonk"))
    assert r.verdict == sf.PASS and not r.critical and r.score >= 70, r.flags
    assert "mutable_metadata" in codes(r)


def test_usdc_rejected_for_authorities():
    r = ev("usdc")
    assert r.verdict == sf.REJECT and {"mint_authority", "freeze_authority"} <= set(r.critical) and r.score == 0


def test_pyusd_rejected_for_token2022_extensions():
    r = ev("pyusd")
    assert "permanent_delegate" in r.critical and r.verdict == sf.REJECT


def test_risky_pump_rejected_concentration_lp_liquidity():
    r = ev("risky", market=market(T["risky"], liq=0.03, status=MIGRATED))
    assert {"extreme_holder_concentration", "extreme_insider_concentration", "liquidity_too_low"} <= set(r.critical)
    assert r.verdict == sf.REJECT and {"lp_unlocked", "insider_cluster"} <= codes(r)


def test_rc_new_serial_rugger_rejected_by_score_and_blacklisted(tmp_path):
    r = ev("rc_new", market=market(T["rc_new"]), quote=quote("rc_new"))
    assert {"creator_rug_history", "serial_creator"} <= set(r.critical)    # strict: both are critical
    assert r.verdict == sf.REJECT


def test_fresh_pump_passes_with_infra_excluded():
    r = ev("fresh_pump", market=market(T["fresh_pump"], liq=5000), quote=quote("fresh_pump"))
    assert r.verdict == sf.PASS and "extreme_holder_concentration" not in codes(r) and "mayhem_supply" in codes(r)


def test_no_route_is_critical():
    r = sf.evaluate(S, T["no_route"], now=NOW, mint_info=info("rc_new"), rc=rc("fresh_pump"),
                    quote=quote("no_route"), market=market(T["no_route"]))
    assert "not_sellable" in r.critical and r.verdict == sf.REJECT


def test_extreme_price_impact_is_critical():
    body = dict(FIX["jupiter_quote"]["rc_new"]["body"], priceImpactPct="0.45")   # recorded body, impact edited
    r = ev("fresh_pump", market=market(T["fresh_pump"]), quote=sf.parse_quote(200, body))
    assert "extreme_price_impact" in r.critical


def test_rugged_flag_is_critical():
    rep = dict(rc("bonk"), rugged=True)
    r = sf.evaluate(S, T["bonk"], now=NOW, mint_info=info("bonk"), rc=rep)
    assert "rugged" in r.critical


def test_rpc_failure_falls_back_to_rugcheck_authorities():
    rep = dict(rc("bonk"), token=dict(rc("bonk")["token"], freezeAuthority="Freeze111"))
    r = sf.evaluate(S, T["bonk"], now=NOW, mint_info=None, rc=rep)
    assert "freeze_authority" in r.critical and r.data["authority_source"] == "rugcheck"


def test_no_data_is_unknown_and_retried_soon():
    r = sf.evaluate(S, "X", now=NOW)
    assert r.verdict == sf.UNKNOWN and r.partial and r.expires_ts - NOW <= 120


def test_fake_volume_and_rug_warning_heuristics():
    md = MarketData("M", price_usd=1e-4, liquidity_usd=2000, market_cap=1e5, migration_status=BONDING,
                    vol_h1=400_000, buys_h1=150, sells_h1=150, buys_h24=150, sells_h24=150, price_change_h1=-80)
    rep = dict(rc("fresh_pump"), totalHolders=8)
    r = sf.evaluate(S, "M", now=NOW, mint_info=info("fresh_pump"), rc=rep, market=md,
                    local={"snapshots": {"n": 10, "peak": 1e-3, "last": 1e-4, "liq_peak": 9000, "liq_last": 2000}})
    assert {"wash_volume", "balanced_wash", "few_makers", "rug_warning", "low_liquidity"} <= codes(r)
    md2 = MarketData("M", vol_h1=250, buys_h1=100, sells_h1=20, liquidity_usd=50_000, market_cap=5e6, migration_status=AMM)
    r2 = sf.evaluate(S, "M", now=NOW, mint_info=info("fresh_pump"), rc=rc("fresh_pump"), market=md2)
    assert {"micro_trades", "thin_liquidity"} <= codes(r2)


def test_thresholds_come_from_env():
    s = load_settings(env={"SAFETY_TOP1_CRIT_PCT": "95", "SAFETY_TOP10_CRIT_PCT": "100"})
    r = sf.evaluate(s, T["risky"], now=NOW, mint_info=info("risky"), rc=rc("risky"))
    assert "extreme_holder_concentration" not in r.critical


def test_report_roundtrip():
    r = ev("usdc")
    r2 = sf.SafetyReport.from_row(r.to_row())
    assert r2.critical == r.critical and r2.flags == r.flags and r2.verdict == r.verdict


# ---------------------------------------------------------------- checker I/O with mocked HTTP
def handler(log, rpc_fail=False):
    def h(req: httpx.Request):
        log.append((req.url.host, req.url.path))
        if "publicnode" in req.url.host:
            mints = json.loads(req.content)["params"][0]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [RPC.get(m) for m in mints]}})
        if "solana" in req.url.host:
            if rpc_fail:
                return httpx.Response(429, headers={"retry-after": "0"})
            mints = json.loads(req.content)["params"][0]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {"context": {}, "value": [RPC.get(m) for m in mints]}})
        if "rugcheck" in req.url.host:
            mint = req.url.path.split("/")[3]
            name = next((k for k, v in T.items() if v == mint), None)
            if name in FIX["rugcheck"]:
                return httpx.Response(200, json=FIX["rugcheck"][name])
            return httpx.Response(400, json={"error": "not found"})
        if "jup.ag" in req.url.host:
            mint = req.url.params["inputMint"]
            name = next((k for k, v in T.items() if v == mint), None)
            q = FIX["jupiter_quote"].get(name) or FIX["jupiter_quote"]["no_route"]
            return httpx.Response(q["status"], json=q["body"])
        return httpx.Response(404)
    return h


def make(tmp_path, rpc_fail=False, **cfg):
    s = load_settings(env={"DB_PATH": str(tmp_path / "s.db"), **{k.upper(): str(v) for k, v in cfg.items()}})
    db = Database(tmp_path / "s.db")
    log = []
    lims = {k: RateLimiter(k, 60000) for k in ("solana_rpc", "solana_rpc_fallback", "rugcheck", "jupiter")}
    c = sf.SafetyChecker(s, db, client=httpx.AsyncClient(transport=httpx.MockTransport(handler(log, rpc_fail))),
                         limiters=lims, clock=lambda: NOW)
    c.rc_pace = RateLimiter("x", 60000)
    c.quote_pace = RateLimiter("y", 60000)
    return c, db, log


def test_check_caches_and_get_report(tmp_path):
    c, db, log = make(tmp_path)
    md = market(T["rc_new"], price_native=1e-6, quote_symbol="SOL")
    r = asyncio.run(c.check(T["rc_new"], quote=True, market=md))
    assert r.verdict == sf.REJECT and r.quote_ts and r.data["quote"]["sellable"]
    assert c.get_report(T["rc_new"]).verdict == sf.REJECT
    assert db.get_safety(T["rc_new"])["verdict"] == sf.REJECT
    n = len(log)
    asyncio.run(c.check(T["rc_new"], quote=True, market=md))      # all three sources still fresh
    assert len(log) == n and c.stats["rc_cache_hits"] == 1
    # learned blacklist: next token by the same creator is flagged even without RugCheck history
    assert db.creator_blacklisted(FIX["rugcheck"]["rc_new"]["creator"])
    c2 = sf.SafetyChecker(c.s, Database(tmp_path / "s.db"), client=c._client, limiters=c.limiters, clock=lambda: NOW)
    assert c2.get_report(T["rc_new"]).critical == r.critical       # loaded from SQLite


def test_rpc_429_uses_publicnode_fallback(tmp_path):
    c, db, log = make(tmp_path, rpc_fail=True)
    r = asyncio.run(c.check(T["usdc"]))
    assert "mint_authority" in r.critical and c.stats["rpc_fallback_calls"] == 1
    assert c.limiters["solana_rpc"].hits_429 == 1


def test_batched_rpc_one_call_for_many_mints(tmp_path):
    c, db, log = make(tmp_path)
    assert asyncio.run(c.fetch_mint_infos([T[k] for k in ORDER])) == 6
    assert sum(1 for h, _ in log if "solana" in h) == 1


def test_serial_creator_from_local_seen_tokens(tmp_path):
    c, db, log = make(tmp_path)
    for i in range(6):
        db.mark_new(TokenEvent("new_token", f"Spam{i}", "pumpportal", creator="SpamDev", seen_ts=NOW - 60))
    db.mark_new(TokenEvent("new_token", T["fresh_pump"], "pumpportal", creator="SpamDev", seen_ts=NOW, dev_buy_sol=9.0))
    r = asyncio.run(c.check(T["fresh_pump"], market=market(T["fresh_pump"])))
    assert {"serial_creator", "large_dev_buy"} <= codes(r)
    assert db.creator_blacklisted("SpamDev")


def test_due_mints_prioritises_new_then_active(tmp_path):
    c, db, log = make(tmp_path)
    asyncio.run(c.check(T["bonk"]))
    mk = {m: MarketData(m, vol_m5=v) for m, v in (("A", 1), ("B", 50), (T["bonk"], 999))}
    assert c.due_mints(["A", "B", T["bonk"]], mk, 10) == ["B", "A"]      # bonk fresh, B more active


def test_blacklist_not_double_counted_with_rug_history():
    r = ev("rc_new", local={"blacklisted": "RugCheck: creator history of rugged tokens"})
    assert "creator_rug_history" in codes(r) and "blacklisted_creator" not in codes(r)
    r2 = ev("fresh_pump", local={"blacklisted": "creator of a rugged token"})
    assert "blacklisted_creator" in codes(r2)


def test_cleanup_keeps_tracked_safety_reports(tmp_path):
    db = Database(tmp_path / "c.db")
    old = NOW - 10 * 3600
    for m in ("TRACKED", "GONE"):
        db.save_safety(sf.SafetyReport(m, old, old + 1, 90, sf.PASS).to_row())
        db.upsert_market(MarketData(m, updated_ts=NOW), tracking=(m == "TRACKED"))
    db.put_rugcheck("GONE", {"mint": "GONE"}, ts=old)
    out = db.cleanup(3, 24, 2, 2, now=NOW)
    assert out["safety"] == 1 and out["rc_cache"] == 1 and db.get_safety("TRACKED") and not db.get_safety("GONE")


# ---------------------------------------------------------------- strict rules + universes
def test_strict_defaults():
    assert S.safety_min_score == 70 and S.safety_top1_crit_pct == 30 and S.safety_max_impact_crit_pct == 15
    assert S.safety_require_rugcheck and S.safety_serial_creator == 3


def test_missing_rugcheck_is_unknown_not_pass():
    r = sf.evaluate(S, T["bonk"], now=NOW, mint_info=info("bonk"), market=market(T["bonk"]))
    assert r.verdict == sf.UNKNOWN and not r.passed
    loose = load_settings(env={"SAFETY_REQUIRE_RUGCHECK": "false"})
    assert sf.evaluate(loose, T["bonk"], now=NOW, mint_info=info("bonk"), market=market(T["bonk"])).verdict == sf.PASS


def test_sell_loss_and_liquidity_floor_are_critical():
    q = dict(quote("fresh_pump"), sell_value_usd=1.2)                       # $2 sells for $1.20
    r = ev("fresh_pump", market=market(T["fresh_pump"], liq=900), quote=q)
    assert {"sell_loss", "liquidity_too_low"} <= set(r.critical)


def test_large_universe_needs_deeper_liquidity():
    md = market(T["fresh_pump"], liq=20_000, mcap=2e6, status=AMM, universe="mcap")
    r = ev("fresh_pump", market=md)
    assert r.universe == "mcap" and any(f.code == "low_liquidity" and f.severity == sf.DANGER for f in r.flags)
    assert "thin_liquidity" not in codes(r)


def test_trade_ready_needs_fresh_sellable_quote():
    r = ev("fresh_pump", market=market(T["fresh_pump"]), quote=quote("fresh_pump"), quote_ts=NOW)
    assert r.passed and r.trade_ready(180, NOW) and not r.trade_ready(180, NOW + 181)
    assert not ev("fresh_pump", market=market(T["fresh_pump"])).trade_ready(180, NOW)


def test_every_critical_code_is_declared():
    r = ev("pyusd")
    assert set(r.critical) <= sf.CRITICAL_CODES


def test_schema_v3_universe_columns_and_members(tmp_path):
    db = Database(tmp_path / "u.db")
    for t in ("seen_tokens", "token_market", "collector_drops", "safety_reports"):
        cols = {r[1] for r in db.con.execute(f"PRAGMA table_info({t})")}
        assert "universe" in cols, t
    db.upsert_market(MarketData("BIG", universe="mcap", market_cap=5e6, updated_ts=NOW))
    assert db.get_market("BIG").universe == "mcap"
    db.upsert_universe_member("BIG", "mcap", rank=2, metric=5e6, source="dexscreener", now=NOW)
    db.upsert_universe_member("BIG", "volume", rank=1, metric=9e6, source="geckoterminal", now=NOW)
    assert sorted(db.universes_of("BIG")) == ["mcap", "volume"] and db.universe_mints("volume") == ["BIG"]
    db.save_safety(ev("usdc").to_row())
    assert db.get_safety(T["usdc"])["universe"] == "new"
