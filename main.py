"""SHADOW MEME entry point.

    python main.py                  # run the bot (Telegram-controlled; start.sh does this in a restart loop)
    python main.py --debug doctor   # self-check (same as: python -m shadow.doctor)
    python main.py --debug doctor --offline
    python main.py --debug scan [seconds] [fallback]   # live scanner test (phase 2)
    python main.py --debug collect [seconds]           # scanner + collector live test (phase 3)
    python main.py --debug safety <mint>               # full safety check of one token (phase 4)
    python main.py --debug safety-live [seconds]       # scanner + collector + safety live test (phase 4)
    python main.py --debug signals [seconds]           # everything + universes + signals + judge (phase 5)
    python main.py --debug paper [seconds]             # everything + paper trading + self-learning (phase 6)
    python main.py --debug paper report                # /paper /positions /history /report /strategies from SQLite
    python main.py --debug learn                       # one self-learning pass on the recorded data
    python main.py --debug db                          # integrity, consistency, sizes, row counts, backups (phase 7)
    python main.py --debug db prune|backup|vacuum|optimize|projection [hours]|export [dir]

Exit codes: 0 ok, 1 doctor found a FAIL, 2 config error, 3 feature not built yet (start.sh stops on 2 and 3).
"""
from __future__ import annotations

import argparse
import os
import sys

from shadow import __version__
from shadow.config import load_settings


def _debug(a) -> int:
    cmd, rest = a.debug[0], a.debug[1:]
    if cmd == "dashboard":
        from shadow.dashboard_cli import run as dash_run

        return dash_run(load_settings(), rest)
    if cmd == "doctor":
        from shadow.doctor import main as doctor_main

        return doctor_main([x if x.startswith("--") else f"--{x}" for x in rest])
    if cmd == "scan":
        import asyncio
        import logging

        from shadow.scan_cli import run_scan

        logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        secs = next((float(x) for x in rest if x.replace(".", "", 1).isdigit()), 60.0)
        try:
            asyncio.run(run_scan(load_settings(), secs, force_fallback="fallback" in rest))
        except KeyboardInterrupt:
            pass
        return 0
    if cmd == "collect":
        import asyncio
        import logging

        from shadow.collect_cli import run_collect

        logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        secs = next((float(x) for x in rest if x.replace(".", "", 1).isdigit()), 300.0)
        try:
            asyncio.run(run_collect(load_settings(), secs, quiet="quiet" in rest))
        except KeyboardInterrupt:
            pass
        return 0
    if cmd in ("safety", "safety-live"):
        import asyncio
        import logging

        from shadow.safety_cli import run_safety_live, run_safety_one

        logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        try:
            if cmd == "safety":
                mints = [x for x in rest if len(x) >= 32]
                if not mints:
                    print("usage: python main.py --debug safety <mint>")
                    return 2
                return asyncio.run(run_safety_one(load_settings(), mints[0]))
            secs = next((float(x) for x in rest if x.replace(".", "", 1).isdigit()), 300.0)
            asyncio.run(run_safety_live(load_settings(), secs, quiet="quiet" in rest))
        except KeyboardInterrupt:
            pass
        return 0
    if cmd == "db":
        from shadow.db_cli import run_db

        return run_db(load_settings(), rest)
    if cmd in ("paper", "learn"):
        import asyncio
        import logging

        logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        s = load_settings()
        if cmd == "learn" or "report" in rest:
            from shadow.db import Database
            from shadow.judge import Judge
            from shadow.learning import Learner
            from shadow.paper import PaperEngine
            from shadow.reports import (history_text, paper_text, pending_strategy_updates, positions_text,
                                        report_text, strategies_text)
            db = Database(s.path(s.db_path))
            PaperEngine(s, db)
            if cmd == "learn":
                for r in Learner(s, db, judge=Judge(s, db)).run_once():
                    print(r)
            for text in (paper_text(db, s), positions_text(db), history_text(db), report_text(db, s),
                         strategies_text(db, s)):
                print(text + "\n")
            pend = pending_strategy_updates(db)
            print(f"Unsent strategy update notifications: {len(pend)}")
            for _, t in pend:
                print(t + "\n")
            db.close()
            return 0
        from shadow.signals_cli import run_signals
        secs = next((float(x) for x in rest if x.replace(".", "", 1).isdigit()), 900.0)
        try:
            asyncio.run(run_signals(s, secs, quiet_rejects="verbose" not in rest, with_paper=True))
        except KeyboardInterrupt:
            pass
        return 0
    if cmd == "signals":
        import asyncio
        import logging

        from shadow.signals_cli import run_signals

        logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        secs = next((float(x) for x in rest if x.replace(".", "", 1).isdigit()), 600.0)
        try:
            asyncio.run(run_signals(load_settings(), secs, quiet_rejects="quiet" in rest))
        except KeyboardInterrupt:
            pass
        return 0
    print(f"unknown debug command: {cmd}")
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"SHADOW MEME {__version__} (paper-only)")
    ap.add_argument("--debug", nargs="+", metavar="CMD", help="debug tools: dashboard [snapshot|publish|url|live N] | db [prune|backup|vacuum|optimize|projection|export] | doctor [offline] [json] | scan [seconds] [fallback] | collect [seconds] | "
                         "safety <mint> | safety-live [seconds] | signals [seconds] [quiet] | paper [seconds|report] | learn")
    a = ap.parse_args(argv)

    if a.debug:
        from shadow.db import WriterLocked
        try:
            return _debug(a)
        except WriterLocked as e:
            print(f"STOP: {e}")
            return 1

    from shadow.logging_setup import setup_logging

    s = load_settings()
    log = setup_logging(s)
    log.info("SHADOW MEME %s starting (PAPER_ONLY=%s)", __version__, s.paper_only)
    for w in getattr(s, "warnings", ()) or ():
        log.warning("config: %s", w)
    if (not s.telegram_bot_token and not s.tg_log_only) or not s.telegram_chat_id:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env - the bot is controlled only through Telegram.")
        return 2
    import asyncio

    from shadow.app import App
    from shadow.db import WriterLocked
    try:
        app = App(s)
    except WriterLocked as e:
        log.error("STOP: %s", e)
        return 4
    secs = float(os.environ.get("SHADOW_RUN_SECONDS") or 0) or None   # testing: stop after N seconds
    try:
        return asyncio.run(app.run(seconds=secs))
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    sys.exit(main())
