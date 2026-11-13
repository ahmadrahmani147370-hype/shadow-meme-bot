"""`python main.py --debug dashboard [snapshot [file] | publish | live [seconds] | url]` (phase 9)."""
from __future__ import annotations

import asyncio
import json
import logging

from . import dashboard as dash
from .config import Settings
from .db import Database


def _db(s: Settings) -> Database:
    db = Database(str(s.path(s.db_path)), busy_timeout_ms=s.db_busy_timeout_ms)
    db.acquire_writer_lock()         # snapshot creates the instance id / default judge params: refuse while the bot runs
    return db


def run(s: Settings, rest: list[str]) -> int:
    sub = rest[0] if rest else "snapshot"
    if sub in ("snapshot", "publish", "url"):
        db = _db(s)
        snap = dash.build_snapshot(db, s)
        if sub == "snapshot":
            out = rest[1] if len(rest) > 1 else str(s.path(s.db_path).parent / "dashboard" / "snapshot.json")
            p = dash.write_local(snap, out)
            b = dash.snapshot_json(snap)
            print(f"snapshot written: {p} ({len(b) / 1024:.1f} KB) | positions {len(snap['positions'])} | history "
                  f"{len(snap['history'])} | equity points {len(snap['equity_curve'])} | signals "
                  f"{len(snap['signals']['recent'])} | updates {len(snap['updates'])}")
            print(f"summary URL ({len(dash.dashboard_url(s, snap))} chars): {dash.dashboard_url(s, snap)}")
            return 0
        if sub == "url":
            print(dash.dashboard_url(s, snap))
            return 0
        if not s.github_token:
            print("GITHUB_TOKEN is not set: nothing to publish (the dashboard shows the URL summary only).")
            return 2

        async def pub():
            p = dash.GitHubPublisher(s.github_token, s.github_repo, s.dashboard_branch)
            try:
                return await p.publish(dash.snapshot_json(snap))
            finally:
                await p.close()
        try:
            sha = asyncio.run(pub())
        except dash.GitHubError as e:
            print(f"publish failed: {e}")
            return 1
        print(f"published {s.github_repo}@{s.dashboard_branch} commit {sha[:10]} -> {dash.raw_url(s)}")
        return 0
    if sub == "live":
        from .app import App
        from .logging_setup import setup_logging
        setup_logging(s)
        secs = float(rest[1]) if len(rest) > 1 else 600.0
        app = App(s, telegram=False)

        async def go():
            await app.run(seconds=secs)
            return await app.publish_dashboard()
        res = asyncio.run(go())
        logging.getLogger("shadow.dashboard").info("final snapshot: %s", json.dumps(res))
        return 0
    print("usage: --debug dashboard [snapshot [file] | publish | url | live [seconds]]")
    return 2
