# SHADOW MEME 🕶️

Solana meme coin scanner + **paper-trading** signal bot in Python, built for **Android Termux**
(no VPS, no paid subscriptions) and controlled **only from Telegram**.

> **Paper-only and analysis-only.** There is no live-trading code, no wallet connection and no
> private key, ever. `PAPER_ONLY` is a constant in the code; `.env` cannot turn it off.
> Signals are not financial advice, and the bot reports only results it actually measured.

## Status: Phase 5 of 10 ✅ (signals, strategies, judge, MCAP / TOP VOLUME universes)

| Phase | What | Status |
|---|---|---|
| 1 | Termux installation, config, doctor, verified data sources | ✅ done |
| 2 | Token scanner (PumpPortal stream + fallbacks) | ✅ done |
| 3 | Data collection (price, mcap, liquidity, volume, buys/sells) | ✅ done |
| 4 | Coin safety checker (strict) | ✅ done |
| 5 | Signal engine + strategies + judge agent + MCAP / TOP VOLUME universes | ✅ done |
| 6 | Paper trading engine ($100, $2/trade, $0.50+$0.50 fees, max 5 open) + self-learning | ⏳ next |
| 7 | SQLite database (positions survive restarts) | ⏳ |
| 8 | Telegram commands, alerts, daily report | ⏳ |
| 9 | Dashboard (Telegram Mini App on GitHub Pages) + self-improving strategies | ⏳ |
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
5. Try the scanner (phase 2): `python main.py --debug scan 120`, the collector (phase 3): `python main.py --debug collect 300`,
   the safety checker (phase 4): `python main.py --debug safety <mint>` or `python main.py --debug safety-live 300`,
   and everything with signals + judge (phase 5): `python main.py --debug signals 600`
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
   Safety test (phase 4): `python main.py --debug safety <mint>` — ek coin ki poori safety report (mint/freeze authority, Token-2022, holders, insiders, dev, liquidity, fake volume, Jupiter par bechne ka test). `python main.py --debug safety-live 300` — 5 minute tak har naye coin ki report, aakhir mein kitne pass/reject hue aur kis wajah se.
   Signals test (phase 5): `python main.py --debug signals 600` — 10 minute tak sab kuch live: naye coins, $1M+ MCAP coins, TOP VOLUME coins, safety, signals aur judge. Har BUY WATCH ke neeche judge ka faisla (APPROVE / VETO) aur wajah. Aakhir mein har list ke signals, judge ki ginti, API calls aur CPU/RAM. `quiet` likhein to REJECT lines screen par nahi aatin (ginti phir bhi hoti hai).
6. Phase 8 ke baad: `nano .env` mein Telegram token aur chat id likhein, phir `./start.sh`.

Scanner do tarah se coins dhoondta hai: PumpPortal websocket (live) aur GeckoTerminal + RugCheck polling. Test mein websocket ne sirf ~19% naye pump.fun coins diye, is liye polling hamesha on rehti hai. Ek coin dobara nahi aata, restart ke baad bhi nahi.

Agar doctor mein `telegram_api` par **ConnectTimeout** aaye (Pakistan ke kuch networks par Telegram block hota hai): doosra network (Wi-Fi ya doosri SIM) try karein, ya muft **1.1.1.1 (WARP)** app on karein. Advanced: `.env` mein `TELEGRAM_API_BASE` apne HTTPS mirror par set karein. Ye phase 8 se zaroori hoga.

Telegram ke liye purana **@Fathersigna147lBot** hi use hoga (chat id `7922990181`). Ek token ek waqt mein sirf ek program chala sakta hai, is liye FATHER bot band hona chahiye.

**Safety checker (phase 4) – Roman Urdu:** har coin ko 0-100 safety score milta hai. Kuch khatre
"CRITICAL" hain — inme se ek bhi ho to coin foran reject, score chahe jitna ho: mint ya freeze
authority on, khatarnak Token-2022 settings (transfer fee, permanent delegate, transfer hook, frozen,
pause — ya koi authority jo baad mein fee/hook laga sake), rugged, liquidity ya price achanak gir
jaye (rug warning), Jupiter par bech na sakein ya $2 bechne par 15% se zyada price impact ya 25% se
zyada nuqsan, ek wallet ke paas 30% ya top 10 ke paas 60% se zyada (pools aur pump.fun ka mayhem
wallet shamil nahi), insiders ke paas 25% se zyada, dev ke paas 20% se zyada, creator ke purane
coins rug hue hon ya wo blacklist mein ho, ek creator ne 24 ghante mein 10+ coins banaye hon,
liquidity $1,500 se kam, wash (jaali) volume, ya bohat trades lekin chand holders. RugCheck report
na mile to faisla UNKNOWN — aise coin par kabhi trade nahi hoga. Sab hadd `.env` mein badal sakte
hain (`SAFETY_...`). Default ab sakht (strict) hain: score 70 se kam = reject. `MAX_TRACKED` ab 200 hai.

**Signals + judge (phase 5) – Roman Urdu:** ab teen lists hain: (1) **naye coins** (pehle jaise,
200 tak), (2) **MCAP** — $1M ya zyada market cap wale meme coins, (3) **TOP VOLUME** — 24 ghante aur
1 ghante mein sab se zyada volume wale coins. SOL, stablecoins, tokenized stocks aur bare coins
(JUP/RAY/BONK/WIF/PUMP) shamil nahi, jab tak `.env` mein `UNIVERSE_ALLOW_MAJORS=true` na ho. Har
coin par 10 detector chalte hain (1-minute aur 5-minute candles): early pump, pehli green candle,
pehla pullback, volume barhna, buyers barhna, buy/sell pressure, breakout, pump thakan (exhaustion),
migration breakout aur achanak dump. Score 0-100: 80+ Strong Watch, 65-79 Watch, 50-64 Weak, 50 se
kam Reject — aur koi CRITICAL safety khatra ho to hamesha REJECT. Teen strategies (Early Entry,
First Pullback, Migration Breakout) ke apne entry/SL/TP/time-stop hain, jo database mein version ke
saath rakhe jaate hain taa ke phase 6 inhein khud behtar kar sake (aur wapas purane version par ja
sake). Har BUY WATCH ko ek alag **judge** dekhta hai (muft, bina AI ke, sirf qaide): der se entry,
kam liquidity, zyada price impact, purani Jupiter quote, dump ka khatra, ek jaise coins mein zyada
watches, ya strategy ka nuqsan — in mein se kuch ho to VETO. Har faisla save hota hai; veto kiye
gaye coins ka bhi baad mein nateeja dekha jaata hai, taa ke pata chale judge sahi tha ya nahi.

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
- **Tracking:** at most `MAX_TRACKED` (200) tokens, a snapshot every `SNAPSHOT_SEC` (15 s) = 7
  DexScreener batch calls per snapshot. A busier token can push out the quietest one after
  `MIN_HOLD_SEC`; a token turned away while all slots are full (`tracking_full`) is kept in a
  backlog and re-checked when a slot frees up, as long as it is still younger than the max age. Tracking stops when a token is older
  than the max age (`aged_out`), has no trades for `DEAD_AFTER_MIN` (`dead`), or its pair vanishes
  (`lost`). Tracked tokens resume after a restart.
- **Candles:** 1-minute OHLCV built locally from snapshots. Volume and buy/sell counts are deltas of
  DexScreener's h24 counters, which are cumulative for tokens younger than 24 h. Up to 10 empty
  minutes are filled with flat candles.
- **Extras:** GeckoTerminal 1-minute OHLCV for the `HOT_SIZE` (3) busiest tokens every 90 s;
  Jupiter price v3 fills liquidity (DexScreener has none for pump.fun bonding pairs) and is the
  price fallback when a pair is missing.
- **Retention:** snapshots 3 h, candles 6 h, untracked market rows and drops 2 days, seen tokens
  2 days, safety reports 3 h after a token stops being tracked, RugCheck cache 1 h (cleanup every
  10 minutes). Estimated steady state at 200 tracked tokens: roughly 100 MB of SQLite.
- **Bags** (`dexId` `bags`, Meteora DBC) is treated as a bonding launchpad like pump.fun.
- **Interface for phases 4/5:** `collector.get_market(mint)`, `collector.get_candles(mint, minutes=60)`,
  `collector.candles_df(mint)`, `collector.tracked_mints()`, `collector.updates` (queue).

## Coin safety checker (phase 4)

```bash
python main.py --debug safety <mint>        # full report for one token incl. a Jupiter sell quote
python main.py --debug safety-live 300      # scanner + collector + safety for 5 minutes, with counts
```

Each tracked token gets a **SafetyReport**: a 0-100 safety sub-score, flags with severity
(`info` / `warn` −5 / `danger` −15 / `critical`) and a verdict `PASS`, `REJECT` or `UNKNOWN`.
Defaults are **strict**: score below `SAFETY_MIN_SCORE` (70) = reject, and **any critical flag rejects
whatever the score**. No RugCheck report = `UNKNOWN`, which is never tradable.

| Check | Source | Critical when |
|---|---|---|
| Mint / freeze authority | Solana RPC `getMultipleAccounts` (100 mints per call), publicnode fallback, RugCheck copy as last fallback | either is still set |
| Token-2022 extensions | same RPC call | transfer fee > 0, fee authority, permanent delegate, non-transferable, transfer hook (or hook authority), default frozen, pausable |
| Holders | RugCheck full report (cached 15 min) with pools, lockers, bonding curves and the pump.fun *mayhem* wallet excluded | top-1 ≥ 30% or top-10 ≥ 60% |
| Insiders / clusters | RugCheck `insiderNetworks`, insider holders | insiders ≥ 25% (cluster of 10+ wallets = warn) |
| Dev wallet | RugCheck creator + balance, scanner dev buy, tokens per creator in 24 h, learned blacklist | dev holds ≥ 20%, creator has rugged tokens, blacklisted creator, 10+ tokens in 24 h |
| Liquidity | DexScreener / Jupiter (collector) or RugCheck | below $1,500 (new launches) or $10k (mcap / volume universes) |
| Fake volume | collector buys/sells/volume/liquidity + RugCheck holders | 1 h volume ≥ 100× liquidity, or 150+ trades with fewer than 20 holders |
| Rug pull warning | price snapshots + DexScreener h1 change | liquidity −50% or price −80% from peak, or −70% in 1 h |
| Sellability | Jupiter `swap/v1/quote` token → SOL for `PAPER_TRADE_SIZE` | no route, impact ≥ 15%, or more than 25% of the value lost |
| RugCheck | `rugged` flag; "creator history of rugged tokens"; other danger risks | rugged / creator history |

Budget at 200 tracked tokens (all keyless): RugCheck reports ≤ `SAFETY_RC_PER_MIN` (30/min; the
scanner adds ~10/min), Jupiter quotes ≤ `SAFETY_QUOTES_PER_MIN` (8/min, only for passing, active
tokens; since phase 5 only coins near a real signal), RPC ~5/min. New tokens are checked first, then
expired reports, busiest first. Re-checks: every 15 min, rejected tokens every 30 min, quotes valid
3 min. All thresholds are in `.env` (`SAFETY_...`).

**Interface for phase 5:** `checker.check(mint, quote=True)`, `checker.get_report(mint)`,
`report.trade_ready(quote_ttl)` (PASS + fresh sellable quote), `checker.request_quote(mint)`.

**Prepared for the next phases:** every table that holds tokens has a `universe` column
(`new` / `mcap` / `volume`) and `universe_members` records that one coin can sit in several lists
(e.g. MCAP ≥ $1M *and* TOP VOLUME). The safety checker already uses stricter liquidity floors for the
`mcap` / `volume` universes. Reports are stored with every flag, severity and penalty, so the phase 5
judge agent can cite exact reasons when it vetoes a signal, and phase 9's self-tuning can compare
safety flags against closed paper-trade results.

**10-minute live test at MAX_TRACKED=200 (13 Nov 2026, 08:30–08:40 PKT):** 255 tokens checked,
90 passed, 165 rejected (by critical reason: creator history of rugged tokens 127, serial creator 124,
liquidity below the floor 86, rug warning 34, extreme holder concentration 8, wash volume 5, extreme
insiders 2, few makers 2; low score only 2). Tracking reached the 200 cap after ~8 minutes. API
calls per minute: DexScreener 29, RugCheck 33 (scanner + safety), Jupiter 9.5, GeckoTerminal 4.4,
RPC 5.3; **zero 429s, zero errors**. CPU 0.7% average, RSS 51 MB, SQLite +3.7 MB in 10 minutes.

## Signals, strategies, judge and universes (phase 5)

```bash
python main.py --debug signals 600          # everything live for 10 minutes, signals + judge verdicts per universe
python main.py --debug signals 600 quiet    # same, without printing every REJECT line (still counted)
```

**Universes** (`shadow/universes.py`), each with its own cap and refresh, no age limit:

| Universe | Members | Source | Detector / strategy parameters |
|---|---|---|---|
| `new` | new launches (phase 3 collector, `MAX_TRACKED`=200) | PumpPortal + GeckoTerminal + RugCheck | new-launch set |
| `mcap` | Solana meme coins with market cap ≥ `UNIVERSE_MCAP_MIN` ($1M), liquidity ≥ $25k, max 100 | GeckoTerminal trending / top 24h volume / top tx pools + DexScreener top boosts + new launches that grew past $1M, ranked via DexScreener | calmer: smaller moves count |
| `volume` | top `UNIVERSE_VOLUME_TOP_N` (30) by 24h volume ∪ top 30 by 1h volume (min $300k / $25k), max 50 | same lists | in between |

A coin can be in several lists (`universe_members` keeps rank, 1h rank, mcap, volumes, active flag); a $1M+
coin is evaluated with the MCAP parameters. The new-launch enrichment no longer overwrites a coin's universe.
SOL, stables, LSTs, tokenized stocks and majors (JUP, RAY, BONK, WIF, PUMP, JTO, PYTH, ORCA) are dropped
unless `UNIVERSE_ALLOW_MAJORS=true`; `UNIVERSE_EXCLUDE_MINTS` adds your own. Universe coins are snapshotted
every 30 s (30 coins per DexScreener call) into the same 1m candles; lists are re-discovered every 5 min.

**Detectors** (`shadow/signals.py`, local 1m candles + 5m resample + market data, one parameter set per universe):
early pump, first green candle after a red run, first pullback (impulse → dip that holds 38.2% → bounce),
volume increase (1m relative-volume z-score confirmed on 5m), buyer increase (buys/min growth with a
rising buy ratio), buy/sell pressure, breakout above the lookback high with volume, pump exhaustion
(bearish), migration breakout (above the post-migration high), sudden dump warning (bearish).
Momentum is weighted by liquidity, and the 5m candle confirms or contradicts.

**Score 0-100** = pattern (≤35) + flow (volume 12, buyers 10, pressure 13) + quality (safety score,
5m confirmation, liquidity) − bearish (exhaustion, dump). **80-100 Strong Watch, 65-79 Watch,
50-64 Weak, <50 Reject; a critical safety flag is always REJECT.** Alert types: `BUY_WATCH`, `SELL`
(SL / TP / dump / exhaustion / time stop of an approved watch), `HOLD` (every 10 min), `REJECT`.

**Strategies** (`shadow/strategies.py`): `early_entry` (new only), `first_pullback` (all), `migration_breakout`
(all), each with entry / SL / TP / time stop per universe. Parameters live in the `strategy_params` table
(version, active, source, parent version); version 1 is seeded from the defaults, phase 6 adds learned
versions with `new_version()` and can `rollback()`. Every signal stores its `params_version`.

**Judge agent** (`shadow/judge.py`, rule-based, free, no LLM) reviews every BUY WATCH with the signal,
safety report, market features, open watches, strategy health and its own past accuracy:
hard vetoes = safety not PASS, no/stale sell quote, sell impact > 3% or round-trip loss > 10%, liquidity
below $8k (new) / $75k (mcap, volume), late entry after an extended move, conflicting dump or exhaustion,
5 approved watches open, same creator or symbol already open, too many same-strategy watches, a
strategy in drawdown (10+ closed, ≥20 points down or 5 losses in a row). Soft reasons (weak flow, thin
score margin, no 5m confirmation, few candles, judge doing worse than chance) lower the confidence;
APPROVE needs no hard veto and confidence ≥ 0.60. Every verdict goes to `judge_verdicts` with its inputs.
Approved **and vetoed** watches are followed virtually until SL / TP / time stop, so phase 6 can compare
approved vs vetoed outcomes. Vetoed watches are tracked silently (no SELL alert).

The safety checker now quotes Jupiter only for coins near a real signal (a strategy setup scoring ≥
`QUOTE_NEAR_SCORE`), checks signal candidates first, and skips RugCheck for 30 min when it cannot build
a report (common for big coins).

**11-minute live test (13 Nov 2026, 09:50–10:01 PKT, `--debug signals 660 quiet`, all real):**
new launches reached the 200 cap; MCAP 13 members (UODF $265M … DESSA $5.7M), TOP VOLUME 19 members
(USPR, mihao, FOOD, Max, CATE, STONK, GTA6, SHARKBUX …), 40 list entries filtered (36 not memes, 4 majors).
6,073 evaluations (new 5,253, mcap 533, volume 287). Signals: **51, all from new launches** —
3 BUY_WATCH (early_entry, class WATCH) and 48 REJECT (29 critical safety, the rest score < 65).
**Judge: 2 APPROVE, 1 VETO** (low confidence: score 67 with only 2 candles and no 5m confirmation).
**No MCAP or TOP VOLUME signal appeared** in 11 minutes: these coins start with no local candle
history, and no first-pullback / migration setup formed in the window. No watch closed yet, so there
are no outcomes to report. API calls/min: DexScreener 30.6, RugCheck 29.3, GeckoTerminal 4.8,
Jupiter 4.5, RPC 4.0; **zero 429s**; 1 error (a RugCheck read timeout). CPU 1.0% average, RSS 54 MB,
SQLite 3.9 MB after 11 minutes.

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
main.py                 entry point (bot from phase 8; --debug doctor / scan / collect / safety / signals now)
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
shadow/safety.py        phase 4: SafetyChecker, SafetyReport, strict rules
shadow/safety_cli.py    `--debug safety` / `safety-live` printers
shadow/universes.py     phase 5: MCAP / TOP VOLUME discovery, meme filter, ranking, 30 s snapshots
shadow/signals.py       phase 5: features, 10 detectors, score, SignalEngine (BUY_WATCH/SELL/HOLD/REJECT)
shadow/strategies.py    phase 5: 3 strategies, versioned params (strategy_params)
shadow/judge.py         phase 5: rule-based judge agent (APPROVE / VETO + reasons)
shadow/signals_cli.py   `--debug signals` live printer
shadow/paper.py         phase 6 stub
shadow/db.py            SQLite schema v4: seen_tokens, token_market, price_snapshots, candles_1m, drops,
                        safety_reports, rugcheck_cache, creator_blacklist, universe_members, universe_runs,
                        signals, judge_verdicts, strategy_params (+ `universe` column: new / mcap / volume)
shadow/telegram_bot.py  phase 8 stub
shadow/dashboard.py     phase 9 stub
tests/                  pytest (config, doctor, scanner, collector, safety, universes, signals, strategies, judge;
                        recorded responses + real candles in tests/fixtures/)
tools/                  record_candles_fixture.py (rebuilds the real-candle fixture)
docs/DATA_SOURCES.md    tested data sources
setup_termux.sh  start.sh  .env.example  requirements.txt  requirements-dev.txt
```

## Development

```bash
pip install -r requirements-dev.txt   # + pandas numpy (via pkg on Termux)
python -m pytest -q                   # all tests
python -m pytest -q -m "not network"  # without internet
```
