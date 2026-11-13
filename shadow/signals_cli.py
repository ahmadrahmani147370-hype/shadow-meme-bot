"""`python main.py --debug signals [seconds]` - everything live: scanner, new-launch collector, MCAP/TOP
VOLUME universes, safety checker, signal engine and judge. Prints signals and verdicts per universe."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import Counter

from .collect_cli import _cpu_mem, _money
from .collector import Collector
from .config import Settings
from .db import Database
from .judge import Judge
from .net import limiter_stats, make_limiters
from .safety import SafetyChecker
from .safety_cli import _db_bytes
from .scanner import Scanner
from .signals import SignalEngine, Sources
from .universes import UniverseCollector

ICON = {"BUY_WATCH": "🟢", "SELL": "🔴", "HOLD": "⏸️", "REJECT": "⛔"}


def _print(row: dict, verdict) -> None:
    t = time.strftime("%H:%M:%S")
    head = (f"{t} {ICON.get(row['alert_type'], '-')} {row['alert_type']:<9} [{row['universe']:<6}] "
            f"{(row.get('symbol') or '?')[:10]:<10} {row['mint'][:8]}… {row.get('strategy') or '-':<18} "
            f"score {row.get('score') or 0:>5.1f} {row.get('class') or ''}")
    if row["alert_type"] == "BUY_WATCH":
        dets = ",".join(json.loads(row.get("detectors_json") or "{}"))
        head += f" | {dets} | entry {row['entry']:.6g} SL {row['sl']:.6g} TP {row['tp']:.6g} {row['time_stop_min']}m"
        if verdict is not None:
            rs = "; ".join(f"{r['code']}: {r['detail']}" for r in verdict.reasons if r["kind"] in ("hard", "soft"))
            head += f"\n          JUDGE {verdict.verdict} conf {verdict.confidence:.2f} - {rs or 'no objections'}"
    else:
        head += f" | {row.get('reason')}"
    print(head, flush=True)


def _print_trade(kind: str, p: dict) -> None:
    t = time.strftime("%H:%M:%S")
    if kind == "OPEN":
        print(f"{t} 💰 PAPER OPEN  #{p['id']} [{p['universe']}] {p['symbol'] or p['mint'][:8]} {p['strategy']} "
              f"${p['size_usd']:.2f} @ {p['entry_price']:.6g} (market {p['market_price']:.6g}, slippage "
              f"{p['entry_slippage_pct']:.2f}%, fee ${p['buy_fee']:.2f}) SL {p['sl']:.6g} TP {p['tp']:.6g}", flush=True)
    else:
        print(f"{t} 💵 PAPER CLOSE #{p['id']} [{p['universe']}] {p['symbol'] or p['mint'][:8]} {p['exit_reason']} "
              f"@ {p['exit_price']:.6g} (market {p['exit_market_price']:.6g}, slippage {p['exit_slippage_pct']:.2f}%) "
              f"P&L after fees ${p['pnl_usd']:+.2f} ({p['pnl_pct']:+.1f}%)", flush=True)


async def run_signals(s: Settings, seconds: float = 600, quiet_rejects: bool = False, with_paper: bool = False) -> dict:
    path = str(s.path(s.db_path))
    db = Database(path, busy_timeout_ms=s.db_busy_timeout_ms)
    db.acquire_writer_lock()  # single writer: a second bot on the same DB stops here (WriterLocked)
    from .maintenance import Maintenance, consistency, file_bytes, integrity_check, projection
    maint = Maintenance(s, db)
    maint_stop = asyncio.Event()
    cons0 = consistency(db)
    if not cons0["ok"]:
        print(f"!! database consistency problems at start: {cons0['problems']}", flush=True)
    db.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    fb0 = file_bytes(db)
    limiters = make_limiters()
    sc = Scanner(s, db, limiters=limiters)
    col = Collector(s, db, limiters=limiters)
    uni = UniverseCollector(s, db, limiters=limiters, skip_fn=lambda m: m in col.tracked) if s.universes_enabled else None
    src = Sources(col, uni)
    chk = SafetyChecker(s, db, limiters=limiters, market_fn=src.get_market)
    judge = Judge(s, db)
    paper = learner = client = None
    if with_paper:
        import httpx

        from .learning import Learner
        from .net import UA
        from .paper import PaperEngine
        client = httpx.AsyncClient(timeout=s.http_timeout_sec, headers={"User-Agent": UA, "Accept": "application/json"})
        paper = PaperEngine(s, db, src=src, checker=chk, limiters=limiters, client=client, on_trade=_print_trade)
        learner = Learner(s, db, judge=judge)
    eng = SignalEngine(s, db, src, checker=chk, judge=judge, paper=paper,
                       on_signal=lambda row, v: None if (quiet_rejects and row["alert_type"] == "REJECT") else _print(row, v))
    db0 = _db_bytes(path, wal=False)
    print(f"SHADOW MEME signals live test - {seconds:.0f}s | new-launch cap {s.max_tracked} | MCAP >= "
          f"${s.universe_mcap_min:,.0f} (max {s.universe_mcap_max}) | TOP VOLUME top {s.universe_volume_top_n} 24h+1h "
          f"(max {s.universe_volume_max}) | strategies v"
          + ", ".join(f"{k}={v.version}" for k, v in eng.strategies.items()) + " | PAPER/ANALYSIS ONLY", flush=True)
    wall0 = time.time()
    cpu_prev, t_prev = _cpu_mem()[0], wall0
    samples = []
    tasks = [asyncio.create_task(col.run(sc.stream())), asyncio.create_task(chk.run(src)),
             asyncio.create_task(eng.run())]
    if uni:
        tasks.append(asyncio.create_task(uni.run()))
    learn_runs = []

    async def learn_loop():
        while True:
            res = learner.run_once()
            learn_runs.append(res)
            acts = [r for r in res if r.get("action") in ("activate", "rollback")]
            print(f"--- {time.strftime('%H:%M:%S')} learning: " + ("; ".join(f"{r['target']} {r['action']}: {r.get('reason')}"
                  for r in acts) if acts else ", ".join(sorted({f"{r['target']}={r['action']}" for r in res}))), flush=True)
            await asyncio.sleep(s.learn_every_min * 60)

    if learner is not None:
        tasks.append(asyncio.create_task(learn_loop()))
    tasks.append(asyncio.create_task(maint.run(maint_stop, initial_delay=min(60.0, s.maintenance_sec))))

    async def status():
        nonlocal cpu_prev, t_prev
        while True:
            await asyncio.sleep(60)
            cpu, _, cur = _cpu_mem()
            now = time.time()
            pct = 100 * (cpu - cpu_prev) / (now - t_prev)
            cpu_prev, t_prev = cpu, now
            row = {"t": round(now - wall0), "new": len(col.tracked), "mcap": len(uni.members["mcap"]) if uni else 0,
                   "volume": len(uni.members["volume"]) if uni else 0, "cpu_pct": round(pct, 1),
                   "rss_mb": round((cur or 0) / 1024, 1), "db_mb": round(_db_bytes(path, wal=False) / 1e6, 2)}
            samples.append(row)
            st = eng.stats
            print(f"--- {time.strftime('%H:%M:%S')} tracked new {row['new']} | mcap {row['mcap']} | volume {row['volume']} "
                  f"(universe tracked {len(uni.tracked) if uni else 0}) | safety checked {chk.stats['checked_unique']} | "
                  f"evals {st['evaluations']} | BUY_WATCH {st['alert_BUY_WATCH']} (approve {st['judge_APPROVE']} / veto "
                  f"{st['judge_VETO']}) SELL {st['alert_SELL']} HOLD {st['alert_HOLD']} REJECT {st['alert_REJECT']} | "
                  f"CPU {row['cpu_pct']}% RSS {row['rss_mb']} MB DB {row['db_mb']} MB", flush=True)
            if paper is not None:
                e = paper.equity()
                print(f"    paper: equity ${e['equity']:.2f} cash ${e['cash']:.2f} open {e['n_open']} | opened "
                      f"{paper.stats['opened']} closed {paper.stats['closed']} | realized ${e['realized_pnl']:+.2f} | "
                      f"backfilled {uni.stats['backfilled_coins'] if uni else 0} coins / "
                      f"{uni.stats['backfilled_candles'] if uni else 0} candles", flush=True)

    tasks.append(asyncio.create_task(status()))
    try:
        await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        maint_stop.set()
        for x in (eng, chk, col, sc) + ((uni,) if uni else ()):
            await x.stop()
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
    wall = time.time() - wall0
    cpu, peak_kb, cur_kb = _cpu_mem()
    db.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db1 = _db_bytes(path, wal=False)
    since = wall0
    rows = [dict(r) for r in db.con.execute("SELECT * FROM signals WHERE ts>=?", (since,))]
    verdicts = [dict(r) for r in db.con.execute("SELECT * FROM judge_verdicts WHERE ts>=?", (since,))]
    per_u: dict[str, Counter] = {}
    for r in rows:
        c = per_u.setdefault(r["universe"], Counter())
        c[f"type:{r['alert_type']}"] += 1
        c[f"class:{r['class']}"] += 1
        c[f"strategy:{r['strategy']}"] += 1
    veto_reasons, approve = Counter(), Counter()
    for v in verdicts:
        approve[(v["universe"], v["verdict"])] += 1
        if v["verdict"] == "VETO":
            for rr in json.loads(v["reasons_json"]):
                if rr["kind"] == "hard":
                    veto_reasons[rr["code"]] += 1
    reject_reasons = Counter((r["reason"] or "").split(":")[0].split(" (")[0] for r in rows if r["alert_type"] == "REJECT")
    print("\n== universes ==")
    if uni:
        us = uni.snapshot()
        print(f"  candidates {us['candidates']} | MCAP members {us['mcap']} | TOP VOLUME members {us['volume']} | "
              f"universe-tracked {us['tracked']} | filtered {us['filtered']}")
        for u in ("mcap", "volume"):
            top = db.con.execute("SELECT m.symbol, u.rank, u.mcap, u.vol_h24, u.rank_h1 FROM universe_members u LEFT JOIN "
                                 "token_market m USING(mint) WHERE u.universe=? AND u.active=1 ORDER BY u.rank LIMIT 8",
                                 (u,)).fetchall()
            print(f"  {u:<6} top: " + ", ".join(f"#{r[1]} {r[0]} mcap {_money(r[2])} vol24 {_money(r[3])} (1h #{r[4]})"
                                                 for r in top))
    print("== signals per universe (this run) ==")
    for u, c in sorted(per_u.items()):
        print(f"  {u}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    if not rows:
        print("  (no signals in this run)")
    print(f"  REJECT reasons: {dict(reject_reasons.most_common(10))}")
    print("== judge ==")
    print(f"  verdicts {dict((f'{u}:{v}', n) for (u, v), n in approve.items())}")
    print(f"  veto reasons {dict(veto_reasons.most_common())}")
    print(f"== detectors fired (evaluations {eng.stats['evaluations']}) ==")
    print("  " + ", ".join(f"{k[4:]}={v}" for k, v in sorted(eng.stats.items()) if k.startswith("det_")))
    for u, c in sorted(eng.by_universe.items()):
        print(f"  {u}: evaluated {c['evaluated']} | " + ", ".join(f"{k}={v}" for k, v in sorted(c.items()) if k != "evaluated"))
    print("  virtual outcomes: " + (", ".join(f"{k[8:]}={v}" for k, v in sorted(eng.stats.items())
                                              if k.startswith("outcome_")) or "none closed"))
    print(f"  waiting for safety {eng.stats['waiting_safety']}, waiting for quote {eng.stats['waiting_quote']}, "
          f"open watches at end {len(eng.open)}, closed {eng.stats['closed_approved']} approved + {eng.stats['closed_vetoed']} vetoed")
    print("== safety ==")
    print(f"  checked {chk.stats['checked_unique']} (pass {chk.stats['passed']}, reject {chk.stats['rejected']}, unknown "
          f"{chk.stats['unknown']}), quotes {chk.stats['quotes']}, RugCheck calls {chk.stats['rc_calls']}, "
          f"missing reports {chk.stats['rc_missing']}")
    print("== API calls per source (429s) ==")
    api = limiter_stats(limiters)
    for k, v in api.items():
        print(f"  {k:<22} {v['calls']:>5}  ({v['429']} x 429)  = {60 * v['calls'] / wall:.1f}/min")
    print(f"== resources == CPU {cpu:.1f}s over {wall:.0f}s ({100 * cpu / wall:.1f}% avg), RSS now "
          f"{(cur_kb or 0) / 1024:.1f} MB, peak {max(peak_kb, cur_kb or 0) / 1024:.1f} MB")
    print(f"== database (after WAL checkpoint) == {db0 / 1e6:.2f} MB -> {db1 / 1e6:.2f} MB")
    print(f"  tables {db.table_counts()}")
    db.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    fb1 = file_bytes(db)
    hours = max(wall / 3600, 1e-9)
    print("== database growth (phase 7) ==")
    print(f"  used {fb0['used'] / 1e6:.2f} MB -> {fb1['used'] / 1e6:.2f} MB = {(fb1['used'] - fb0['used']) / 1e6 / hours:+.1f} "
          f"MB/hour net (file {fb1['file'] / 1e6:.2f} MB, free {fb1['free_pct']:.1f}%, WAL {fb1['wal'] / 1e6:.2f} MB)")
    pj = projection(db, s, since_ts=wall0)
    print(f"  projected size after N days (rates from {pj['hours'] * 60:.0f} min, sizes via {pj['method']}): "
          + ", ".join(f"{d}d {v} MB" for d, v in pj["size_at_days"].items())
          + f" | steady (90d) ~{pj['steady_mb']} MB, cap DB_MAX_MB={s.db_max_mb:g} | kept-forever tables "
          f"+{pj['forever_mb_per_year']} MB/year")
    for r in pj["tables"][:12]:
        ret = "forever" if r["forever"] else f"{r['retention_h']:g}h"
        print(f"    {r['table']:<36} {r['rows_per_h']:>9}/h x {ret:>7} x {r['bytes_per_row']:>5} B = "
              f"{r['steady_mb']:>6} MB at 90 days")
    cons1 = consistency(db)
    print(f"  maintenance {dict(maint.stats)} errors {maint.errors[-3:]} | integrity {integrity_check(db, quick=True)} | "
          f"consistency {'OK' if cons1['ok'] else cons1['problems']} {cons1['facts']}")
    if with_paper:
        from .learning import cost_pct, judge_stats
        from .reports import history_text, paper_text, positions_text, report_text, strategies_text
        final = learner.run_once()
        print("\n== paper trading ==")
        print(paper_text(db, s))
        print(positions_text(db))
        print(history_text(db, 20))
        print(f"  opened {paper.stats['opened']} | closed {paper.stats['closed']} | exits "
              f"{ {k[5:]: v for k, v in paper.stats.items() if k.startswith('exit_')} } | not opened: {dict(paper.skips)}")
        print("== daily report ==")
        print(report_text(db, s))
        js = judge_stats(db, wall0, cost_pct(s))
        print("== judge accuracy (this run, virtual outcomes) ==")
        print(f"  approved {js['approved']} | vetoed {js['vetoed']} | veto precision {js['veto_precision']} | "
              f"edge {js['edge_pct']}")
        print(f"  by reason {js['by_reason']}")
        print("== self-learning ==")
        for r in final:
            print(f"  {r}")
        print(strategies_text(db, s))
        print(f"== universe backfill (GeckoTerminal) == coins {uni.stats['backfilled_coins'] if uni else 0}, candles "
              f"{uni.stats['backfilled_candles'] if uni else 0}")
        await client.aclose()
    errs = list(eng.errors)[-5:] + list(chk.errors)[-5:] + list(col.errors)[-5:] + (list(uni.errors)[-5:] if uni else []) \
        + list(sc.snapshot()["last_errors"])
    if paper is not None:
        errs += list(paper.errors)[-5:]
    errors_total = eng.stats["errors"] + chk.stats["errors"] + col.stats["errors"] + (uni.stats["errors"] if uni else 0) \
        + (len(paper.errors) if paper is not None else 0)
    print(f"== errors == {errors_total}")
    for e in errs:
        print(f"  {e}")
    db.close()
    return {"signals": rows, "verdicts": verdicts, "api": api, "samples": samples, "per_universe": per_u,
            "veto_reasons": veto_reasons, "engine": dict(eng.stats)}
