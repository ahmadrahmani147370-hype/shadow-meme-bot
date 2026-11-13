"""Normalized records shared by all phases."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

NEW_TOKEN = "new_token"
MIGRATION = "migration"

BONDING = "bonding"      # still on a launchpad bonding curve (pump.fun, meteora-dbc, launchlab)
MIGRATED = "migrated"    # graduated to an AMM pool (pumpswap / raydium / meteora-damm)
AMM = "amm"              # first seen directly on an AMM pool (no launchpad seen)
UNKNOWN = "unknown"

# Token universes (phase 4 adds the column everywhere; mcap/volume collectors arrive in phase 5)
U_NEW = "new"          # fresh launches from the scanner (<= MAX_TOKEN_AGE_MIN)
U_MCAP = "mcap"        # any Solana meme coin with market cap >= $1M
U_VOLUME = "volume"    # highest 24h / 1h volume Solana meme coins
UNIVERSES = (U_NEW, U_MCAP, U_VOLUME)


@dataclass
class TokenEvent:
    kind: str                       # new_token | migration
    mint: str
    source: str                     # pumpportal | geckoterminal | rugcheck
    name: str = ""
    symbol: str = ""
    launch_ts: float | None = None  # unix seconds; for PumpPortal = time the create event arrived
    seen_ts: float = field(default_factory=time.time)
    creator: str = ""               # dev wallet (create tx signer)
    dev_buy_tokens: float | None = None
    dev_buy_sol: float | None = None
    initial_mcap_sol: float | None = None
    initial_mcap_usd: float | None = None
    status: str = UNKNOWN           # bonding | migrated | amm | unknown
    launchpad: str = ""             # pump | meteora-dbc | ...
    pool: str = ""                  # pump-amm, pumpswap, raydium ... for migrations / AMM pools
    pool_address: str = ""
    bonding_curve: str = ""
    signature: str = ""
    uri: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("raw")
        return json.dumps(d, ensure_ascii=False)


@dataclass
class MarketData:
    """Latest enriched market state of one token (DexScreener main pair + Jupiter fill-in)."""
    mint: str
    symbol: str = ""
    name: str = ""
    pair_address: str = ""
    dex_id: str = ""
    quote_symbol: str = ""
    price_usd: float | None = None
    price_native: float | None = None
    market_cap: float | None = None
    fdv: float | None = None
    liquidity_usd: float | None = None
    liquidity_source: str = ""          # dexscreener | jupiter
    vol_m5: float = 0.0
    vol_h1: float = 0.0
    vol_h6: float = 0.0
    vol_h24: float = 0.0
    buys_m5: int = 0
    sells_m5: int = 0
    buys_h1: int = 0
    sells_h1: int = 0
    buys_h6: int = 0
    sells_h6: int = 0
    buys_h24: int = 0
    sells_h24: int = 0
    price_change_m5: float | None = None
    price_change_h1: float | None = None
    pair_created_ts: float | None = None   # oldest pair of the token (real age), unix seconds
    launch_ts: float | None = None         # min(scanner launch time, oldest pair)
    migration_status: str = UNKNOWN        # bonding | migrated | amm | unknown
    n_pairs: int = 0
    source: str = "dexscreener"
    updated_ts: float = 0.0
    universe: str = U_NEW

    def age_min(self, now: float | None = None) -> float | None:
        if not self.launch_ts:
            return None
        return ((now or time.time()) - self.launch_ts) / 60.0

    @property
    def txns_h24(self) -> int:
        return self.buys_h24 + self.sells_h24


@dataclass
class Candle:
    mint: str
    ts: int                 # minute start, unix seconds (multiple of 60)
    open: float
    high: float
    low: float
    close: float
    volume_usd: float = 0.0
    buys: int = 0
    sells: int = 0
    n_snapshots: int = 0    # 0 = gap-filled flat candle
    source: str = "local"   # local | geckoterminal
