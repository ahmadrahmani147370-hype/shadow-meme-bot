"""Registry of the free data sources SHADOW MEME uses (verified 2026-11-13, see docs/DATA_SOURCES.md).

The doctor probes every entry here. `required=True` means the bot can not do its job without it;
optional ones only produce a warning when they fail (for example Pump.fun's frontend API, which
Cloudflare blocks for datacenter IPs and may or may not allow from a phone's mobile IP).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Well-known mints used only as probe targets.
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WSOL = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True)
class Endpoint:
    name: str
    role: str                 # what the bot uses it for
    url: str
    kind: str = "http"        # http | rpc | ws
    method: str = "GET"
    rpc_method: str = ""
    rpc_params: tuple = ()
    expect: str = ""          # a JSON key/substring that must appear in a healthy answer
    required: bool = True
    key_needed: str = "no"    # no | optional | yes
    limit: str = ""           # documented / observed limit
    headers: dict = field(default_factory=dict)


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("pumpportal_ws", "new Pump.fun launches + migrations (stream)",
             "wss://pumpportal.fun/api/data", kind="ws", expect="subscribed",
             limit="1 websocket connection per client; new-token + migration streams free; per-token trade stream is paid"),
    Endpoint("dexscreener_profiles", "newest token profiles (discovery)",
             "https://api.dexscreener.com/token-profiles/latest/v1", expect="tokenAddress", limit="60 req/min"),
    Endpoint("dexscreener_tokens", "price, mcap, liquidity, volume, buys/sells, pair age (batch up to 30)",
             f"https://api.dexscreener.com/tokens/v1/solana/{BONK}", expect="pairAddress", limit="300 req/min"),
    Endpoint("dexscreener_search", "search fallback (pairs by name/symbol)",
             "https://api.dexscreener.com/latest/dex/search?q=BONK", expect="pairs", limit="300 req/min"),
    Endpoint("geckoterminal_new_pools", "new Solana pools (pump-fun, pumpswap, raydium, meteora)",
             "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1", expect="pool_created_at",
             limit="~10 req/min official (keyless, shared per IP)"),
    Endpoint("solana_rpc", "mint + freeze authority, supply, signatures",
             "https://api.mainnet-beta.solana.com", kind="rpc", method="POST", rpc_method="getAccountInfo",
             rpc_params=(BONK, {"encoding": "jsonParsed"}), expect="mintAuthority",
             limit="free tier; per-method limits (getAccountInfo ~50/10s); getTokenLargestAccounts is 429 (limit 0)"),
    Endpoint("solana_rpc_fallback", "fallback RPC for getAccountInfo",
             "https://solana-rpc.publicnode.com", kind="rpc", method="POST", rpc_method="getAccountInfo",
             rpc_params=(BONK, {"encoding": "jsonParsed"}), expect="mintAuthority", required=False,
             limit="free; getTokenLargestAccounts blocked"),
    Endpoint("rugcheck_summary", "risk list, score, LP locked %",
             f"https://api.rugcheck.xyz/v1/tokens/{BONK}/report/summary", expect="score",
             key_needed="optional", limit="free: ~1-3 req/s, cached reports only"),
    Endpoint("jupiter_price", "USD price fallback (up to 50 mints per call)",
             f"https://api.jup.ag/price/v3?ids={BONK}", expect="usdPrice", key_needed="optional",
             limit="keyless 0.5 req/s (30/min), shared bucket with quote"),
    Endpoint("jupiter_quote", "sellability check (token -> SOL route + price impact)",
             f"https://api.jup.ag/swap/v1/quote?inputMint={BONK}&outputMint={WSOL}&amount=1000000000000&slippageBps=500",
             expect="outAmount", key_needed="optional", limit="keyless 0.5 req/s (30/min), shared bucket with price"),
    # required=False until phase 8 wires Telegram: a blocked network only WARNs for now.
    Endpoint("telegram_api", "alerts + commands (phase 8)", "https://api.telegram.org/", expect="",
             required=False, limit="~30 msg/s overall, ~1 msg/s per chat"),
    Endpoint("pumpfun_frontend", "Pump.fun coin lists / king-of-the-hill (optional extra)",
             "https://frontend-api-v3.pump.fun/coins?offset=0&limit=1&sort=created_timestamp&order=DESC&includeNsfw=false",
             expect="mint", required=False, limit="undocumented; Cloudflare 403 from datacenter IPs"),
    Endpoint("birdeye", "not used (needs API key)",
             f"https://public-api.birdeye.so/defi/price?address={BONK}", expect="value", required=False,
             key_needed="yes", headers={"x-chain": "solana"}, limit="401 Unauthorized without X-API-KEY"),
)


def by_name(name: str) -> Endpoint:
    for e in ENDPOINTS:
        if e.name == name:
            return e
    raise KeyError(name)
