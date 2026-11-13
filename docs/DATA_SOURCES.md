# SHADOW MEME – free data sources (verified)

All results below come from real requests made on **13 Nov 2026, 06:23–06:32 PKT (UTC+5)** from a
Linux server in a US datacenter (Cloudflare edge IAD). A phone on mobile data may get different
answers, especially from Cloudflare-protected sites, so always run `python -m shadow.doctor` on the
phone itself. Rate limits are from each provider's docs or response headers on that date.

## Results

| Source | Endpoint tested | Result | Latency | Key needed | Limit (docs / headers) |
|---|---|---|---|---|---|
| **PumpPortal websocket** | `wss://pumpportal.fun/api/data` → `subscribeNewToken`, `subscribeMigration` | ✅ connected, both acknowledged; received live `create` events and a `migrate` event | ~0.9–1.4 s connect | No (for these two streams) | Docs: use **one** websocket connection; new-token + migration streams free. `subscribeTokenTrade`/`subscribeAccountTrade` need an API key + funded wallet (0.01 SOL / 10k msgs) → **not used** |
| **DexScreener** token-profiles | `/token-profiles/latest/v1` | ✅ 200, 30 profiles (28 Solana) | 80–110 ms | No | 60 req/min |
| **DexScreener** tokens (batch) | `/tokens/v1/solana/{up to 30 mints}` | ✅ 200; returns price, mcap, fdv, liquidity, volume m5/h1/h6/h24, **buys/sells** m5/h1/h6/h24, `pairCreatedAt`, `dexId` (`pumpfun` while on the bonding curve, `pumpswap`/`raydium` after migration). Worked for a pump.fun token ~1 min old | 45–95 ms | No | 300 req/min |
| DexScreener legacy / pairs / search | `/latest/dex/tokens/{mint}`, `/latest/dex/pairs/solana/{pair}`, `/token-pairs/v1/solana/{mint}`, `/latest/dex/search` | ✅ 200 | 40–120 ms | No | 300 req/min |
| **GeckoTerminal** new pools | `/api/v2/networks/solana/new_pools` | ✅ 200, 20 pools from `pump-fun`, `pumpswap`, `meteora-dbc`, `meteora-damm-v2` | 95–105 ms | No | Official docs: **~10 calls/min** keyless (CoinGecko keyless page says ~5/min, shared per IP) |
| GeckoTerminal OHLCV | `/networks/solana/pools/{pool}/ohlcv/minute?aggregate=1` | ✅ 200, 1-minute candles | ~390 ms | No | same pool as above; data cached 1 min |
| GeckoTerminal token pools | `/networks/solana/tokens/{mint}/pools` | ✅ 200 (empty for a token seconds old, filled ~1 min later) | 80–250 ms | No | same |
| **Solana public RPC** | `api.mainnet-beta.solana.com` `getAccountInfo` (jsonParsed) | ✅ 200; mint + freeze authority, supply, decimals, Token-2022 extensions | 0.75–1.8 s | No | Headers: tier `free`, getAccountInfo 50 per window, getSignaturesForAddress 10, 40 connections, 5 pubsub |
| Solana public RPC | `getTokenLargestAccounts` | ❌ **429** (method limit = 0 on the free tier) | – | – | blocked |
| Solana public RPC | `getSignaturesForAddress`, `getHealth` | ✅ 200 | ~1.5 s | No | see above |
| publicnode RPC | `solana-rpc.publicnode.com` `getAccountInfo` | ✅ 200 | ~100 ms | No | `getTokenLargestAccounts` → 403 "Request blocked" |
| drpc / ankr RPC | `solana.drpc.org`, `rpc.ankr.com/solana` | ❌ need paid plan / key | – | Yes | not used |
| **RugCheck** summary | `api.rugcheck.xyz/v1/tokens/{mint}/report/summary` | ✅ 200; risks list, score, `score_normalised`, `lpLockedPct`. Worked for 1-minute-old pump.fun tokens (flagged a copycat) | 0.5–1.5 s | No (optional key) | Header `x-rate-limit-limit: 15`; docs: free ≈ 1–3 req/s, **cached reports only** |
| RugCheck full report | `/v1/tokens/{mint}/report` | ✅ 200; `creator`, `creatorBalance`, **`topHolders` (20)**, `insiderNetworks`, `graphInsidersDetected`, `markets`, `totalHolders`, `rugged`, `launchpad` | ~1.2 s | No | same |
| RugCheck new tokens | `/v1/stats/new_tokens` | ✅ 200 | ~1.7 s | No | same |
| **Jupiter** price | `api.jup.ag/price/v3?ids=` | ✅ 200 (up to 50 mints). Brand-new tokens return only `createdAt`/`launchpad` until they have liquidity | 0.9–1.9 s | No (optional free key) | Keyless **0.5 req/s (30/min)**, free key 1 req/s; one shared bucket for price + quote |
| **Jupiter** quote (sellability) | `api.jup.ag/swap/v1/quote` (token → SOL) and `/swap/v2/order` without taker | ✅ 200; `outAmount`, `priceImpactPct`, route | 0.85–1.7 s | No | same bucket |
| Jupiter lite-api | `lite-api.jup.ag/price/v3`, `/swap/v1/quote` | ✅ still answered, **but announced shut down on 30 Oct 2026** → not used | ~1.2–1.7 s | – | – |
| Jupiter old | `quote-api.jup.ag/v6`, `api.jup.ag/price/v2` | ❌ SSL EOF / 404 (retired) | – | – | – |
| **Pump.fun frontend API** | `frontend-api-v3/v2/frontend-api.pump.fun/coins…`, `/coins/king-of-the-hill`, `advanced-api-v2.pump.fun/coins/graduated` | ❌ **403 Cloudflare** ("Just a moment…" / `access_denied`), also with browser headers | 30–135 ms | No (undocumented) | Blocks datacenter IPs. May work from a phone's mobile IP – the doctor checks it and only warns |
| **Birdeye** | `public-api.birdeye.so/defi/price` | ❌ **401 Unauthorized** without `X-API-KEY` | ~60 ms | **Yes** | not used |
| Telegram Bot API | `api.telegram.org` | ✅ 200 | ~0.8 s | Bot token | ~1 msg/s per chat |

## Phase 2 finding: PumpPortal free stream coverage (13 Nov 2026, ~07:00–07:03 PKT)

A 120-second side-by-side run (websocket vs. polling RugCheck `new_tokens` every 10 s and
GeckoTerminal `new_pools` every ~30 s) showed the free PumpPortal `subscribeNewToken` stream
delivered **14 creates**, while GeckoTerminal listed **27 new pump-fun bonding pools** created in the
same window, of which the websocket had seen only **5 (~19%)**. Of 42 pump-suffix mints RugCheck
listed in the window, the websocket had seen 5. So the websocket alone misses most launches.
Phase 2 therefore polls GeckoTerminal (every 20 s) and RugCheck (every 6 s) **all the time**
(`SCANNER_POLL_ALWAYS=true`), not only when the websocket is down. Migration events from the
websocket did arrive (1 in each 3-minute run).

Other observations:
- RugCheck `new_tokens` returns only the latest 10 mints and covers every new SPL mint (not only
  meme launchpads), so it needs enrichment (name, pool) in phase 3. Polling every 6 s still misses some.
- GeckoTerminal `new_pools` reports new *pools*, so an old token that gets a new AMM pool (e.g. a
  $2M-mcap token on meteora-damm-v2) appears "new". Phase 3 must filter by real token age.
- One wallet (`DMdCJk…`) created many 0-SOL "zero"/spam tokens – a good dev-wallet signal for phase 4.

## Source plan for phases 2–4

| Need | Primary | Fallback |
|---|---|---|
| New Pump.fun launches | PumpPortal `subscribeNewToken` (push, free) **plus** always-on polling (see finding above) | GeckoTerminal `new_pools` (pump-fun dex) every 20 s; RugCheck `/stats/new_tokens` every 6 s |
| Pump.fun → PumpSwap/Raydium migration | PumpPortal `subscribeMigration` | DexScreener `dexId` change `pumpfun` → `pumpswap`/`raydium`; GeckoTerminal new pools on `pumpswap`/`raydium` |
| Other new meme coins (Raydium, Meteora) | GeckoTerminal `new_pools` | DexScreener `token-profiles/latest` + `token-boosts/latest` |
| Price, mcap, liquidity, volume, buys/sells, age | DexScreener `/tokens/v1/solana/` batched 30 mints per call | GeckoTerminal pool attributes (`transactions`, `volume_usd`, `reserve_in_usd`); Jupiter price v3 for price only |
| Candles (first green candle, pullback, breakout) | Build 1-minute candles locally from DexScreener price polls stored in SQLite | GeckoTerminal OHLCV minute (only for a few hot tokens – 10 calls/min) |
| Mint / freeze authority | Solana RPC `getAccountInfo` jsonParsed | publicnode RPC; RugCheck `token.mintAuthority/freezeAuthority` |
| Dev wallet, top holders, insiders | RugCheck full report (`creator`, `creatorBalance`, `topHolders`, `insiderNetworks`) | PumpPortal create event `traderPublicKey` + `initialBuy` for the dev's first buy. (`getTokenLargestAccounts` is blocked on free RPCs; a free Helius/QuickNode key in `SOLANA_RPC_URL` would re-enable it) |
| Rug / LP risk | RugCheck summary (`risks`, `score_normalised`, `lpLockedPct`) | local rules on liquidity + holder concentration |
| Sellability | Jupiter `swap/v1/quote` token → SOL for the paper position size: no route or huge price impact = not sellable | Token-2022 extension check via RPC (transfer fee / non-transferable / permanent delegate) |
| Fake volume | Local heuristics on DexScreener buys/sells vs volume vs liquidity, plus RugCheck insider flags | – |

## Budget per minute (keyless)

Jupiter 30, GeckoTerminal ~10, DexScreener 300 (tokens) / 60 (profiles), RugCheck ≈ 60–180, public
RPC getAccountInfo ≈ 50 per window. The phase 2–4 code will use one shared async rate limiter per
source, cache safety results per mint (they rarely change), and back off on 429.

## Risks

- **Pump.fun frontend API is Cloudflare-blocked** here. The bot does not depend on it.
- **PumpPortal** is a free third-party service: it can change or ban clients that open many
  connections. The bot keeps exactly one connection and reconnects with backoff.
- **GeckoTerminal keyless limit is tiny** (~10/min), so it is only a fallback.
- **Jupiter keyless** is 0.5 req/s for price + quote together; sellability checks must be queued.
  A free Jupiter key (developers.jup.ag) doubles it.
- **RugCheck free tier serves cached reports**, so a very new token may show a stale or partial report.
- **getTokenLargestAccounts is blocked on every free public RPC tested.**
- Free APIs change without notice. The doctor exists to catch that: run it after every update.
