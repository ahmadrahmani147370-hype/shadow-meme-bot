#!/usr/bin/env python3
"""Archive all paper trades and restart the paper account (bot must be stopped).
   bash tools/box_stop.sh && python3 tools/reset_paper.py --balance 100 && setsid nohup bash tools/box_run.sh >/dev/null 2>&1 &
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shadow.config import load_settings  # noqa: E402
from shadow.db import Database  # noqa: E402
from shadow.paper_reset import archive_and_reset  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--balance", type=float, default=None, help="new start balance (default PAPER_START_BALANCE)")
ap.add_argument("--label", default=None)
a = ap.parse_args()
s = load_settings()
db = Database(str(s.path(s.db_path)), busy_timeout_ms=s.db_busy_timeout_ms)
db.acquire_writer_lock()            # refuses while the bot runs
res = archive_and_reset(db, a.balance if a.balance is not None else s.paper_start_balance,
                        archive_dir=s.path("data/archive"), label=a.label)
print(json.dumps(res, indent=1))
db.close()
