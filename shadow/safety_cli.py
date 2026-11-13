"""Safety debug tools.

    python main.py --debug safety <mint>             # one full check incl. Jupiter sell quote
    python main.py --debug safety-live [seconds]     # scanner + collector + safety checker live test
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import Counter

from .collect_cli import _cpu_mem, _money
from .collector import DS_TOKENS, Collector, group_pairs, market_from_pairs, standard_quote
from .config import Settings
from .db import Database
from .net import UA, fetch_json, limiter_stats, make_limiters
from .safety import CRITICAL, DANGER, WARN, SafetyChecker, SafetyReport
from .scanner import Scanner

ICON = {CRITICAL: "⛔", DANGER: "🔴", WARN: "🟡", "info": "ℹ️"}


def _print_report(rep: SafetyReport, md=None) -> None:
    print(f"\n{rep.mint}  {(md.symbol if md else '') or ''}")
    print(f"  verdict {rep.verdict}   safety score {rep.score}/100   partial={rep.partial}")
    if rep.critical:
        print(f"  CRITICAL (forces reject): {', '.join(rep.critical)}")
    for f in sorted(rep.flags, key=lambda f: -f.penalty):
        print(f"  {ICON.get(f.severity, '-')} {f.severity:<8} {f.code:<30} -{f.penalty:<3} {f.detail}")
    d = rep.data
    keys = ("program", "decimals", "authority_source", "extensions", "rc_score", "holders", "top1_pct", "top10_pct",
            "excluded_holders", "lp_locked_pct", "insider_pct", "largest_network", "dev_hold_pct", "creator", "creator_tokens_rc",
            "dev_buy_sol", "liquidity_usd", "liq_mcap", "turnover_h1", "launchpad", "quote")
    for k in keys:
        if d.get(k) not in (None, [], ""):
            print(f"  {k:<18} {d[k]}")


async def run_safety_one(s: Settings, mint: str) -> int:
    import httpx

    db = Database(s.path(s.db_path))
    limiters = make_limiters()
    async with httpx.AsyncClient(timeout=s.http_timeout_sec, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept": "application/json"}) as client:
        md = None
        try:
            pairs = await fetch_json(client, limiters["dexscreener"], DS_TOKENS + mint)
            good = [p for p in group_pairs(pairs, [mint]).get(mint, []) if standard_quote(p)]
            if good:
                md = market_from_pairs(mint, good, time.time(), launchpad_hint=mint.endswith("pump"))
        except Exception as e:
            print(f"(DexScreener failed: {type(e).__name__}; liquidity/volume checks will use RugCheck only)")
        if md and md.liquidity_usd is None:
            with contextlib.suppress(Exception):
                j = await fetch_json(client, limiters["jupiter"], "https://api.jup.ag/price/v3", params={"ids": mint})
                liq = (j or {}).get(mint, {}).get("liquidity")
                if liq is not None:
                    md.liquidity_usd, md.liquidity_source = float(liq), "jupiter"
        chk = SafetyChecker(s, db, client=client, limiters=limiters)
        rep = await chk.check(mint, quote=True, force=True, market=md)
        if md:
            print(f"market: {md.dex_id} {md.migration_status} price ${md.price_usd} mcap {_money(md.market_cap)} "
                  f"liq {_money(md.liquidity_usd)} ({md.liquidity_source}) vol1h {_money(md.vol_h1)}")
        _print_report(rep, md)
        print(f"\nAPI calls: {limiter_stats(limiters)}  errors: {chk.stats.get('errors', 0)}")
    db.close()
    return 0 if rep.verdict != "UNKNOWN" else 1


def _db_bytes(path: str, wal: bool = True) -> int:
    parts = (path, path + "-wal", path + "-shm") if wal else (path,)
    return sum(os.path.getsize(p) for p in parts if os.path.exists(p))


async def run_safety_live(s: Settings, seconds: float = 300, quiet: bool = False) -> dict:
    path = str(s.path(s.db_path))
    db = Database(path)
    limiters = make_limiters()
    sc = Scanner(s, db, limiters=limiters)
    col = Collector(s, db, limiters=limiters)
    chk = SafetyChecker(s, db, limiters=limiters, market_fn=col.get_market)
    seen_verdict: dict[str, str] = {}

    def on_report(rep, md):
        if quiet or seen_verdict.get(rep.mint) == (rep.verdict, bool(rep.quote_ts)):
            return
        seen_verdict[rep.mint] = (rep.verdict, bool(rep.quote_ts))
        q = rep.data.get("quote") or {}
        qtxt = (f" sell-impact {q.get('impact_pct')}%" if q.get("sellable") else
                (" NOT SELLABLE" if q.get("sellable") is False else ""))
        print(f"{time.strftime('%H:%M:%S')} {'✅' if rep.passed else '⛔'} {((md.symbol if md else '') or '?')[:10]:<10} "
              f"{rep.mint[:8]}… {rep.summary()}{qtxt}", flush=True)

    chk.on_report = on_report
    db0, counts0 = _db_bytes(path, wal=False), db.table_counts()
    print(f"SHADOW MEME safety live test - {seconds:.0f}s; MAX_TRACKED {s.max_tracked}, RugCheck {s.safety_rc_per_min}/min, "
          f"quotes {s.safety_quotes_per_min}/min. DB {db0 / 1e6:.2f} MB at start.")
    wall0 = time.time()
    cpu_prev, t_prev = _cpu_mem()[0], wall0
    samples = []
    tasks = [asyncio.create_task(col.run(sc.stream())), asyncio.create_task(chk.run(col))]

    async def status():
        nonlocal cpu_prev, t_prev
        while True:
            await asyncio.sleep(60)
            cpu, _, cur = _cpu_mem()
            now = time.time()
            pct = 100 * (cpu - cpu_prev) / (now - t_prev)
            cpu_prev, t_prev = cpu, now
            row = {"t": round(now - wall0), "tracked": len(col.tracked), "cpu_pct": round(pct, 1),
                   "rss_mb": round((cur or 0) / 1024, 1), "db_mb": round(_db_bytes(path, wal=False) / 1e6, 2),
                   "wal_mb": round((_db_bytes(path) - _db_bytes(path, wal=False)) / 1e6, 2),
                   "checked": chk.stats["checked_unique"]}
            samples.append(row)
            print(f"--- {time.strftime('%H:%M:%S')} tracked {row['tracked']} (deferred {len(col.deferred)}) | "
                  f"checked {row['checked']} pass {chk.stats['passed']} reject {chk.stats['rejected']} | "
                  f"quotes {chk.stats['quotes']} | CPU {row['cpu_pct']}% RSS {row['rss_mb']} MB DB {row['db_mb']} MB "
                  f"(+WAL {row['wal_mb']} MB) | API {sum(v['calls'] for v in limiter_stats(limiters).values())} calls",
                  flush=True)

    tasks.append(asyncio.create_task(status()))
    try:
        await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await chk.stop()
        await col.stop()
        await sc.stop()
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
    wall = time.time() - wall0
    cpu, peak_kb, cur_kb = _cpu_mem()
    counts = db.table_counts()
    db.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db1 = _db_bytes(path, wal=False)
    flags = Counter()
    for r in chk.reports.values():
        for f in r.flags:
            if f.severity in (CRITICAL, DANGER):
                flags[f.code] += 1
    snap = {"safety": chk.snapshot(), "collector": col.snapshot(), "scanner": sc.snapshot(),
            "api": limiter_stats(limiters), "safety_pacing": {"rugcheck": chk.rc_pace.calls, "quotes": chk.quote_pace.calls},
            "cpu_s": round(cpu, 1), "cpu_pct": round(100 * cpu / wall, 1), "peak_rss_mb": round(peak_kb / 1024, 1),
            "rss_mb": round((cur_kb or 0) / 1024, 1), "db_mb_start": round(db0 / 1e6, 2), "db_mb_end": round(db1 / 1e6, 2),
            "tables_start": counts0, "tables_end": counts, "samples": samples, "flag_counts": dict(flags.most_common())}
    st = snap["safety"]
    print("\n== safety ==")
    for k in ("checked_unique", "checked", "passed", "rejected", "unknown", "rc_calls", "rc_cache_hits", "rc_missing",
              "rpc_calls", "rpc_fallback_calls", "quotes", "quote_no_route", "quote_skipped_no_price",
              "creators_blacklisted", "errors"):
        print(f"  {k:<24} {st.get(k, 0)}")
    print(f"  rejected by reason       {st['reject_reasons']}")
    print(f"  danger/critical flags    {snap['flag_counts']}")
    c = snap["collector"]
    print("== collector ==")
    print(f"  tracked now {c['tracked']} (max {c.get('tracked_max', 0)}), tracked total {c.get('tracked_total', 0)}, "
          f"deferred {c['deferred']}, requeued {c.get('requeued_from_full', 0)}, drops {c['drops']}")
    print("== API calls per source (429s) ==")
    for k, v in snap["api"].items():
        print(f"  {k:<22} {v['calls']:>5}  ({v['429']} x 429)  = {60 * v['calls'] / wall:.1f}/min")
    print(f"== resources == CPU {snap['cpu_s']}s over {wall:.0f}s ({snap['cpu_pct']}% avg), RSS now {snap['rss_mb']} MB, "
          f"peak {snap['peak_rss_mb']} MB")
    print(f"== database (after WAL checkpoint) == {snap['db_mb_start']} MB -> {snap['db_mb_end']} MB "
          f"(+{(db1 - db0) / 1e6 / (wall / 3600):.1f} MB/hour at this load)")
    print(f"  tables {counts}")
    for e in list(st["last_errors"]) + list(c["last_errors"]) + list(snap["scanner"]["last_errors"]):
        print(f"  error: {e}")
    db.close()
    return snap
