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
