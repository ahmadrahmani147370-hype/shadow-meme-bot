"""`python main.py --debug scan [seconds] [fallback]` - run the scanner live and print what it sees."""
from __future__ import annotations

import asyncio
import contextlib
import time

from .config import Settings
from .db import Database
from .models import MIGRATION
from .scanner import Scanner


def _fmt(ev) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(ev.seen_ts))
    if ev.kind == MIGRATION:
        return f"{t} 🚀 MIGRATION  {ev.mint}  -> {ev.pool or ev.status}  [{ev.source}]"
    extra = []
    if ev.creator:
        extra.append(f"dev {ev.creator[:6]}…")
    if ev.dev_buy_sol is not None:
        extra.append(f"dev buy {ev.dev_buy_sol:.3f} SOL")
    if ev.initial_mcap_sol is not None:
        extra.append(f"mcap {ev.initial_mcap_sol:.1f} SOL")
    if ev.initial_mcap_usd is not None:
        extra.append(f"mcap ${ev.initial_mcap_usd:,.0f}")
    name = f"{ev.symbol or '?'} ({ev.name[:24]})" if ev.name else (ev.symbol or "?")
    return f"{t} 🆕 NEW  {name:<34} {ev.mint}  {ev.status}{'/' + ev.launchpad if ev.launchpad else ''}{'/' + ev.pool if ev.pool else ''}  [{ev.source}]  {' | '.join(extra)}"


async def run_scan(s: Settings, seconds: float = 60, force_fallback: bool = False, quiet: bool = False) -> dict:
    db = Database(s.path(s.db_path))
    before = db.count_seen().get("total", 0)
    sc = Scanner(s, db, force_fallback=force_fallback)
    print(f"SHADOW MEME scanner - {seconds:.0f}s, {'FORCED FALLBACK' if force_fallback else ('websocket + always-on polling' if s.scanner_poll_always else 'websocket + auto fallback')}; "
          f"{before} mints already in {s.db_path}. Ctrl+C to stop.")

    async def consume():
        async for ev in sc.stream():
            if not quiet:
                print(_fmt(ev), flush=True)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(asyncio.shield(consumer), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await sc.stop()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await consumer
    snap = sc.snapshot()
    db.close()
    keys = ["new_tokens", "new_pumpportal", "new_geckoterminal", "new_rugcheck", "migrations", "duplicates",
            "duplicate_migrations", "ws_connects", "ws_disconnects", "ws_acks", "ws_idle_timeouts", "ws_errors",
            "fallback_on", "fallback_off", "poll_geckoterminal", "poll_rugcheck", "errors", "queue_dropped"]
    print("\n== counts ==")
    for k in keys:
        print(f"  {k:<22} {snap.get(k, 0)}")
    print(f"  seen_in_db             {snap['seen_in_db']}")
    for e in snap["last_errors"]:
        print(f"  error: {e}")
    return snap
