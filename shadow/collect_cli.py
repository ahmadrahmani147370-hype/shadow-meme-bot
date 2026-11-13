"""`python main.py --debug collect [seconds]` - scanner + collector live, prints enriched tokens and candle counts."""
from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import time

from .collector import Collector
from .config import Settings
from .db import Database
from .net import limiter_stats, make_limiters
from .scanner import Scanner


def _money(x):
    if x is None:
        return "?"
    return f"${x / 1e6:.2f}M" if x >= 1e6 else (f"${x / 1e3:.1f}k" if x >= 1e3 else f"${x:.0f}")


def _line(md, tracking: bool) -> str:
    t = time.strftime("%H:%M:%S")
    age = md.age_min()
    return (f"{t} 💧 {md.symbol[:12] or '?':<12} {md.mint[:8]}…  age {age:.1f}m  {md.migration_status:<8} {md.dex_id:<10} "
            f"mcap {_money(md.market_cap):>8}  liq {_money(md.liquidity_usd):>7}  vol5m {_money(md.vol_m5):>7}  "
            f"5m b/s {md.buys_m5}/{md.sells_m5}  {'TRACK' if tracking else 'stored'}")


def _cpu_mem():
    ru = resource.getrusage(resource.RUSAGE_SELF)
    rss_kb = ru.ru_maxrss  # KB on Linux/Android
    cur = None
    with contextlib.suppress(Exception):
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    cur = int(line.split()[1])
    return ru.ru_utime + ru.ru_stime, rss_kb, cur


async def run_collect(s: Settings, seconds: float = 300, quiet: bool = False) -> dict:
    db = Database(s.path(s.db_path))
    db.acquire_writer_lock()
    limiters = make_limiters()
    sc = Scanner(s, db, limiters=limiters)
    stops = []
    col = Collector(s, db, limiters=limiters,
                    on_enriched=None if quiet else (lambda md, tr: print(_line(md, tr), flush=True)),
                    on_stop=lambda md, r: stops.append(r))
    print(f"SHADOW MEME collector - {seconds:.0f}s; max tracked {s.max_tracked}, snapshot every {s.snapshot_sec}s, "
          f"max age {s.max_token_age_min} min. Ctrl+C to stop.")
    t0, wall0 = time.process_time(), time.time()
    task = asyncio.create_task(col.run(sc.stream()))

    async def status():
        while True:
            await asyncio.sleep(60)
            c = db.table_counts()
            print(f"--- {time.strftime('%H:%M:%S')} tracked {len(col.tracked)} | pending {len(col.pending)} | "
                  f"snapshots {c['price_snapshots']} | candles {c['candles_1m']} | drops {dict(col.drops)} | "
                  f"scanner {sc.mode}", flush=True)

    st = asyncio.create_task(status())
    try:
        await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await col.stop()
        await sc.stop()
        for t in (task, st):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
    cpu, peak_kb, cur_kb = _cpu_mem()
    wall = time.time() - wall0
    counts = db.table_counts()
    local = db.con.execute("SELECT COUNT(*), COUNT(DISTINCT mint) FROM candles_1m WHERE source='local'").fetchone()
    gt = db.con.execute("SELECT COUNT(*), COUNT(DISTINCT mint) FROM candles_1m WHERE source='geckoterminal'").fetchone()
    snap = {"scanner": sc.snapshot(), "collector": col.snapshot(), "api": limiter_stats(limiters), "tables": counts,
            "cpu_s": round(cpu, 1), "cpu_pct": round(100 * cpu / wall, 1) if wall else 0, "peak_rss_mb": round(peak_kb / 1024, 1),
            "rss_mb": round((cur_kb or 0) / 1024, 1), "local_candles": tuple(local), "gt_candles": tuple(gt)}
    c = snap["collector"]
    print("\n== collector ==")
    for k in ("received", "enriched", "tracked_total", "tracked_max", "snapshots", "snapshots_jupiter", "candles_local",
              "candles_gapfill", "candles_gt", "migrations_derived", "migrations_tracked", "errors"):
        print(f"  {k:<20} {c.get(k, 0)}")
    print(f"  tracked_now          {c['tracked']}  (pending {c['pending']})")
    print(f"  dropped              {c['drops']}")
    print(f"  stopped              { {k[8:]: v for k, v in c.items() if k.startswith('stopped_')} }")
    print(f"  candles in db        local {local[0]} for {local[1]} tokens | geckoterminal {gt[0]} for {gt[1]} tokens")
    print("== scanner ==")
    print(f"  new {snap['scanner'].get('new_tokens', 0)} (pumpportal {snap['scanner'].get('new_pumpportal', 0)}, "
          f"geckoterminal {snap['scanner'].get('new_geckoterminal', 0)}, rugcheck {snap['scanner'].get('new_rugcheck', 0)}), "
          f"migrations {snap['scanner'].get('migrations', 0)}, duplicates {snap['scanner'].get('duplicates', 0)}, "
          f"errors {snap['scanner'].get('errors', 0)}")
    print("== API calls per source (429s) ==")
    for k, v in snap["api"].items():
        print(f"  {k:<22} {v['calls']:>5}  ({v['429']} x 429)")
    print(f"== resources == CPU {snap['cpu_s']}s over {wall:.0f}s ({snap['cpu_pct']}%), RSS now {snap['rss_mb']} MB, "
          f"peak {snap['peak_rss_mb']} MB")
    print(f"== tables == {counts}")
    for e in list(c["last_errors"]) + list(snap["scanner"]["last_errors"]):
        print(f"  error: {e}")
    db.close()
    return snap
