# SHADOW MEME 🕶️

Solana meme coin scanner + **paper-trading** signal bot in Python, built for **Android Termux**
(no VPS, no paid subscriptions) and controlled **only from Telegram**.

> **Paper-only and analysis-only.** There is no live-trading code, no wallet connection and no
> private key, ever. `PAPER_ONLY` is a constant in the code; `.env` cannot turn it off.
> Signals are not financial advice, and the bot reports only results it actually measured.

## Status: Phase 3 of 10 ✅ (data collection)

| Phase | What | Status |
|---|---|---|
| 1 | Termux installation, config, doctor, verified data sources | ✅ done |
| 2 | Token scanner (PumpPortal stream + fallbacks) | ✅ done |
| 3 | Data collection (price, mcap, liquidity, volume, buys/sells) | ✅ done |
| 4 | Coin safety checker | ⏳ next |
| 5 | Signal engine + 3 strategies + 0-100 score | ⏳ |
| 6 | Paper trading engine ($100, $2/trade, $0.50+$0.50 fees, max 5 open) | ⏳ |
| 7 | SQLite database (positions survive restarts) | ⏳ |
| 8 | Telegram commands, alerts, daily report | ⏳ |
| 9 | Dashboard / performance report | ⏳ |
| 10 | Complete testing | ⏳ |

Files for later phases exist as clearly marked stubs (`PHASE = N`, `IMPLEMENTED = False`).

## Install on Android (Termux)

1. Install **Termux from F-Droid** (the Play Store version is outdated). Optional: **Termux:API** and **Termux:Boot**, also from F-Droid.
2. In Termux:

```bash
pkg install -y git python && git clone https://github.com/ahmadrahmani147370-hype/shadow-meme-bot && cd shadow-meme-bot && bash setup_termux.sh
```

3. Check everything:

```bash
python -m shadow.doctor
```

4. Android: **Settings → Apps → Termux → Battery → Unrestricted**, otherwise Android kills it in the background.
5. Try the scanner (phase 2): `python main.py --debug scan 120`, and the collector (phase 3): `python main.py --debug collect 300`
6. From phase 8: `nano .env` (Telegram token + chat id), then `./start.sh`.

**Telegram bot:** SHADOW MEME will use the existing **@Fathersigna147lBot** with owner chat id
`7922990181`. It is wired in phase 8. A bot token can only be used by one program at a time, so the
old FATHER bot must be stopped before SHADOW MEME runs.

**If the doctor shows `telegram_api` ConnectTimeout** (seen on a Pakistani mobile network while
every other check passed): try another network (Wi-Fi or another SIM) or the free **1.1.1.1 (WARP)**
app. Advanced option: set `TELEGRAM_API_BASE` in `.env` to your own HTTPS Bot API mirror. The
doctor checks whatever `TELEGRAM_API_BASE` points to; the bot uses it from phase 8.

Update later: `cd shadow-meme-bot && git pull && python -m shadow.doctor`

## Roman Urdu quick start 🇵🇰

1. F-Droid se **Termux** install karein (Play Store wala purana hai). Chahein to **Termux:API** aur **Termux:Boot** bhi.
2. Termux khol kar ye ek line chalayein:

```bash
pkg install -y git python && git clone https://github.com/ahmadrahmani147370-hype/shadow-meme-bot && cd shadow-meme-bot && bash setup_termux.sh
```

3. Sab theek hai ya nahi, check karein: `python -m shadow.doctor` — aakhir mein `READY` aana chahiye.
4. Phone ki Settings → Apps → Termux → Battery → **Unrestricted** kar dein, warna Android background mein band kar deta hai.
5. Scanner test karein (phase 2): `python main.py --debug scan 120` — 2 minute tak naye coins aur migrations screen par aayenge, aakhir mein ginti.
   Collector test (phase 3): `python main.py --debug collect 300` — har coin ki price, market cap, liquidity, volume aur buys/sells, aur 1-minute candles. Aakhir mein batata hai kitne coins track hue, kitne kis wajah se chhore gaye, har API ko kitni calls gayin, aur CPU/RAM kitni lagi.
6. Phase 8 ke baad: `nano .env` mein Telegram token aur chat id likhein, phir `./start.sh`.

Scanner do tarah se coins dhoondta hai: PumpPortal websocket (live) aur GeckoTerminal + RugCheck polling. Test mein websocket ne sirf ~19% naye pump.fun coins diye, is liye polling hamesha on rehti hai. Ek coin dobara nahi aata, restart ke baad bhi nahi.

Agar doctor mein `telegram_api` par **ConnectTimeout** aaye (Pakistan ke kuch networks par Telegram block hota hai): doosra network (Wi-Fi ya doosri SIM) try karein, ya muft **1.1.1.1 (WARP)** app on karein. Advanced: `.env` mein `TELEGRAM_API_BASE` apne HTTPS mirror par set karein. Ye phase 8 se zaroori hoga.

Telegram ke liye purana **@Fathersigna147lBot** hi use hoga (chat id `7922990181`). Ek token ek waqt mein sirf ek program chala sakta hai, is liye FATHER bot band hona chahiye.

Yaad rakhein: ye bot **sirf paper trading** karta hai. Na wallet, na private key, na asli paisa.
Phone par 24 ghante lagatar chalne ki guarantee nahi — Android, battery aur network kabhi bhi rok sakte hain.
`start.sh` crash ke baad khud dobara chalata hai, lekin phone band ho to bot bhi band hoga.

## Token scanner (phase 2)

```bash
python main.py --debug scan 180            # 3 minutes live: websocket + automatic fallback
python main.py --debug scan 60 fallback    # force the fallback pollers (GeckoTerminal + RugCheck)
```

- **Primary:** one PumpPortal websocket (`subscribeNewToken` + `subscribeMigration`), pings every
  20 s, an idle watchdog (reconnect if silent for 120 s) and reconnect backoff 1 s → 60 s with jitter.
- **Polling (always on by default):** GeckoTerminal `new_pools` (every 20 s, queue ≤ 6/min) and
  RugCheck `new_tokens` (every 6 s, ≤ 30/min) through rate-limit queues that pause on HTTP 429.
  In a live test the free PumpPortal stream delivered only ~19% of new pump.fun launches, so polling
  runs even while the websocket is up (`SCANNER_POLL_ALWAYS=true`). With `false` it is only a
  fallback: it starts after the websocket is down for 20 s and stops when it is back.
- **Record per token:** name, symbol, mint, launch time, source, dev wallet, dev initial buy
  (tokens + SOL), initial market cap (SOL from Pump.fun, USD from GeckoTerminal), status
  `bonding` / `migrated` / `amm` / `unknown`, launchpad, pool and pool address.
- **Migrations:** PumpPortal `migrate` events, or a bonding token that shows up on a new AMM pool.
- **De-dup across restarts:** SQLite table `seen_tokens` (schema versioned with `PRAGMA user_version`,
  so phase 7 only appends migrations).
- **Interface for phase 3:** `async for ev in Scanner(settings, db).stream(): ...` (or `scanner.queue`).

## Data collector (phase 3)

```bash
python main.py --debug collect 300     # scanner + collector live for 5 minutes
```

- **Enrichment:** new tokens from the scanner are looked up on DexScreener `tokens/v1`, 30 mints
  per call through the shared rate-limit queue. The main pair is the SOL/USDC/USDT pair with the
  most liquidity. Fields: price, market cap, FDV, liquidity, volume and buys/sells for m5/h1/h6/h24,
  pair created time, dexId, pair address. Migration status comes from dexId (`pumpfun`/`meteoradbc`
  = bonding; an AMM pair such as `pumpswap`/`raydium`/`meteora` next to it = migrated).
- **Filters / drops:** `too_old` (real age from the oldest pair or launch > `MAX_TOKEN_AGE_MIN`,
  default 120), `no_pair` (no DexScreener pair after `NO_PAIR_TTL_SEC`, default 300 s, re-tried
  10 s → 60 s), `not_meme` (only paired against non-SOL/USDC/USDT tokens, e.g. tokenized stocks or
  pair spam), `inactive` (fewer than `MIN_TRACK_TXNS` trades by the TTL), `tracking_full`.
- **Tracking:** at most `MAX_TRACKED` (40) tokens, a snapshot every `SNAPSHOT_SEC` (15 s). A busier
  token can push out the quietest one after `MIN_HOLD_SEC`. Tracking stops when a token is older
  than the max age (`aged_out`), has no trades for `DEAD_AFTER_MIN` (`dead`), or its pair vanishes
  (`lost`). Tracked tokens resume after a restart.
- **Candles:** 1-minute OHLCV built locally from snapshots. Volume and buy/sell counts are deltas of
  DexScreener's h24 counters, which are cumulative for tokens younger than 24 h. Up to 10 empty
  minutes are filled with flat candles.
- **Extras:** GeckoTerminal 1-minute OHLCV for the `HOT_SIZE` (3) busiest tokens every 90 s;
  Jupiter price v3 fills liquidity (DexScreener has none for pump.fun bonding pairs) and is the
  price fallback when a pair is missing.
- **Retention:** snapshots 6 h, candles 48 h, untracked market rows and drops 7 days, seen tokens
  14 days (cleanup every 10 minutes).
- **Interface for phases 4/5:** `collector.get_market(mint)`, `collector.get_candles(mint, minutes=60)`,
  `collector.candles_df(mint)`, `collector.tracked_mints()`, `collector.updates` (queue).

## Doctor (self-check)

```bash
python -m shadow.doctor            # everything incl. network
python -m shadow.doctor --offline  # skip network
python main.py --debug doctor      # same thing
```

It checks: Python ≥ 3.10, packages, the paper-only guard, `.env` (never prints secrets), write
access to `data/` and `logs/`, free disk, a full SQLite create/write/read/update/reopen cycle,
Termux tools (wake-lock, Termux:Boot) and every data source in
[`shadow/sources.py`](shadow/sources.py). Exit code 0 = no FAIL.

## Data sources

All free and keyless by default. Tested results, limits and the source plan:
**[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)**.
Short version: PumpPortal websocket (new launches + migrations), DexScreener (market data),
GeckoTerminal (new pools, candles), Solana public RPC (mint/freeze authority), RugCheck (risks,
holders), Jupiter (price + sellability quote). Pump.fun's own API is Cloudflare-blocked from servers
and Birdeye needs a key, so neither is required.

## Android limits and how the bot handles them

- `start.sh` takes a **wake-lock**, restarts the bot after a crash with backoff (10 s → 5 min), and rotates `logs/stdout.log` at 5 MB (Python logs rotate at 1 MB × 5).
- `setup_termux.sh` writes `~/.termux/boot/start-shadow-meme.sh`, so with Termux:Boot installed the bot starts after a reboot.
- Exit codes: `2` = config error and `3` = feature not built yet; `start.sh` stops instead of looping on these.
- Network drops: later phases reconnect the websocket and back off on HTTP errors and 429s.

## Project layout

```
main.py                 entry point (bot from phase 8; --debug doctor / scan / collect now)
shadow/__init__.py      version + PAPER_ONLY constant
shadow/config.py        .env loading, safe defaults, paper-only guard
shadow/doctor.py        self-check
shadow/sources.py       verified free endpoints (used by doctor)
shadow/logging_setup.py rotating logs
shadow/scanner.py       phase 2: websocket + fallback scanner, de-dup
shadow/scan_cli.py      `--debug scan` live printer
shadow/models.py        TokenEvent record
shadow/net.py           rate-limit queues + JSON fetch
shadow/collector.py     phase 3: enrichment, age filter, tracking, snapshots, hot set
shadow/candles.py       1-minute candle builder + GeckoTerminal OHLCV parser
shadow/collect_cli.py   `--debug collect` live printer
shadow/safety.py        phase 4 stub    shadow/signals.py    phase 5 stub
shadow/strategies.py    phase 5 stub    shadow/paper.py      phase 6 stub
shadow/db.py            SQLite schema v2: seen_tokens, token_market, price_snapshots, candles_1m, drops
shadow/telegram_bot.py  phase 8 stub
shadow/dashboard.py     phase 9 stub
tests/                  pytest (config, doctor, scanner, collector; recorded responses in tests/fixtures/)
docs/DATA_SOURCES.md    tested data sources
setup_termux.sh  start.sh  .env.example  requirements.txt  requirements-dev.txt
```

## Development

```bash
pip install -r requirements-dev.txt   # + pandas numpy (via pkg on Termux)
python -m pytest -q                   # all tests
python -m pytest -q -m "not network"  # without internet
```
