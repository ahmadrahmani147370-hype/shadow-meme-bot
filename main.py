"""SHADOW MEME entry point.

    python main.py                  # run the bot (Telegram-controlled; arrives in phase 8)
    python main.py --debug doctor   # self-check (same as: python -m shadow.doctor)
    python main.py --debug doctor --offline

Exit codes: 0 ok, 1 doctor found a FAIL, 2 config error, 3 feature not built yet (start.sh stops on 2 and 3).
"""
from __future__ import annotations

import argparse
import sys

from shadow import __version__
from shadow.config import load_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"SHADOW MEME {__version__} (paper-only)")
    ap.add_argument("--debug", nargs="+", metavar="CMD", help="debug tools: doctor [--offline] [--json]")
    a = ap.parse_args(argv)

    if a.debug:
        cmd, rest = a.debug[0], a.debug[1:]
        if cmd == "doctor":
            from shadow.doctor import main as doctor_main

            return doctor_main([x if x.startswith("--") else f"--{x}" for x in rest])
        print(f"unknown debug command: {cmd}")
        return 2

    from shadow.logging_setup import setup_logging

    s = load_settings()
    log = setup_logging(s)
    log.info("SHADOW MEME %s starting (PAPER_ONLY=%s)", __version__, s.paper_only)
    log.warning("The Telegram bot loop arrives in phase 8. Phase 1 only ships setup + doctor: "
                "run `python main.py --debug doctor`.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
