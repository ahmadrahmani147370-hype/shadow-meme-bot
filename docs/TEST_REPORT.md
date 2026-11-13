# SHADOW MEME v1.0.0 - Test report (Phase 10)

Date: 13 Nov 2026 (times PKT, UTC+5). Machine: Linux box (x86-64, Python 3.13, SQLite 3.46). Not tested on
a real Android phone: Termux compatibility was reviewed and the install was simulated with a minimal
virtualenv (only the pure-Python requirements, no pandas/numpy). All numbers below were measured; nothing
is estimated unless it says so. **Paper trading only** - no wallet, no private key, no real money.

## 1. Fresh clone → setup → doctor → tests

| Step | Result |
|---|---|
| `git clone` of `main` (995b560) into an empty folder | ok |
| `BUILD_EXTENSION=no pip install -r requirements-dev.txt` in a new venv | ok - only pure-Python wheels: httpx 0.28.1, httpcore, h11, anyio, certifi, idna, websockets 17.4, python-dotenv 1.2.4, pytest 9.2 |
| `python -m shadow.doctor` | **27 OK, 3 WARN, 2 FAIL** - the 2 FAILs were pandas and numpy missing → **bug 1** (fixed) |
| `pytest` (incl. the live network test) | 289 passed, 1 skipped (the pandas-only test) |
| After the fixes: fresh clone of the release, same minimal venv | doctor **26 OK, 6 WARN, 0 FAIL → READY**; pytest **305 passed, 1 skipped** |

## 2. Long end-to-end run (35 minutes)

Started with `start.sh` from the fresh clone (minimal venv, empty database), everything on: PumpPortal
websocket + GeckoTerminal/RugCheck polling, collector, MCAP / TOP VOLUME universes, safety checker, signal
engine + judge, paper trading, self-learning, maintenance/backup, dashboard snapshots (local), Telegram bot
in **log-only mode** (`TELEGRAM_LOG_ONLY=true`: the whole Telegram path runs - alerts, queue, rate limits,
commands via injected updates - but no request goes to Telegram). 13:24:00 → 13:59:00, stopped by
`SHADOW_RUN_SECONDS`; `start.sh` then reported "bot exited normally". One HEALTH line per minute (34 samples).

| Metric | Start (1 min) | End (34 min) | Min / max / avg |
|---|---|---|---|
| Memory (RSS) | 51.7 MB | 59.6 MB | 51.7 / 59.6 / 57.0 MB - rises while the token lists fill, flattening (55.9 → 56.7 → 57.3 → 58.0 → 57.4 → 59.6 at 5-min steps) |
| CPU (one core) | 0.6 % | 1.5 % | 0.6 / 1.5 / 1.0 % |
| Event-loop lag | 1.6 ms | 1.2 ms | 0.2 / 2.3 / 1.1 ms |
| Database file | 0.55 MB | 10.19 MB | + WAL 0–4.8 MB (checkpointed) |
| DB growth | | | ≈ 17 MB/h while the rolling tables fill; retention + the 150 MB cap stop it (phase 7 projection: ~82 MB after 1 day, ~145 MB after 30 days) |

API use (whole run, calls per minute vs. our own budget, which is below the free limits):

| Source | Calls | Per minute | Budget/min | 429s |
|---|---|---|---|---|
| DexScreener | 1163 | 34.6 | 240 | 0 |
| RugCheck | 770 | 22.3 | 50 | 0 |
| Solana RPC (public) | 261 | 7.8 | 60 | 0 |
| Jupiter (quotes/prices) | 207 | 6.2 | 25 | 0 |
| GeckoTerminal | 163 | 4.7 | 6 | 0 |
| DexScreener profiles | 6 | 0.2 | 50 | 0 |

Activity: 889 new tokens seen (websocket + polling), 456 safety-checked, tracked 184 new / 13 MCAP / 17 TOP
VOLUME. Signals: 11 BUY WATCH (judge 3 approve / 8 veto), 3 SELL, 151 REJECT. Paper: 3 trades opened and
closed (STONK +$0.58, Democat +$0.62, USPR +$0.05), equity $100.00 → $101.25. Three trades is far too few to
judge the strategy; this only shows the pipeline works.

Stability: **0 warnings, 0 errors, 0 tracebacks, 0 HTTP 429**, 0 scanner errors, websocket stayed up, no task
died, Telegram queue 0, nothing dropped. Database after the run: `integrity_check ok`, paper books
consistent (cash 101.2482 = expected), daily backup written, dashboard snapshot written.

Telegram end-to-end (log-only, at minute 25): 15 commands injected through the real poll loop
(/start /status /scanner /newtokens /signals /paper /positions /history /report /strategies /export
/dashboard /help /pause /resume) → 15 handled, 26 messages/documents produced (6 CSV documents for
/export, a web_app button for /dashboard), a message from a foreign chat ignored, pause/resume restored.
42 messages in total went through the outgoing queue (startup, alerts, trade open/close, replies).

`start.sh` signal test: SIGTERM to `start.sh` → bot logs "stopped cleanly", script exits, nothing left.

Earlier live runs with the **real** Telegram API (phase 8, 11 min): 26 long polls, 0 errors, no 409, the
startup message delivered.

## 3. Safety audit

| Check | Result |
|---|---|
| `PAPER_ONLY` | constant `True` in `shadow/__init__.py`; `Settings.paper_only` is `init=False`; `PAPER_ONLY=false` in .env is ignored with a warning; `PaperEngine` asserts it. Tested (`tests/test_safety_audit.py`). |
| Private keys | no code reads, stores or uses one; any `*PRIVATE_KEY*` / `*SEED*` / `*MNEMONIC*` / `*KEYPAIR*` variable is ignored with a warning. No Solana signing library is a dependency. |
| Transactions | no `sendTransaction` / signing / Jupiter swap-execute code; Solana RPC methods used: `getAccountInfo`, `getMultipleAccounts` only (read-only). Jupiter: quote/price only. Enforced by tests. |
| Secrets in the repo | full history of all branches searched: the real Telegram token never appears; only obviously fake test tokens. `.env`, `*.db`, `*.log` are not tracked. |
| Secrets in logs | all box logs of phases 7-10 searched for the token: none. httpx request logging is off, every log line passes a redaction filter, Telegram/GitHub errors print `<token>`. |
| Dashboard data | whitelisted fields only (no token, keys, error texts or URLs); the snapshot is public because the repo is public. |

## 4. Termux compatibility review

- Runtime dependencies: httpx, websockets (installed with `BUILD_EXTENSION=no`, pure Python), python-dotenv -
  no compiler, Rust or C build needed. pandas/numpy are no longer required (bug 1).
- Standard library only otherwise; `fcntl`, `sqlite3`, `/proc/self/stat` exist on Android. SQLite builds
  without `dbstat` are handled (size estimate fallback). Missing tz database → fixed UTC+5 fallback.
- `start.sh`: bash, `wc`, `date`, `command -v`, optional `termux-wake-lock`; restart loop tested; SIGTERM
  tested. Use `bash start.sh` (works with or without termux-exec). The Termux:Boot script now calls
  `bash ./start.sh` (bug 3).
- Not verified on a real phone: Android battery killing, Termux:Boot, termux-api battery reading.

## 5. Bugs found and fixed in phase 10

1. **Doctor failed on a minimal install**: pandas/numpy were "required" although only the optional
   `candles_df()` helper uses them; `setup_termux.sh` could fall back to `pip install pandas` (compiles for a
   long time on a phone). → optional (WARN), setup installs only pure-Python packages.
2. **A crashed core task went unnoticed**: if e.g. the collector task died, the process kept running without
   it. → watchdog in the HEALTH loop logs the exception and exits 1, so `start.sh` / `tools/box_run.sh`
   restart the bot.
3. **Termux:Boot script relied on termux-exec** (`./start.sh` with a `/usr/bin/env` shebang) → `bash ./start.sh`;
   new `.env` files get `chmod 600`; outdated phase texts in the setup script.
4. **A stale exported `TELEGRAM_BOT_TOKEN` silently beat the new value in `.env`** (found during the token
   rotation) → the bot now warns (key name only, never the value); `tools/box_run.sh` unsets it.
5. Added for testing/operations: `TELEGRAM_LOG_ONLY`, the HEALTH line, `tools/box_run.sh` + `box_stop.sh`,
   `DASHBOARD_PUBLISHER=gh` (upload through the GitHub CLI login, no token in `.env`).

## 6. Known limits

- Strategy results are not proven: a few dozen paper trades in total so far. Self-learning needs ≥30 closed
  watches before it changes anything; no strategy_update happened in any test run.
- Free data sources: PumpPortal's free websocket misses many launches (polling covers them, with delay);
  GeckoTerminal is the tightest budget (4.7 of 6 calls/min used); RugCheck reports can be missing → verdict
  UNKNOWN → never traded.
- Paper fills use Jupiter quotes + slippage; real fills, MEV and failed transactions are not simulated.
- On a phone Android may kill or pause Termux despite the wake lock; on the box the machine itself can be
  suspended when idle (processes pause and resume; no alerts while paused).
- Telegram may be blocked on some Pakistani networks (`TELEGRAM_API_BASE` / WARP).

## 7. Final verification (release commit 6033e38, tag v1.0.0)

- Fresh clone, new venv with only `requirements-dev.txt` (no pandas/numpy, like Termux), no token:
  doctor `26 OK, 6 WARN, 0 FAIL -> READY` (WARNs: pandas + numpy optional, Telegram token empty in the test
  copy, GITHUB_TOKEN optional, pump.fun frontend behind Cloudflare, Birdeye needs a key - both unused);
  `pytest`: **305 passed, 1 skipped** (the pandas-only helper test), including the live network test and
  5 headless-Chrome dashboard tests.
- Development machine with pandas installed: **306 passed**.
