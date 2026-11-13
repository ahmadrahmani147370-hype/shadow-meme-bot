"""PHASE 4 - Coin safety checker (analysis only, never trades).

For one mint it combines:
  * Solana RPC getMultipleAccounts (jsonParsed, 100 mints per call; publicnode fallback):
    mint authority, freeze authority, decimals, supply, Token-2022 extensions.
  * RugCheck full report (cached in SQLite): risks, holders, insiders, LP lock, creator, rugged flag.
  * Local data: dev buy size from the scanner, tokens per creator (serial creator / spam blacklist),
    DexScreener/Jupiter market data and price snapshots from the collector (liquidity, fake volume, rug warning).
  * Jupiter quote token -> SOL for the paper trade size (sellability + price impact). Quotes share the
    keyless 30/min Jupiter bucket, so they run only on demand or for tokens near a signal.

Output: SafetyReport with a 0-100 safety sub-score, flags with severity and a CRITICAL set that forces
REJECT whatever the score is.

Interface for phase 5:
    checker.check(mint, quote=False|True) -> SafetyReport   (fetches what is stale, then evaluates)
    checker.get_report(mint)               -> SafetyReport | None (memory/DB, may be expired)
    checker.request_quote(mint)            -> queue a sellability quote (near a signal)
    checker.run(collector)                 -> background loop for every tracked token
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field

from .config import Settings
from .db import Database
from .models import AMM, MIGRATED, U_NEW, MarketData
from .net import UA, RateLimiter, RateLimitError, make_limiters, request_raw

PHASE = 4
IMPLEMENTED = True
log = logging.getLogger("shadow.safety")

RC_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
JUP_QUOTE = "https://api.jup.ag/swap/v1/quote"
WSOL = "So11111111111111111111111111111111111111112"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
RPC_BATCH = 100

# Wallets that hold supply for a protocol, not for a person (excluded from holder concentration).
# BwWK17...: system-owned wallet holding ~50% of every 2B-supply pump.fun token (pump.fun "Mayhem mode"
# agent supply; verified phase 4: ~69k SOL, owner = System Program, top holder of many fresh pump tokens).
KNOWN_INFRA_OWNERS = {
    "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s": "pump.fun mayhem agent",
}
INFRA_TYPES = {"AMM", "LOCKER"}

INFO, WARN, DANGER, CRITICAL = "info", "warn", "danger", "critical"
PENALTY = {INFO: 0, WARN: 5, DANGER: 15, CRITICAL: 100}
SPECIAL_PENALTY = {"serial_creator": 20, "lp_unlocked": 20, "mayhem_supply": 3}
# Every code that can be raised with CRITICAL severity (any one forces REJECT whatever the score).
CRITICAL_CODES = {
    # contract
    "mint_authority", "freeze_authority", "transfer_fee", "transfer_fee_authority", "permanent_delegate",
    "non_transferable", "transfer_hook", "transfer_hook_authority", "default_frozen", "pausable", "not_a_mint",
    # sellability
    "not_sellable", "extreme_price_impact", "sell_loss",
    # rug / holders / dev
    "rugged", "rug_warning", "extreme_holder_concentration", "extreme_insider_concentration", "dev_holds",
    "creator_rug_history", "blacklisted_creator", "serial_creator",
    # market
    "liquidity_too_low", "wash_volume", "few_makers"}
NO_ROUTE_CODES = {"NO_ROUTES_FOUND", "COULD_NOT_FIND_ANY_ROUTE", "TOKEN_NOT_TRADABLE", "ROUTE_NOT_FOUND"}
# RugCheck risks we compute ourselves (with infra excluded) or that are normal for fresh launches.
RC_SKIP = {"single holder ownership", "top 10 holders high ownership", "high holder concentration", "high ownership",
           "low liquidity", "low amount of lp providers", "mint authority still enabled",
           "freeze authority still enabled", "copycat token", "low amount of holders"}
PASS, REJECT, UNKNOWN = "PASS", "REJECT", "UNKNOWN"


@dataclass
class Flag:
    code: str
    severity: str
    detail: str = ""
    penalty: int = 0


@dataclass
class SafetyReport:
    mint: str
    checked_ts: float
    expires_ts: float
    score: int
    verdict: str                     # PASS | REJECT | UNKNOWN (missing RugCheck/RPC data: never tradable)
    critical: list[str] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    rpc_ts: float | None = None
    rc_ts: float | None = None
    quote_ts: float | None = None
    partial: bool = False
    universe: str = U_NEW

    @property
    def passed(self) -> bool:
        return self.verdict == PASS

    def trade_ready(self, quote_ttl_sec: float, now: float | None = None) -> bool:
        """What phase 5/6 must require before a paper BUY: PASS + a fresh, sellable Jupiter quote."""
        q = self.data.get("quote") or {}
        return self.passed and q.get("sellable") is True and self.quote_fresh(quote_ttl_sec, now)

    def reject_reasons(self) -> list[str]:
        if self.verdict != REJECT:
            return []
        return list(self.critical) or ["low_safety_score"]

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_ts

    def quote_fresh(self, ttl_sec: float, now: float | None = None) -> bool:
        return bool(self.quote_ts) and (now or time.time()) - self.quote_ts < ttl_sec

    def to_row(self) -> dict:
        return {"mint": self.mint, "checked_ts": self.checked_ts, "expires_ts": self.expires_ts, "score": self.score,
                "verdict": self.verdict, "critical_json": json.dumps(self.critical),
                "flags_json": json.dumps([asdict(f) for f in self.flags]), "data_json": json.dumps(self.data),
                "rpc_ts": self.rpc_ts, "rc_ts": self.rc_ts, "quote_ts": self.quote_ts, "partial": int(self.partial),
                "universe": self.universe}

    @classmethod
    def from_row(cls, r: dict) -> "SafetyReport":
        return cls(mint=r["mint"], checked_ts=r["checked_ts"], expires_ts=r["expires_ts"], score=r["score"],
                   verdict=r["verdict"], critical=json.loads(r["critical_json"] or "[]"),
                   flags=[Flag(**f) for f in json.loads(r["flags_json"] or "[]")],
                   data=json.loads(r["data_json"] or "{}"), rpc_ts=r["rpc_ts"], rc_ts=r["rc_ts"],
                   quote_ts=r["quote_ts"], partial=bool(r["partial"]), universe=r.get("universe") or U_NEW)

    def summary(self) -> str:
        worst = [f.code for f in self.flags if f.severity in (CRITICAL, DANGER)]
        return (f"{self.verdict:<7} score {self.score:>3}"
                + (f" critical={','.join(self.critical)}" if self.critical else "")
                + (f" danger={','.join(c for c in worst if c not in self.critical)}" if worst else "")
                + (" (partial)" if self.partial else ""))


# ---------------------------------------------------------------- pure parsers (unit-tested)
def _f(x):
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def parse_mint_account(value: dict | None) -> dict | None:
    """One getMultipleAccounts jsonParsed entry -> {program, mint_authority, freeze_authority, decimals, supply,
    extensions:{name: state}}. None if the account does not exist / is not a token mint."""
    if not value:
        return None
    data = value.get("data")
    if not isinstance(data, dict) or (data.get("parsed") or {}).get("type") != "mint":
        return {"program": value.get("owner"), "not_mint": True}
    info = data["parsed"].get("info") or {}
    return {"program": value.get("owner"), "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"), "decimals": info.get("decimals"),
            "supply": _f(info.get("supply")),
            "extensions": {e.get("extension"): e.get("state") or {} for e in info.get("extensions") or []}}


def mint_from_rugcheck(rc: dict) -> dict | None:
    """Fallback when RPC failed: RugCheck repeats the mint fields (token_extensions uses its own shape)."""
    tok = rc.get("token") or {}
    if not tok:
        return None
    ext = {}
    for k, v in (rc.get("token_extensions") or {}).items():
        if v in (None, False, {}):
            continue
        ext[k] = v if isinstance(v, dict) else {"enabled": v}
    tf = ext.get("transferFeeConfig")
    if tf is not None and "newerTransferFee" not in tf:
        pct = _f((rc.get("transferFee") or {}).get("pct")) or 0
        ext["transferFeeConfig"] = {"newerTransferFee": {"transferFeeBasisPoints": int(pct * 100)},
                                    "transferFeeConfigAuthority": tf.get("transferFeeConfigAuthority")}
    return {"program": rc.get("tokenProgram"), "mint_authority": tok.get("mintAuthority"),
            "freeze_authority": tok.get("freezeAuthority"), "decimals": tok.get("decimals"),
            "supply": _f(tok.get("supply")), "extensions": ext, "from": "rugcheck"}


def extension_flags(ext: dict) -> list[Flag]:
    out = []
    if "transferFeeConfig" in ext:
        st = ext["transferFeeConfig"] or {}
        bps = max(int(((st.get(k) or {}).get("transferFeeBasisPoints")) or 0)
                  for k in ("newerTransferFee", "olderTransferFee"))
        if bps > 0:
            out.append(Flag("transfer_fee", CRITICAL, f"Token-2022 transfer fee {bps / 100:.2f}%"))
        elif st.get("transferFeeConfigAuthority"):
            out.append(Flag("transfer_fee_authority", CRITICAL, "transfer fee is 0 but an authority can raise it"))
    if "permanentDelegate" in ext and (ext["permanentDelegate"] or {}).get("delegate"):
        out.append(Flag("permanent_delegate", CRITICAL, "permanent delegate can move/burn anyone's tokens"))
    if "nonTransferable" in ext:
        out.append(Flag("non_transferable", CRITICAL, "token is non-transferable"))
    if "transferHook" in ext:
        st = ext["transferHook"] or {}
        if st.get("programId"):
            out.append(Flag("transfer_hook", CRITICAL, f"transfer hook program {st['programId'][:8]}..."))
        elif st.get("authority"):
            out.append(Flag("transfer_hook_authority", CRITICAL, "no hook yet, but an authority can add one"))
    if "defaultAccountState" in ext:
        st = ext["defaultAccountState"] or {}
        if str(st.get("accountState", st.get("state", ""))).lower() == "frozen" or st.get("enabled") is True:
            out.append(Flag("default_frozen", CRITICAL, "new token accounts start frozen"))
    if "pausableConfig" in ext and ((ext["pausableConfig"] or {}).get("authority") or (ext["pausableConfig"] or {}).get("paused")):
        out.append(Flag("pausable", CRITICAL, "an authority can pause all transfers"))
    if "mintCloseAuthority" in ext and (ext["mintCloseAuthority"] or {}).get("closeAuthority"):
        out.append(Flag("mint_close_authority", WARN, "mint can be closed"))
    if "confidentialTransferMint" in ext:
        out.append(Flag("confidential_transfers", WARN, "confidential (hidden) transfers enabled"))
    for k in ("interestBearingConfig", "scaledUiAmountConfig"):
        if k in ext:
            out.append(Flag("ui_amount_changes", WARN, f"{k}: displayed balance can change"))
    return out


def trim_rugcheck(r: dict) -> dict:
    """Keep only what evaluate() needs (cache rows stay small on a phone)."""
    holders = (r.get("topHolders") or [])[:20]
    refs = set()
    for h in holders:
        refs.update((h.get("address"), h.get("owner")))
    markets = [{"marketType": m.get("marketType"), "pubkey": m.get("pubkey"), "liquidityA": m.get("liquidityA"),
                "liquidityB": m.get("liquidityB"), "lpLockedPct": (m.get("lp") or {}).get("lpLockedPct")}
               for m in (r.get("markets") or [])[:5]]
    for m in markets:
        refs.update((m["pubkey"], m["liquidityA"], m["liquidityB"]))
    known = {a: v for a, v in (r.get("knownAccounts") or {}).items() if a in refs}
    return {
        "mint": r.get("mint"), "token": r.get("token"), "tokenProgram": r.get("tokenProgram"),
        "token_extensions": r.get("token_extensions"), "transferFee": r.get("transferFee"),
        "risks": [{k: x.get(k) for k in ("name", "level", "value", "score", "description")} for x in r.get("risks") or []],
        "score": r.get("score"), "score_normalised": r.get("score_normalised"), "rugged": r.get("rugged"),
        "topHolders": [{k: h.get(k) for k in ("address", "owner", "pct", "insider")} for h in holders],
        "knownAccounts": known, "markets": markets, "totalMarketLiquidity": r.get("totalMarketLiquidity"),
        "totalLPProviders": r.get("totalLPProviders"), "totalHolders": r.get("totalHolders"),
        "creator": r.get("creator"), "creatorBalance": r.get("creatorBalance"),
        "creatorTokensCount": len(r.get("creatorTokens") or []) if "creatorTokensCount" not in r else r["creatorTokensCount"],
        "insiderNetworks": [{k: n.get(k) for k in ("size", "type", "tokenAmount", "currentHolding", "activeAccounts")}
                            for n in r.get("insiderNetworks") or []][:10],
        "graphInsidersDetected": r.get("graphInsidersDetected"), "launchpad": (r.get("launchpad") or {}).get("platform")
        if isinstance(r.get("launchpad"), dict) else r.get("launchpad"),
        "tokenMeta": {"mutable": (r.get("tokenMeta") or {}).get("mutable")}, "price": r.get("price"),
    }


def holder_concentration(rc: dict) -> dict:
    """Top-1 / top-10 % of supply held by real wallets (pools, lockers, bonding curves, protocol wallets excluded)."""
    known = rc.get("knownAccounts") or {}
    infra = {a for a, v in known.items() if (v or {}).get("type") in INFRA_TYPES}
    for m in rc.get("markets") or []:
        infra.update(x for x in (m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB")) if x)
    excluded, real, mayhem = [], [], 0.0
    for h in rc.get("topHolders") or []:
        a, o, pct = h.get("address"), h.get("owner"), _f(h.get("pct")) or 0.0
        if o in KNOWN_INFRA_OWNERS or a in KNOWN_INFRA_OWNERS:
            excluded.append((KNOWN_INFRA_OWNERS.get(o) or KNOWN_INFRA_OWNERS.get(a), pct))
            mayhem += pct
        elif a in infra or o in infra:
            excluded.append(((known.get(a) or known.get(o) or {}).get("name", "pool/locker"), pct))
        else:
            real.append(h)
    real.sort(key=lambda h: _f(h.get("pct")) or 0, reverse=True)
    pcts = [_f(h.get("pct")) or 0.0 for h in real]
    insider_top = sum(_f(h.get("pct")) or 0.0 for h in real if h.get("insider"))
    return {"top1_pct": round(pcts[0], 2) if pcts else 0.0, "top10_pct": round(sum(pcts[:10]), 2),
            "excluded": [(n, round(p, 2)) for n, p in excluded], "mayhem_pct": round(mayhem, 2),
            "insider_top_pct": round(insider_top, 2), "top1_owner": (real[0].get("owner") if real else None)}


def insider_stats(rc: dict) -> dict:
    supply = _f((rc.get("token") or {}).get("supply")) or 0
    nets = rc.get("insiderNetworks") or []
    held = sum(_f(n.get("currentHolding", n.get("tokenAmount"))) or 0 for n in nets)
    return {"insider_pct": round(100 * held / supply, 2) if supply else 0.0, "networks": len(nets),
            "largest_network": max((int(n.get("size") or 0) for n in nets), default=0),
            "graph_insiders": int(rc.get("graphInsidersDetected") or 0)}


def _slug(name: str) -> str:
    return "rc_" + "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")[:40]


def quote_amount_raw(size_usd: float, price_usd: float | None, decimals: int | None) -> int | None:
    if not price_usd or price_usd <= 0 or decimals is None:
        return None
    return max(1, int(size_usd / price_usd * (10 ** int(decimals))))


def parse_quote(status: int, body) -> dict:
    """Jupiter /swap/v1/quote -> {sellable, impact_pct, out_sol, route, error}."""
    if status == 200 and isinstance(body, dict) and body.get("outAmount"):
        labels = [((r.get("swapInfo") or {}).get("label")) for r in body.get("routePlan") or []]
        return {"sellable": True, "impact_pct": round(100 * (_f(body.get("priceImpactPct")) or 0.0), 3),
                "out_sol": int(body["outAmount"]) / 1e9, "route": "+".join(x for x in labels if x)[:60]}
    code = (body or {}).get("errorCode") if isinstance(body, dict) else None
    err = (body or {}).get("error") if isinstance(body, dict) else str(body)[:80]
    if status in (400, 404) and (code in NO_ROUTE_CODES or "route" in str(err).lower() or "tradable" in str(err).lower()):
        return {"sellable": False, "error": f"{code or status}: {err}"}
    return {"sellable": None, "error": f"HTTP {status}: {code or err}"}


# ---------------------------------------------------------------- evaluation (pure)
def evaluate(s: Settings, mint: str, *, now: float, mint_info: dict | None = None, rc: dict | None = None,
             quote: dict | None = None, market: MarketData | None = None, local: dict | None = None,
             rpc_ts: float | None = None, rc_ts: float | None = None, quote_ts: float | None = None) -> SafetyReport:
    local = local or {}
    flags: list[Flag] = []
    data: dict = {}
    add = flags.append

    # 1. authorities + Token-2022 extensions (RPC first, RugCheck copy as fallback)
    mi = mint_info if mint_info and not mint_info.get("not_mint") else (mint_from_rugcheck(rc) if rc else None)
    if mint_info and mint_info.get("not_mint"):
        add(Flag("not_a_mint", CRITICAL, f"account owner {str(mint_info.get('program'))[:8]} is not a token mint"))
    if mi:
        data.update(program="token-2022" if mi.get("program") == TOKEN_2022 else "spl-token",
                    decimals=mi.get("decimals"), supply=mi.get("supply"), authority_source=mi.get("from", "rpc"),
                    extensions=sorted(mi.get("extensions") or {}))
        if mi.get("mint_authority"):
            add(Flag("mint_authority", CRITICAL, f"mint authority active ({str(mi['mint_authority'])[:8]}...)"))
        if mi.get("freeze_authority"):
            add(Flag("freeze_authority", CRITICAL, f"freeze authority active ({str(mi['freeze_authority'])[:8]}...)"))
        flags.extend(extension_flags(mi.get("extensions") or {}))

    # 2. RugCheck report
    if rc:
        data.update(rc_score=rc.get("score_normalised"), holders=rc.get("totalHolders"),
                    rc_liquidity=rc.get("totalMarketLiquidity"), launchpad=rc.get("launchpad"))
        if rc.get("rugged"):
            add(Flag("rugged", CRITICAL, "RugCheck marks this token as rugged"))
        for r in rc.get("risks") or []:
            name, lvl = str(r.get("name") or ""), str(r.get("level") or "")
            low = name.lower()
            if low in RC_SKIP:
                continue
            if "lp unlocked" in low:
                add(Flag("lp_unlocked", DANGER, f"{name} {r.get('value') or ''}".strip()))
            elif "creator history of rugged" in low:
                add(Flag("creator_rug_history", CRITICAL, name))
            elif low == "mutable metadata":
                add(Flag("mutable_metadata", WARN, "metadata can still be changed"))
            elif lvl in ("danger", "warn"):
                add(Flag(_slug(name), DANGER if lvl == "danger" else WARN, f"RugCheck: {name} {r.get('value') or ''}".strip()))
        hc = holder_concentration(rc)
        ins = insider_stats(rc)
        data.update(top1_pct=hc["top1_pct"], top10_pct=hc["top10_pct"], excluded_holders=hc["excluded"], **ins)
        if hc["top1_pct"] >= s.safety_top1_crit_pct or hc["top10_pct"] >= s.safety_top10_crit_pct:
            add(Flag("extreme_holder_concentration", CRITICAL,
                     f"top1 {hc['top1_pct']:.1f}% / top10 {hc['top10_pct']:.1f}% of supply (pools excluded)"))
        elif hc["top1_pct"] >= s.safety_top1_danger_pct or hc["top10_pct"] >= s.safety_top10_danger_pct:
            add(Flag("holder_concentration", DANGER, f"top1 {hc['top1_pct']:.1f}% / top10 {hc['top10_pct']:.1f}%"))
        elif hc["top10_pct"] >= s.safety_top10_danger_pct * 0.6:
            add(Flag("holder_concentration", WARN, f"top10 {hc['top10_pct']:.1f}%"))
        if hc["mayhem_pct"]:
            add(Flag("mayhem_supply", WARN, f"{hc['mayhem_pct']:.1f}% held by the pump.fun mayhem agent (protocol wallet)"))
        ipct = max(ins["insider_pct"], hc["insider_top_pct"])
        if ipct >= s.safety_insider_crit_pct:
            add(Flag("extreme_insider_concentration", CRITICAL, f"insider wallets hold {ipct:.1f}%"))
        elif ipct >= s.safety_insider_danger_pct:
            add(Flag("insider_concentration", DANGER, f"insider wallets hold {ipct:.1f}%"))
        if ins["largest_network"] >= 10:
            add(Flag("insider_cluster", WARN, f"linked wallet cluster of {ins['largest_network']} accounts"))
        supply = _f((rc.get("token") or {}).get("supply"))
        cb = _f(rc.get("creatorBalance"))
        if supply and cb:
            dev_pct = 100 * cb / supply
            data["dev_hold_pct"] = round(dev_pct, 2)
            if dev_pct >= s.safety_dev_hold_crit_pct:
                add(Flag("dev_holds", CRITICAL, f"creator still holds {dev_pct:.1f}%"))
            elif dev_pct >= s.safety_dev_hold_danger_pct:
                add(Flag("dev_holds", DANGER, f"creator still holds {dev_pct:.1f}%"))
            elif dev_pct >= s.safety_dev_hold_danger_pct / 2:
                add(Flag("dev_holds", WARN, f"creator holds {dev_pct:.1f}%"))
        data["creator_tokens_rc"] = rc.get("creatorTokensCount") or 0
        locked = [m.get("lpLockedPct") for m in rc.get("markets") or [] if m.get("lpLockedPct") is not None]
        if locked:
            data["lp_locked_pct"] = round(max(locked), 1)   # unlocked LP is flagged by RugCheck's own risk

    # 3. dev wallet: serial creator / blacklist / dev buy
    creator = local.get("creator") or (rc or {}).get("creator")
    data["creator"] = creator
    n_local, n_rc = int(local.get("creator_tokens_24h") or 0), int((rc or {}).get("creatorTokensCount") or 0)
    has_history = any(f.code == "creator_rug_history" for f in flags)
    n_max = max(n_local, n_rc)
    if n_max >= s.safety_serial_creator:
        sev = CRITICAL if n_max >= s.safety_serial_creator_crit else DANGER
        add(Flag("serial_creator", sev, f"creator launched {n_local} tokens in 24h here, {n_rc} known to RugCheck"))
    if local.get("blacklisted") and not has_history and n_max < s.safety_serial_creator:
        add(Flag("blacklisted_creator", CRITICAL, f"creator blacklisted: {local['blacklisted']}"))
    dev_buy = _f(local.get("dev_buy_sol"))
    if dev_buy is not None:
        data["dev_buy_sol"] = dev_buy
        if dev_buy >= s.safety_dev_buy_warn_sol:
            add(Flag("large_dev_buy", WARN, f"dev bought {dev_buy:.2f} SOL at launch"))

    # 4. liquidity (coins in the mcap/volume universes need far deeper pools)
    universe = (market.universe if market and market.universe else None) or local.get("universe") or U_NEW
    data["universe"] = universe
    liq = market.liquidity_usd if market and market.liquidity_usd is not None else _f((rc or {}).get("totalMarketLiquidity"))
    data["liquidity_usd"] = liq
    if universe == U_NEW:
        crit_liq, min_liq, warn_liq = s.safety_crit_liq_usd, s.safety_min_liq_usd, s.safety_warn_liq_usd
    else:
        min_liq = s.safety_min_liq_usd_large
        crit_liq, warn_liq = min_liq / 5, min_liq * 2
    if liq is None:
        add(Flag("liquidity_unknown", DANGER, "no liquidity figure yet"))
    elif liq < crit_liq:
        add(Flag("liquidity_too_low", CRITICAL, f"liquidity ${liq:,.0f} < ${crit_liq:,.0f}"))
    elif liq < min_liq:
        add(Flag("low_liquidity", DANGER, f"liquidity ${liq:,.0f}"))
    elif liq < warn_liq:
        add(Flag("low_liquidity", WARN, f"liquidity ${liq:,.0f}"))
    if universe == U_NEW and market and liq and market.market_cap and market.migration_status in (AMM, MIGRATED):
        ratio = liq / market.market_cap
        data["liq_mcap"] = round(ratio, 3)
        if ratio < s.safety_min_liq_mcap:
            add(Flag("thin_liquidity", DANGER, f"liquidity only {100 * ratio:.1f}% of market cap"))

    # 5. fake volume heuristics
    if market:
        tx_h1 = (market.buys_h1 or 0) + (market.sells_h1 or 0)
        if liq and market.vol_h1:
            turnover = market.vol_h1 / liq
            data["turnover_h1"] = round(turnover, 1)
            if turnover >= 100:
                add(Flag("wash_volume", CRITICAL, f"1h volume {turnover:.0f}x liquidity"))
            elif turnover >= 30:
                add(Flag("high_turnover", WARN, f"1h volume {turnover:.0f}x liquidity"))
        if tx_h1 >= 100 and market.vol_h1 and market.vol_h1 / tx_h1 < 3:
            add(Flag("micro_trades", WARN, f"{tx_h1} trades averaging ${market.vol_h1 / tx_h1:.2f} (bot spam)"))
        if tx_h1 >= 200 and abs((market.buys_h1 or 0) - (market.sells_h1 or 0)) / tx_h1 < 0.03:
            add(Flag("balanced_wash", WARN, f"buys {market.buys_h1} vs sells {market.sells_h1} almost equal"))
        holders = (rc or {}).get("totalHolders")
        if holders is not None and holders < 20 and market.txns_h24 >= 150:
            add(Flag("few_makers", CRITICAL, f"{market.txns_h24} trades but only {holders} holders"))

    # 6. rug pull warning (collapse in liquidity or price while we watched it)
    snap = local.get("snapshots") or {}
    reasons = []
    if snap.get("liq_peak") and snap.get("liq_last") is not None and snap["liq_last"] < 0.5 * snap["liq_peak"]:
        reasons.append(f"liquidity -{100 * (1 - snap['liq_last'] / snap['liq_peak']):.0f}% from peak")
    if snap.get("peak") and snap.get("last") and (snap.get("n") or 0) >= 4 and snap["last"] < 0.2 * snap["peak"]:
        reasons.append(f"price -{100 * (1 - snap['last'] / snap['peak']):.0f}% from peak")
    if market and market.price_change_h1 is not None and market.price_change_h1 <= -70:
        reasons.append(f"1h price change {market.price_change_h1:.0f}%")
    codes = {f.code for f in flags}
    if "lp_unlocked" in codes and "dev_holds" in codes:
        reasons.append("unlocked LP + creator holding supply")
    if reasons:
        add(Flag("rug_warning", CRITICAL, "; ".join(reasons)))

    # 7. sellability (Jupiter quote for the paper trade size)
    if quote:
        data["quote"] = {k: quote.get(k) for k in ("sellable", "impact_pct", "out_sol", "sell_value_usd", "route", "error")
                         if quote.get(k) is not None}
        if quote.get("sellable") is False:
            add(Flag("not_sellable", CRITICAL, f"Jupiter has no sell route ({quote.get('error')})"))
        elif quote.get("sellable"):
            imp = quote.get("impact_pct") or 0.0
            if imp >= s.safety_max_impact_crit_pct:
                add(Flag("extreme_price_impact", CRITICAL, f"selling ${s.paper_trade_size:g} moves price {imp:.1f}%"))
            elif imp >= s.safety_max_impact_danger_pct:
                add(Flag("price_impact", DANGER, f"selling ${s.paper_trade_size:g} moves price {imp:.1f}%"))
            val = quote.get("sell_value_usd")
            if val is not None:
                loss = 100 * (1 - val / s.paper_trade_size)
                data["sell_loss_pct"] = round(loss, 1)
                if loss >= s.safety_max_sell_loss_pct:
                    add(Flag("sell_loss", CRITICAL, f"selling ${s.paper_trade_size:g} returns only ${val:.2f} ({loss:.0f}% lost)"))
        else:
            add(Flag("quote_failed", WARN, str(quote.get("error"))[:80]))
    else:
        add(Flag("sellability_unchecked", INFO, "no Jupiter sell quote yet (run near a signal)"))

    # score + verdict
    for f in flags:
        f.penalty = SPECIAL_PENALTY.get(f.code, PENALTY[f.severity]) if f.severity != CRITICAL else PENALTY[CRITICAL]
    critical = sorted({f.code for f in flags if f.severity == CRITICAL})
    score = max(0, 100 - sum(f.penalty for f in flags))
    have_rpc, have_rc = mi is not None, rc is not None
    partial = not (have_rpc and have_rc)
    if not have_rpc:
        flags.append(Flag("authority_unchecked", WARN, "mint/freeze authority not checked (RPC + RugCheck failed)", 0))
    if not have_rc:
        flags.append(Flag("rugcheck_unavailable", WARN, "no RugCheck report (holders/insiders unchecked)", 0))
    if critical:
        verdict = REJECT
    elif (not have_rpc and not have_rc) or (not have_rc and s.safety_require_rugcheck):
        verdict = UNKNOWN
    elif score < s.safety_min_score:
        verdict = REJECT
    else:
        verdict = PASS
    ttl = s.safety_reject_ttl_sec if verdict == REJECT else s.safety_ttl_sec
    if partial:
        ttl = min(ttl, 120)   # retry missing sources soon
    return SafetyReport(mint, now, now + ttl, score, verdict, critical, flags, data, rpc_ts, rc_ts, quote_ts, partial,
                        universe)


# ---------------------------------------------------------------- checker (I/O)
class SafetyChecker:
    def __init__(self, settings: Settings, db: Database, *, client=None, limiters=None, clock=time.time,
                 market_fn=None):
        self.s, self.db = settings, db
        self._client = client
        self.limiters = limiters if limiters is not None else make_limiters()
        for name, per_min in (("solana_rpc", 60), ("solana_rpc_fallback", 60), ("rugcheck", 50), ("jupiter", 25)):
            self.limiters.setdefault(name, RateLimiter(name, per_min))
        # safety's own pacing on top of the shared per-source queues
        self.rc_pace = RateLimiter("safety_rugcheck", max(1, settings.safety_rc_per_min))
        self.quote_pace = RateLimiter("safety_quotes", max(1, settings.safety_quotes_per_min or 1))
        self.clock = clock
        self.market_fn = market_fn or db.get_market
        self.reports: dict[str, SafetyReport] = {}
        self._mint_info: dict[str, tuple[float, dict | None]] = {}
        self._quotes: dict[str, tuple[float, dict]] = {}
        self.quote_queue: deque[str] = deque()
        self.priority: set[str] = set()              # signal candidates waiting for a safety check
        self.near_fn = None                          # set by the signal engine (phase 5): real signal proximity
        self._rc_missing: dict[str, float] = {}      # RugCheck could not build a report (negative cache)
        self._quote_tried: dict[str, float] = {}   # last attempt (also failed/no-price ones)
        self.stats: Counter = Counter()
        self.reject_reasons: Counter = Counter()
        self.errors: deque = deque(maxlen=50)
        self._stop = asyncio.Event()

    # ---- public API
    def get_report(self, mint: str) -> SafetyReport | None:
        r = self.reports.get(mint)
        if r is None:
            row = self.db.get_safety(mint)
            if row:
                r = self.reports[mint] = SafetyReport.from_row(row)
        return r

    def request_quote(self, mint: str, priority: bool = False) -> None:
        tried = self._quote_tried.get(mint)
        if tried and self.clock() - tried < (60 if priority else self.s.safety_quote_ttl_sec):
            return
        if mint in self.quote_queue:
            if priority:
                self.quote_queue.remove(mint)
                self.quote_queue.appendleft(mint)
            return
        if priority:
            self.quote_queue.appendleft(mint)
        else:
            self.quote_queue.append(mint)

    def prioritize(self, mint: str) -> None:
        """A signal candidate needs its safety report now: checked before everything else."""
        self.priority.add(mint)

    async def check(self, mint: str, *, quote: bool = False, force: bool = False,
                    market: MarketData | None = None) -> SafetyReport:
        await self._ensure_client()
        now = self.clock()
        need_rpc = force or self._mint_info_stale(mint, now)
        if need_rpc:
            await self.fetch_mint_infos([mint])
        rc, rc_ts = await self.rugcheck(mint, force=force)
        if quote:
            await self.quote(mint, market=market, force=force)
        return self._evaluate_and_save(mint, rc=rc, rc_ts=rc_ts, market=market)

    def snapshot(self) -> dict:
        return {**dict(self.stats), "reject_reasons": dict(self.reject_reasons), "quote_queue": len(self.quote_queue),
                "last_errors": list(self.errors)[-5:]}

    # ---- sources
    async def _ensure_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=self.s.http_timeout_sec, follow_redirects=True,
                                             headers={"User-Agent": UA, "Accept": "application/json"})
            self._own_client = True

    async def aclose(self):
        if getattr(self, "_own_client", False) and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _err(self, text: str) -> None:
        self.stats["errors"] += 1
        self.errors.append(f"{time.strftime('%H:%M:%S')} {text}")
        log.warning(text)

    def _mint_info_stale(self, mint: str, now: float) -> bool:
        got = self._mint_info.get(mint)
        if not got:
            return True
        ts, info = got
        risky = bool(info and (info.get("mint_authority") or info.get("freeze_authority")))
        ttl = self.s.safety_reject_ttl_sec if risky else self.s.safety_ttl_sec
        return info is None and now - ts > 60 or now - ts > ttl

    async def fetch_mint_infos(self, mints: list[str]) -> int:
        """getMultipleAccounts in batches of 100: primary RPC, publicnode fallback."""
        n = 0
        for i in range(0, len(mints), RPC_BATCH):
            chunk = mints[i:i + RPC_BATCH]
            body = {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                    "params": [chunk, {"encoding": "jsonParsed", "commitment": "confirmed"}]}
            values = None
            for url, lim in ((self.s.solana_rpc_url, "solana_rpc"), (self.s.solana_rpc_fallback, "solana_rpc_fallback")):
                if not url:
                    continue
                try:
                    status, res = await request_raw(self._client, self.limiters[lim], "POST", url, json_body=body)
                    self.stats["rpc_calls"] += 1
                    if lim != "solana_rpc":
                        self.stats["rpc_fallback_calls"] += 1
                    vals = ((res or {}).get("result") or {}).get("value") if isinstance(res, dict) else None
                    if status == 200 and isinstance(vals, list) and len(vals) == len(chunk):
                        values = vals
                        break
                    self._err(f"{lim} HTTP {status}: {str((res or {}).get('error') if isinstance(res, dict) else res)[:80]}")
                except asyncio.CancelledError:
                    raise
                except RateLimitError:
                    self._err(f"{lim} 429")
                except Exception as e:
                    self._err(f"{lim} failed: {type(e).__name__}: {str(e)[:80]}")
            now = self.clock()
            if values is None:
                continue
            for m, v in zip(chunk, values):
                self._mint_info[m] = (now, parse_mint_account(v))
                n += 1
        return n

    async def rugcheck(self, mint: str, force: bool = False) -> tuple[dict | None, float | None]:
        if not force:
            row = self.db.con.execute("SELECT ts, report_json FROM rugcheck_cache WHERE mint=?", (mint,)).fetchone()
            if row and self.clock() - row[0] <= self.s.safety_ttl_sec:
                self.stats["rc_cache_hits"] += 1
                return json.loads(row[1]), row[0]
        miss = self._rc_missing.get(mint)
        if miss and not force and self.clock() - miss < self.s.safety_reject_ttl_sec:
            self.stats["rc_skipped_missing"] += 1
            return None, None
        await self.rc_pace.acquire()
        headers = {"X-API-KEY": self.s.rugcheck_api_key} if self.s.rugcheck_api_key else None
        try:
            status, body = await request_raw(self._client, self.limiters["rugcheck"], "GET",
                                             RC_REPORT.format(mint=mint), headers=headers)
            self.stats["rc_calls"] += 1
        except asyncio.CancelledError:
            raise
        except RateLimitError:
            self._err("rugcheck 429 - queue paused")
            return None, None
        except Exception as e:
            self._err(f"rugcheck failed: {type(e).__name__}: {str(e)[:80]}")
            return None, None
        if status != 200 or not isinstance(body, dict) or not body.get("mint"):
            self.stats["rc_missing"] += 1
            if 400 <= status < 500:
                self._rc_missing[mint] = self.clock()   # e.g. "unable to generate report": retry after 30 min
            if status >= 500:
                self._err(f"rugcheck HTTP {status}")
            return None, None
        rep, now = trim_rugcheck(body), self.clock()
        self.db.put_rugcheck(mint, rep, now)
        return rep, now

    def _decimals(self, mint: str) -> int | None:
        info = (self._mint_info.get(mint) or (0, None))[1]
        if info and info.get("decimals") is not None:
            return int(info["decimals"])
        row = self.db.con.execute("SELECT report_json FROM rugcheck_cache WHERE mint=?", (mint,)).fetchone()
        if row:
            return ((json.loads(row[0]).get("token") or {}).get("decimals"))
        return None

    async def quote(self, mint: str, *, market: MarketData | None = None, force: bool = False) -> dict | None:
        """Sell quote token -> SOL for the paper trade size. Uses 1 of the 30/min keyless Jupiter calls."""
        now = self.clock()
        got = self._quotes.get(mint)
        if got and not force and now - got[0] < self.s.safety_quote_ttl_sec:
            return got[1]
        self._quote_tried[mint] = now
        market = market or self.market_fn(mint)
        price = market.price_usd if market else None
        dec = self._decimals(mint)
        if dec is None:
            await self.fetch_mint_infos([mint])
            dec = self._decimals(mint)
        amount = quote_amount_raw(self.s.paper_trade_size, price, dec)
        if amount is None:
            self.stats["quote_skipped_no_price"] += 1
            return None
        if self.s.safety_quotes_per_min <= 0:
            return None
        await self.quote_pace.acquire()
        headers = {"x-api-key": self.s.jupiter_api_key} if self.s.jupiter_api_key else None
        params = {"inputMint": mint, "outputMint": WSOL, "amount": str(amount),
                  "slippageBps": str(int(self.s.paper_slippage_pct * 100)), "restrictIntermediateTokens": "true"}
        try:
            status, body = await request_raw(self._client, self.limiters["jupiter"], "GET", JUP_QUOTE,
                                             params=params, headers=headers)
            self.stats["quotes"] += 1
        except asyncio.CancelledError:
            raise
        except RateLimitError:
            self._err("jupiter quote 429 - queue paused")
            return None
        except Exception as e:
            self._err(f"jupiter quote failed: {type(e).__name__}: {str(e)[:80]}")
            return None
        q = parse_quote(status, body)
        if q.get("sellable") and market and market.price_usd and market.price_native and market.quote_symbol in ("SOL", "WSOL"):
            sol_usd = market.price_usd / market.price_native
            q["sell_value_usd"] = round(q["out_sol"] * sol_usd, 4)
        if q.get("sellable") is False:
            self.stats["quote_no_route"] += 1
        self._quotes[mint] = (self.clock(), q)
        return q

    # ---- evaluation + storage
    def _local(self, mint: str, rc: dict | None) -> dict:
        seen = self.db.get_seen(mint) or {}
        creator = seen.get("creator") or (rc or {}).get("creator")
        now = self.clock()
        out = {"creator": creator, "dev_buy_sol": seen.get("dev_buy_sol"),
               "creator_tokens_24h": self.db.creator_token_count(creator, now - 86400) if creator else 0,
               "snapshots": self.db.snapshot_extremes(mint)}
        bl = self.db.creator_blacklisted(creator) if creator else None
        if bl:
            out["blacklisted"] = bl["reason"]
        return out

    def _evaluate_and_save(self, mint: str, *, rc=None, rc_ts=None, market=None) -> SafetyReport:
        now = self.clock()
        mi_ts, mi = self._mint_info.get(mint) or (None, None)
        q_ts, q = self._quotes.get(mint) or (None, None)
        if q_ts and now - q_ts >= self.s.safety_quote_ttl_sec:
            q_ts, q = None, None
        if rc is None:
            row = self.db.con.execute("SELECT ts, report_json FROM rugcheck_cache WHERE mint=?", (mint,)).fetchone()
            if row and now - row[0] <= self.s.safety_ttl_sec:
                rc, rc_ts = json.loads(row[1]), row[0]
        market = market or self.market_fn(mint)
        local = self._local(mint, rc)
        rep = evaluate(self.s, mint, now=now, mint_info=mi, rc=rc, quote=q, market=market, local=local,
                       rpc_ts=mi_ts if mi else None, rc_ts=rc_ts, quote_ts=q_ts)
        self._learn_blacklist(rep, local)
        prev = self.reports.get(mint)
        self.reports[mint] = rep
        self.db.save_safety(rep.to_row())
        self.stats["checked"] += 1
        if prev is None:
            self.stats["checked_unique"] += 1
            self.stats[{PASS: "passed", REJECT: "rejected", UNKNOWN: "unknown"}[rep.verdict]] += 1
            for r in rep.reject_reasons():
                self.reject_reasons[r] += 1
        elif prev.verdict != rep.verdict:
            self.stats[f"verdict_changed_{prev.verdict}_to_{rep.verdict}"] += 1
        return rep

    def _learn_blacklist(self, rep: SafetyReport, local: dict) -> None:
        creator = rep.data.get("creator")
        if not creator or local.get("blacklisted"):
            return
        codes = {f.code for f in rep.flags}
        reason = None
        if "rugged" in codes:
            reason = "creator of a rugged token"
        elif "creator_rug_history" in codes:
            reason = "RugCheck: creator history of rugged tokens"
        elif "serial_creator" in codes:
            reason = f"serial creator ({max(local.get('creator_tokens_24h') or 0, rep.data.get('creator_tokens_rc') or 0)} tokens)"
        if reason:
            self.db.blacklist_creator(creator, reason, local.get("creator_tokens_24h"))
            self.stats["creators_blacklisted"] += 1

    # ---- background loop
    def due_mints(self, mints: list[str], markets: dict[str, MarketData], limit: int) -> list[str]:
        now = self.clock()
        new, stale = [], []
        first = [m for m in mints if m in self.priority]
        for m in mints:
            if m in self.priority:
                continue
            r = self.get_report(m)
            if r is None:
                new.append(m)
            elif r.expired(now):
                stale.append(m)
        key = lambda m: (markets[m].vol_m5 if markets.get(m) else 0.0)  # noqa: E731
        new.sort(key=key, reverse=True)
        stale.sort(key=key, reverse=True)
        return (first + new + stale)[:limit]

    def near_signal(self, md: MarketData | None, rep: SafetyReport | None, mint: str | None = None) -> bool:
        """Quote only tokens near a signal. With the phase-5 engine attached this is its real proximity
        (a strategy setup with score >= QUOTE_NEAR_SCORE); standalone it falls back to active, buy-heavy
        tokens that passed (used by --debug safety-live)."""
        if not (rep and rep.passed):
            return False
        if self.near_fn is not None:
            return self.near_fn(mint or rep.mint, md, rep)
        return bool(md and md.vol_m5 >= self.s.safety_quote_min_vol5m and (md.buys_m5 or 0) > (md.sells_m5 or 0))

    async def _check_loop(self, collector) -> None:
        while not self._stop.is_set():
            mints = collector.tracked_mints()
            markets = {m: collector.get_market(m) for m in mints}
            due = self.due_mints(mints, markets, limit=max(1, self.s.safety_rc_per_min))
            if not due:
                await self._sleep(2)
                continue
            now = self.clock()
            await self.fetch_mint_infos([m for m in due if self._mint_info_stale(m, now)])
            for m in due:
                if self._stop.is_set():
                    break
                try:
                    rc, rc_ts = await self.rugcheck(m)
                    rep = self._evaluate_and_save(m, rc=rc, rc_ts=rc_ts, market=markets.get(m))
                    self.priority.discard(m)
                    if self.on_report:
                        self.on_report(rep, markets.get(m))
                    if not rep.critical and self.near_signal(markets.get(m), rep) \
                            and not rep.quote_fresh(self.s.safety_quote_ttl_sec, self.clock()):
                        self.request_quote(m)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._err(f"check {m[:8]}: {type(e).__name__}: {str(e)[:100]}")

    async def _quote_loop(self, collector) -> None:
        while not self._stop.is_set():
            if not self.quote_queue or self.s.safety_quotes_per_min <= 0:
                # also re-quote passing, active tokens whose quote expired
                for m in collector.tracked_mints():
                    rep = self.reports.get(m)
                    if rep and not rep.critical and self.near_signal(collector.get_market(m), rep) \
                            and not rep.quote_fresh(self.s.safety_quote_ttl_sec, self.clock()):
                        self.request_quote(m)
                if not self.quote_queue:
                    await self._sleep(3)
                    continue
            m = self.quote_queue.popleft()
            await asyncio.sleep(0)
            try:
                md = collector.get_market(m)
                q = await self.quote(m, market=md)
                if q is not None:
                    rep = self._evaluate_and_save(m, market=md)
                    if self.on_report:
                        self.on_report(rep, md)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._err(f"quote {m[:8]}: {type(e).__name__}: {str(e)[:100]}")

    on_report = None

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self, collector) -> None:
        await self._ensure_client()
        try:
            await asyncio.gather(self._check_loop(collector), self._quote_loop(collector))
        finally:
            await self.aclose()

    async def stop(self) -> None:
        self._stop.set()


__all__ = ["SafetyChecker", "SafetyReport", "Flag", "evaluate", "parse_mint_account", "extension_flags",
           "holder_concentration", "insider_stats", "trim_rugcheck", "parse_quote", "quote_amount_raw",
           "CRITICAL_CODES", "KNOWN_INFRA_OWNERS", "PASS", "REJECT", "UNKNOWN"]
