"""Settings loaded from .env with safe defaults.

- Secrets (Telegram token, optional API keys) are read but never printed; use Settings.redacted().
- PAPER_ONLY is always True. Setting PAPER_ONLY=false in .env is ignored with a warning.
- Any *PRIVATE_KEY* / *SEED* / *MNEMONIC* style variable is ignored with a warning: the bot never needs one.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import PAPER_ONLY

log = logging.getLogger("shadow.config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRET_FIELDS = ("telegram_bot_token", "jupiter_api_key", "birdeye_api_key", "rugcheck_api_key")
FORBIDDEN_ENV_HINTS = ("PRIVATE_KEY", "SECRET_KEY", "SEED", "MNEMONIC", "WALLET_KEY", "KEYPAIR")


def _load_dotenv(path: Path) -> None:
    """Load .env into os.environ without overriding variables already set."""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        return
    except ImportError:  # minimal fallback parser so the doctor still works
        pass
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _str(env: dict, key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _url(env: dict, key: str, default: str, warnings: list) -> str:
    v = _str(env, key, default).rstrip("/")
    if not v.startswith("https://"):
        warnings.append(f"{key}={v!r} must start with https://, using default {default}")
        return default
    return v


def _num(env: dict, key: str, default, cast, warnings: list, lo=None, hi=None):
    raw = (env.get(key) or "").strip()
    if raw == "":
        return default
    try:
        val = cast(raw)
    except ValueError:
        warnings.append(f"{key}={raw!r} is not a valid number, using default {default}")
        return default
    if (lo is not None and val < lo) or (hi is not None and val > hi):
        warnings.append(f"{key}={raw} is outside [{lo}, {hi}], using default {default}")
        return default
    return val


@dataclass(frozen=True)
class Settings:
    # Telegram (phase 8). Commands are accepted only from telegram_chat_id.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"  # phase 8; change if api.telegram.org is blocked

    # Data sources. All free / keyless by default.
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_rpc_fallback: str = "https://solana-rpc.publicnode.com"
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    jupiter_api_key: str = ""   # optional: free key raises Jupiter from 0.5 to 1 req/s
    birdeye_api_key: str = ""   # optional: Birdeye refuses all requests without a key
    rugcheck_api_key: str = ""  # optional
    http_timeout_sec: float = 15.0

    # Storage
    db_path: str = "data/shadow.db"
    log_dir: str = "logs"
    log_level: str = "INFO"

    # Scanner
    scan_interval_sec: int = 20
    max_token_age_min: int = 120
    scanner_poll_always: bool = True   # PumpPortal's free stream misses most launches -> also poll (see docs)
    gt_poll_sec: int = 20
    rc_poll_sec: int = 6
    ws_idle_timeout_sec: int = 120
    fallback_after_sec: int = 20

    # Collector (phase 3)
    max_tracked: int = 40          # phone friendly cap on tokens polled for snapshots
    min_track_txns: int = 5        # a token needs this many trades before it is tracked
    min_hold_sec: int = 120        # a tracked token can't be evicted by a busier one before this
    snapshot_sec: int = 15
    enrich_sec: int = 5
    no_pair_ttl_sec: int = 300     # give up on tokens without a DexScreener pair after this
    dead_after_min: int = 10       # stop tracking a token with no trades for this long
    hot_size: int = 3              # tokens that also get GeckoTerminal OHLCV
    hot_refresh_sec: int = 90
    jup_refresh_sec: int = 60      # Jupiter price/liquidity fill-in for tracked tokens
    snapshot_retention_h: int = 6
    candle_retention_h: int = 48

    # Paper trading (phase 6). Money values are virtual USD.
    paper_start_balance: float = 100.0
    paper_trade_size: float = 2.0
    paper_buy_fee: float = 0.50
    paper_sell_fee: float = 0.50
    paper_slippage_pct: float = 3.0
    paper_max_open: int = 5
    paper_daily_target: int = 50   # a target, never a quota: weak signals are never forced
    min_trade_score: int = 65      # only "Watch" (65-79) or "Strong Watch" (80-100) may open a paper trade

    paper_only: bool = field(default=PAPER_ONLY, init=False)
    warnings: tuple = ()

    def redacted(self) -> dict:
        d = asdict(self)
        for k in SECRET_FIELDS:
            d[k] = "set" if d[k] else "empty"
        return d

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_settings(env_file: str | Path | None = None, env: dict | None = None) -> Settings:
    """Build Settings. `env` (a dict) is used by tests instead of os.environ."""
    if env is None:
        _load_dotenv(Path(env_file) if env_file else PROJECT_ROOT / ".env")
        env = dict(os.environ)
    w: list[str] = []

    raw_paper = _str(env, "PAPER_ONLY", "true").lower()
    if raw_paper not in ("1", "true", "yes", "on"):
        w.append("PAPER_ONLY can not be disabled. Live trading does not exist in this bot; staying paper-only.")
    for key in env:
        if any(h in key.upper() for h in FORBIDDEN_ENV_HINTS) and env.get(key):
            w.append(f"{key} is ignored: SHADOW MEME never needs a private key, seed or wallet secret. Remove it from .env.")

    s = Settings(
        telegram_bot_token=_str(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str(env, "TELEGRAM_CHAT_ID"),
        telegram_api_base=_url(env, "TELEGRAM_API_BASE", Settings.telegram_api_base, w),
        solana_rpc_url=_str(env, "SOLANA_RPC_URL", Settings.solana_rpc_url),
        solana_rpc_fallback=_str(env, "SOLANA_RPC_FALLBACK", Settings.solana_rpc_fallback),
        pumpportal_ws=_str(env, "PUMPPORTAL_WS", Settings.pumpportal_ws),
        jupiter_api_key=_str(env, "JUPITER_API_KEY"),
        birdeye_api_key=_str(env, "BIRDEYE_API_KEY"),
        rugcheck_api_key=_str(env, "RUGCHECK_API_KEY"),
        http_timeout_sec=_num(env, "HTTP_TIMEOUT_SEC", 15.0, float, w, 2, 120),
        db_path=_str(env, "DB_PATH", Settings.db_path),
        log_dir=_str(env, "LOG_DIR", Settings.log_dir),
        log_level=_str(env, "LOG_LEVEL", "INFO").upper(),
        scan_interval_sec=_num(env, "SCAN_INTERVAL_SEC", 20, int, w, 5, 3600),
        max_token_age_min=_num(env, "MAX_TOKEN_AGE_MIN", 120, int, w, 1, 10080),
        scanner_poll_always=_str(env, "SCANNER_POLL_ALWAYS", "true").lower() in ("1", "true", "yes", "on"),
        gt_poll_sec=_num(env, "GT_POLL_SEC", 20, int, w, 10, 3600),
        rc_poll_sec=_num(env, "RC_POLL_SEC", 6, int, w, 3, 3600),
        ws_idle_timeout_sec=_num(env, "WS_IDLE_TIMEOUT_SEC", 120, int, w, 15, 3600),
        fallback_after_sec=_num(env, "FALLBACK_AFTER_SEC", 20, int, w, 0, 3600),
        max_tracked=_num(env, "MAX_TRACKED", 40, int, w, 1, 500),
        min_track_txns=_num(env, "MIN_TRACK_TXNS", 5, int, w, 0, 100000),
        min_hold_sec=_num(env, "MIN_HOLD_SEC", 120, int, w, 0, 86400),
        snapshot_sec=_num(env, "SNAPSHOT_SEC", 15, int, w, 5, 3600),
        enrich_sec=_num(env, "ENRICH_SEC", 5, int, w, 2, 600),
        no_pair_ttl_sec=_num(env, "NO_PAIR_TTL_SEC", 300, int, w, 30, 86400),
        dead_after_min=_num(env, "DEAD_AFTER_MIN", 10, int, w, 1, 1440),
        hot_size=_num(env, "HOT_SIZE", 3, int, w, 0, 20),
        hot_refresh_sec=_num(env, "HOT_REFRESH_SEC", 90, int, w, 30, 3600),
        jup_refresh_sec=_num(env, "JUP_REFRESH_SEC", 60, int, w, 10, 3600),
        snapshot_retention_h=_num(env, "SNAPSHOT_RETENTION_H", 6, int, w, 1, 720),
        candle_retention_h=_num(env, "CANDLE_RETENTION_H", 48, int, w, 1, 2160),
        paper_start_balance=_num(env, "PAPER_START_BALANCE", 100.0, float, w, 1, 1e9),
        paper_trade_size=_num(env, "PAPER_TRADE_SIZE", 2.0, float, w, 0.01, 1e6),
        paper_buy_fee=_num(env, "PAPER_BUY_FEE", 0.50, float, w, 0, 1e6),
        paper_sell_fee=_num(env, "PAPER_SELL_FEE", 0.50, float, w, 0, 1e6),
        paper_slippage_pct=_num(env, "PAPER_SLIPPAGE_PCT", 3.0, float, w, 0, 50),
        paper_max_open=_num(env, "PAPER_MAX_OPEN", 5, int, w, 1, 50),
        paper_daily_target=_num(env, "PAPER_DAILY_TARGET", 50, int, w, 0, 10000),
        min_trade_score=_num(env, "MIN_TRADE_SCORE", 65, int, w, 50, 100),
        warnings=tuple(w),
    )
    if s.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        s = _replace(s, log_level="INFO", warnings=s.warnings + (f"LOG_LEVEL={s.log_level} unknown, using INFO",))
    for msg in s.warnings:
        log.warning(msg)
    return s


def _replace(s: Settings, **kw) -> Settings:
    from dataclasses import replace

    return replace(s, **kw)
