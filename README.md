# SHADOW MEME 🕶️

Solana meme coin scanner + **paper-trading** signal bot in Python, built for **Android Termux**
(no VPS, no paid subscriptions) and controlled **only from Telegram**.

> **Paper-only and analysis-only.** There is no live-trading code, no wallet connection and no
> private key, ever. `PAPER_ONLY` is a constant in the code; `.env` cannot turn it off.
> Signals are not financial advice, and the bot reports only results it actually measured.

## Status: Phase 1 of 10 ✅ (installation + foundation)

| Phase | What | Status |
|---|---|---|
| 1 | Termux installation, config, doctor, verified data sources | ✅ done |
| 2 | Token scanner (PumpPortal stream + fallbacks) | ⏳ next |
| 3 | Data collection (price, mcap, liquidity, volume, buys/sells) | ⏳ |
| 4 | Coin safety checker | ⏳ |
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
5. From phase 8: `nano .env` (Telegram token + chat id), then `./start.sh`.

Update later: `cd shadow-meme-bot && git pull && python -m shadow.doctor`

## Roman Urdu quick start 🇵🇰

1. F-Droid se **Termux** install karein (Play Store wala purana hai). Chahein to **Termux:API** aur **Termux:Boot** bhi.
2. Termux khol kar ye ek line chalayein:

```bash
pkg install -y git python && git clone https://github.com/ahmadrahmani147370-hype/shadow-meme-bot && cd shadow-meme-bot && bash setup_termux.sh
```

3. Sab theek hai ya nahi, check karein: `python -m shadow.doctor` — aakhir mein `READY` aana chahiye.
4. Phone ki Settings → Apps → Termux → Battery → **Unrestricted** kar dein, warna Android background mein band kar deta hai.
5. Phase 8 ke baad: `nano .env` mein Telegram token aur chat id likhein, phir `./start.sh`.

Yaad rakhein: ye bot **sirf paper trading** karta hai. Na wallet, na private key, na asli paisa.
Phone par 24 ghante lagatar chalne ki guarantee nahi — Android, battery aur network kabhi bhi rok sakte hain.
`start.sh` crash ke baad khud dobara chalata hai, lekin phone band ho to bot bhi band hoga.

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
main.py                 entry point (bot from phase 8, --debug doctor now)
shadow/__init__.py      version + PAPER_ONLY constant
shadow/config.py        .env loading, safe defaults, paper-only guard
shadow/doctor.py        self-check
shadow/sources.py       verified free endpoints (used by doctor)
shadow/logging_setup.py rotating logs
shadow/scanner.py       phase 2 stub    shadow/collector.py  phase 3 stub
shadow/safety.py        phase 4 stub    shadow/signals.py    phase 5 stub
shadow/strategies.py    phase 5 stub    shadow/paper.py      phase 6 stub
shadow/db.py            phase 7 stub    shadow/telegram_bot.py phase 8 stub
shadow/dashboard.py     phase 9 stub
tests/                  pytest (config + doctor; live network test marked `network`)
docs/DATA_SOURCES.md    tested data sources
setup_termux.sh  start.sh  .env.example  requirements.txt  requirements-dev.txt
```

## Development

```bash
pip install -r requirements-dev.txt   # + pandas numpy (via pkg on Termux)
python -m pytest -q                   # all tests
python -m pytest -q -m "not network"  # without internet
```
