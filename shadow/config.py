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
SECRET_FIELDS = ("telegram_bot_token", "github_token", "jupiter_api_key", "birdeye_api_key", "rugcheck_api_key")
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


def _shadowed_secrets(path: Path) -> list[str]:
    """Secret keys whose exported value differs from a non-empty value in .env (e.g. after a token rotation).
    Only key names are returned, never values."""
    if not path.is_file():
        return []
    vals = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            vals[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    keys = [f.upper() for f in SECRET_FIELDS]
    return [k for k in keys if vals.get(k) and os.environ.get(k) and os.environ[k] != vals[k]]


def _str(env: dict, key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _bool(env: dict, key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    return default if raw == "" else raw in ("1", "true", "yes", "on")


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
    telegram_chat_id: str = "7922990181"                 # ONLY this chat may control the bot / gets alerts
    telegram_api_base: str = "https://api.telegram.org"  # change if api.telegram.org is blocked (mirror/proxy)
    tg_startup_message: bool = True       # "SHADOW MEME online (paper-only)" when the bot starts
    tg_alerts_dry_run: bool = False       # true = alerts are only logged (testing); command replies still sent
    tg_hold_alerts: bool = True           # HOLD updates for open watches (rate-limited)
    tg_sell_cooldown_min: float = 10.0    # max 1 SELL alert per coin+strategy per N minutes
    tg_hold_cooldown_min: float = 30.0    # max 1 HOLD alert per coin per N minutes
    tg_reject_digest_min: float = 60.0    # REJECTs are never sent one by one: 1 digest per N minutes (0 = off)
    tg_max_alerts_per_min: int = 20       # signal alerts above this per minute are summarised, not sent
    tg_queue_max: int = 300               # outgoing queue while Telegram is unreachable (oldest dropped)
    tg_poll_timeout_sec: int = 25         # getUpdates long-poll timeout
    tg_send_interval_sec: float = 1.1     # min gap between messages (Telegram: ~1 msg/s per chat)
    tg_menu_button: bool = True           # chat menu button opens the dashboard Mini App
    tg_log_only: bool = False             # testing: no Telegram network calls at all, sends only written to the log
    tg_notify: str = "trades"             # v1.1.1: trades = ONLY paper BUY + SELL (close) pushed; buy = BUY only; all = v1.0
    health_log_sec: int = 300             # HEALTH line (RSS, CPU, DB size, API calls/429s, loop lag) every N seconds

    # Dashboard (phase 9): Telegram Mini App on GitHub Pages (docs/). Optional GITHUB_TOKEN (fine-grained,
    # this repo only, Contents: read & write) lets the bot push the full snapshot to the dashboard-data branch.
    github_token: str = ""
    github_repo: str = "ahmadrahmani147370-hype/shadow-meme-bot"
    dashboard_url: str = "https://ahmadrahmani147370-hype.github.io/shadow-meme-bot/"
    dashboard_branch: str = "dashboard-data"
    dashboard_publisher: str = "auto"    # auto (GITHUB_TOKEN if set) | token | gh (GitHub CLI login, no token in .env) | off
    dashboard_push_min: float = 5.0       # snapshot every N minutes (local file always; GitHub only with a token)

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
    max_tracked: int = 200         # cap on tokens polled for snapshots (200 = 7 DexScreener calls / 15 s)
    min_track_txns: int = 5        # a token needs this many trades before it is tracked
    min_hold_sec: int = 120        # a tracked token can't be evicted by a busier one before this
    snapshot_sec: int = 15
    enrich_sec: int = 5
    no_pair_ttl_sec: int = 300     # give up on tokens without a DexScreener pair after this
    dead_after_min: int = 10       # stop tracking a token with no trades for this long
    hot_size: int = 3              # tokens that also get GeckoTerminal OHLCV
    hot_refresh_sec: int = 180     # 1 GT call/min: the other GT minute goes to universe backfill
    jup_refresh_sec: int = 60      # Jupiter price/liquidity fill-in for tracked tokens
    snapshot_retention_h: int = 3      # signals look back <= MAX_TOKEN_AGE_MIN (2 h)
    candle_retention_h: int = 4        # tokens are tracked <= 2 h, backfill reads 60 min (phase 7: 6 -> 4 h)
    market_retention_d: float = 0.5    # untracked token_market rows + drops (phase 7: 2 -> 0.5 d; tracking stops at 2 h)
    seen_retention_d: float = 1        # de-dup table; serial-creator check needs 24 h (phase 7: 2 -> 1 d)

    # Safety (phase 4), STRICT defaults. Thresholds are % of total supply unless noted.
    safety_min_score: int = 70           # below this sub-score = reject
    safety_ttl_sec: int = 900            # full re-check (RugCheck + RPC) after this
    safety_reject_ttl_sec: int = 1800    # re-check rejected tokens less often
    safety_quote_ttl_sec: int = 180      # a sellability quote is valid this long
    safety_rc_per_min: int = 30          # RugCheck reports per minute for safety (scanner polls separately)
    safety_quotes_per_min: int = 8       # Jupiter quotes per minute (shares the 30/min keyless bucket)
    safety_quote_min_vol5m: float = 500  # until phase 5: quote passing tokens with at least this 5m volume (USD)
    safety_top1_crit_pct: float = 30.0
    safety_top10_crit_pct: float = 60.0
    safety_top1_danger_pct: float = 15.0
    safety_top10_danger_pct: float = 40.0
    safety_insider_crit_pct: float = 25.0
    safety_insider_danger_pct: float = 10.0
    safety_dev_hold_crit_pct: float = 20.0
    safety_dev_hold_danger_pct: float = 5.0
    safety_dev_buy_warn_sol: float = 5.0
    safety_serial_creator: int = 3       # tokens by the same creator in 24 h = serial creator (danger)
    safety_serial_creator_crit: int = 10 # ... this many = critical
    safety_crit_liq_usd: float = 1500.0  # below = critical (new launches)
    safety_min_liq_usd: float = 3000.0   # below = danger
    safety_warn_liq_usd: float = 6000.0  # below = warn
    safety_min_liq_usd_large: float = 50000.0  # danger floor for the mcap/volume universes (coins >= $1M)
    safety_min_liq_mcap: float = 0.05    # liquidity / market cap below this on AMM pools = danger
    safety_max_impact_crit_pct: float = 15.0
    safety_max_impact_danger_pct: float = 5.0
    safety_max_sell_loss_pct: float = 25.0  # quote value of selling the paper size lost more than this = critical
    safety_require_rugcheck: bool = True    # no RugCheck report -> UNKNOWN (never tradable), not PASS

    # Universes (phase 5): MCAP >= $1M and TOP VOLUME coins, beside the new-launch tracking
    universes_enabled: bool = True
    universe_mcap_min: float = 1_000_000.0
    universe_mcap_min_liq: float = 25_000.0
    universe_mcap_max: int = 100          # tracked MCAP coins (most 24h volume first)
    universe_volume_top_n: int = 30       # top N by 24h volume + top N by 1h volume
    universe_volume_max: int = 50
    universe_volume_min_liq: float = 20_000.0
    universe_volume_min_mcap: float = 100_000.0
    universe_volume_min_vol_h24: float = 300_000.0   # a "top volume" coin needs real volume, not just a rank
    universe_volume_min_vol_h1: float = 25_000.0
    universe_refresh_sec: int = 30        # snapshot of tracked universe coins (DexScreener batches)
    universe_discover_sec: int = 300      # GeckoTerminal trending/top pools + DexScreener boosts
    universe_gt_pages: int = 1            # pages per GeckoTerminal list (20 pools each, 3 lists)
    universe_max_candidates: int = 300
    universe_allow_majors: bool = False   # JUP/RAY/BONK/WIF/PUMP... are not memes to trade unless allowed
    universe_exclude_mints: str = ""      # extra mints to ignore, comma separated

    # Signals (phase 5)
    signal_eval_sec: int = 15
    signal_cooldown_min: int = 10         # same coin + strategy re-signals at most this often
    signal_hold_every_min: int = 10       # HOLD updates for an open watch
    quote_near_score: int = 55            # a candidate this close to a signal gets a Jupiter quote

    # Judge agent (phase 5): rule-based reviewer of every BUY WATCH
    judge_min_confidence: float = 0.60
    judge_max_impact_pct: float = 3.0
    judge_max_sell_loss_pct: float = 10.0
    judge_min_liq_new: float = 8_000.0
    judge_min_liq_large: float = 75_000.0
    judge_max_correlated: int = 2
    judge_max_strategy_dd_pct: float = 20.0

    # Paper trading (phase 6). Money values are virtual USD.
    paper_start_balance: float = 100.0
    paper_trade_size: float = 2.0
    paper_buy_fee: float = 0.0     # Ahmed 13 Nov 2026: no fees by default (slippage from the quote stays)
    paper_sell_fee: float = 0.0
    paper_slippage_pct: float = 3.0
    paper_max_open: int = 5
    paper_daily_target: int = 50   # a target, never a quota: weak signals are never forced
    min_trade_score: int = 50      # global floor; v1.1: each strategy's own min_score decides (>= $1M coins score lower)
    paper_min_slippage_pct: float = 0.5   # entry/exit slippage = Jupiter quote price impact, at least this
    paper_exit_quote_max_age_sec: int = 600  # older quote at exit -> PAPER_SLIPPAGE_PCT is used instead
    paper_no_price_close_min: int = 30    # no price for this long -> close at the last known price
    paper_rug_liquidity_usd: float = 500.0  # pool liquidity below this = drained (rug): exit value capped by it
    paper_max_price_jump: float = 50.0    # one-update price jump above this x = bad data, ignored
    paper_equity_every_sec: int = 60

    # Self-learning (phase 6): guarded tuning of strategy params and judge thresholds
    learn_enabled: bool = True
    learn_every_min: int = 30
    learn_min_trades: int = 12            # v1.1: closed samples (watches + shadow) per strategy+universe (was 30)
    learn_min_test: int = 4               # of which at least this many in the out-of-sample (latest) part (was 10)
    learn_shrink_k: float = 20.0          # v1.1: gains are shrunk by n/(n+K): small samples need bigger gains
    learn_train_frac: float = 0.7         # walk-forward: oldest 70% choose, newest 30% must confirm
    learn_max_step_pct: float = 20.0      # one step changes one param by at most this %
    learn_min_improve_pct: float = 0.5    # % points per opportunity, on train AND test
    learn_cooldown_h: float = 3.0         # at most one change per strategy (or judge) per this many hours (was 6)
    learn_rollback_min: int = 6           # live samples of a new version before it is judged (was 10)
    shadow_track: bool = True             # v1.1: near-miss setups (score just under min) are tracked virtually
    shadow_score_margin: float = 15.0     # ... down to min_score - this

    # v1.1 trading universe / daily cap / fast refresh (Ahmed's rules, 13 Nov 2026)
    trade_min_mcap_usd: float = 1_000_000.0  # only coins with market cap >= this are traded (all universes)
    eval_min_mcap_usd: float = 750_000.0  # safety check + scoring start here (catch coins before they cross $1M)
    trade_min_liq_usd: float = 50_000.0   # pool liquidity floor for a trade
    trade_min_liq_mcap: float = 0.02      # liquidity / market cap floor (fake-supply $50M launches have ~0.2%)
    trade_min_age_min: float = 10.0       # no trades in a token's first minutes (launch chaos)
    coins_per_day: int = 1000             # unique coins admitted to evaluation per PKT day (0 = unlimited)
    coins_per_day_min: int = 500          # v1.1.1: guaranteed minimum (pace controller lowers the EVAL floor only)
    eval_pace_floors: str = "750000,250000,100000,50000,25000"   # evaluation floors tried when behind pace
    pace_fill_per_min: int = 10           # max coins/min admitted through a lowered floor
    fast_refresh_sec: float = 1.0         # price refresh for open positions / watches / near-trigger coins (0 = off)
    fast_max_mints: int = 30              # one DexScreener batch call per tick (30 mints max per call)
    fast_near_sec: int = 120              # a coin with a strategy setup stays in the fast set this long
    fast_jup_sec: float = 5.0             # v1.1: Jupiter price v3 for open positions/watches every N s (0 = off)
    learn_rollback_margin_pct: float = 0.0  # new version worse than the old one on the same samples -> rollback
    judge_tuning_enabled: bool = True
    universe_backfill_sec: int = 60       # one GeckoTerminal OHLCV backfill per minute for universe members
    universe_backfill_minutes: int = 60

    # Persistence hardening (phase 7)
    timezone: str = "Asia/Karachi"        # day boundaries (50-trade counter, daily report, backups) = PKT midnight
    db_busy_timeout_ms: int = 10000
    db_max_mb: float = 150.0              # hard cap: above this after a normal prune -> emergency prune
    maintenance_sec: int = 300            # prune / checkpoint / optimize / backup check
    optimize_every_h: float = 6.0         # PRAGMA optimize
    vacuum_every_h: float = 24.0          # VACUUM at most this often ...
    vacuum_min_free_pct: float = 10.0     # ... and only when this % of the file is free pages
    backup_dir: str = "data/backups"
    backup_keep: int = 7                  # daily gzip backups kept (rotation)
    signal_retention_d: float = 30        # BUY_WATCH / SELL / HOLD signals (learning + /history)
    signal_reject_retention_d: float = 2  # REJECT signals
    creator_blacklist_retention_d: float = 14  # local cache; RugCheck's creator history still flags them later
    seen_raw_retention_h: float = 1       # raw scanner JSON of seen_tokens (debug only, never read)
    backup_mode: str = "state"            # state = durable tables only (small) | full = whole database
    signal_detail_retention_h: float = 24 # REJECT feature/detector JSON is dropped after this
    paper_equity_retention_d: float = 30

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


# Test hook only (tests/conftest.py): defaults layered under an explicit `env` dict. Empty in the bot.
TEST_DEFAULTS: dict[str, str] = {}


def load_settings(env_file: str | Path | None = None, env: dict | None = None) -> Settings:
    """Build Settings. `env` (a dict) is used by tests instead of os.environ."""
    w: list[str] = []
    if env is not None and TEST_DEFAULTS:
        env = {**TEST_DEFAULTS, **env}
    if env is None:
        path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
        shadowed = _shadowed_secrets(path)
        _load_dotenv(path)
        env = dict(os.environ)
        for k in shadowed:
            w.append(f"{k} from the environment is used, NOT the different value in .env (environment wins). "
                     f"If you changed it in .env, run `unset {k}` or open a new shell.")

    raw_paper = _str(env, "PAPER_ONLY", "true").lower()
    if raw_paper not in ("1", "true", "yes", "on"):
        w.append("PAPER_ONLY can not be disabled. Live trading does not exist in this bot; staying paper-only.")
    for key in env:
        if any(h in key.upper() for h in FORBIDDEN_ENV_HINTS) and env.get(key):
            w.append(f"{key} is ignored: SHADOW MEME never needs a private key, seed or wallet secret. Remove it from .env.")

    s = Settings(
        telegram_bot_token=_str(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str(env, "TELEGRAM_CHAT_ID", Settings.telegram_chat_id),
        telegram_api_base=_url(env, "TELEGRAM_API_BASE", Settings.telegram_api_base, w),
        tg_startup_message=_bool(env, "TELEGRAM_STARTUP_MESSAGE", True),
        tg_alerts_dry_run=_bool(env, "TELEGRAM_ALERTS_DRYRUN", False),
        tg_hold_alerts=_bool(env, "TELEGRAM_HOLD_ALERTS", True),
        tg_sell_cooldown_min=_num(env, "TELEGRAM_SELL_COOLDOWN_MIN", 10.0, float, w, 0, 1440),
        tg_hold_cooldown_min=_num(env, "TELEGRAM_HOLD_COOLDOWN_MIN", 30.0, float, w, 0, 1440),
        tg_reject_digest_min=_num(env, "TELEGRAM_REJECT_DIGEST_MIN", 60.0, float, w, 0, 1440),
        tg_max_alerts_per_min=_num(env, "TELEGRAM_MAX_ALERTS_PER_MIN", 20, int, w, 1, 600),
        tg_queue_max=_num(env, "TELEGRAM_QUEUE_MAX", 300, int, w, 10, 10000),
        tg_poll_timeout_sec=_num(env, "TELEGRAM_POLL_TIMEOUT_SEC", 25, int, w, 0, 50),
        tg_send_interval_sec=_num(env, "TELEGRAM_SEND_INTERVAL_SEC", 1.1, float, w, 0, 60),
        tg_menu_button=_bool(env, "TELEGRAM_MENU_BUTTON", True),
        tg_log_only=_bool(env, "TELEGRAM_LOG_ONLY", False),
        tg_notify=(lambda v: v if v in ("buy", "trades", "all") else "trades")(_str(env, "TELEGRAM_NOTIFY", "trades").lower()),
        health_log_sec=_num(env, "HEALTH_LOG_SEC", 300, int, w, 10, 86400),
        github_token=_str(env, "GITHUB_TOKEN"),
        github_repo=_str(env, "GITHUB_REPO", Settings.github_repo),
        dashboard_url=_url(env, "DASHBOARD_URL", Settings.dashboard_url.rstrip("/"), w) + "/",
        dashboard_branch=_str(env, "DASHBOARD_BRANCH", Settings.dashboard_branch),
        dashboard_publisher=_str(env, "DASHBOARD_PUBLISHER", "auto").lower(),
        dashboard_push_min=_num(env, "DASHBOARD_PUSH_MIN", 5.0, float, w, 1, 1440),
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
        max_tracked=_num(env, "MAX_TRACKED", 200, int, w, 1, 600),
        min_track_txns=_num(env, "MIN_TRACK_TXNS", 5, int, w, 0, 100000),
        min_hold_sec=_num(env, "MIN_HOLD_SEC", 120, int, w, 0, 86400),
        snapshot_sec=_num(env, "SNAPSHOT_SEC", 15, int, w, 5, 3600),
        enrich_sec=_num(env, "ENRICH_SEC", 5, int, w, 2, 600),
        no_pair_ttl_sec=_num(env, "NO_PAIR_TTL_SEC", 300, int, w, 30, 86400),
        dead_after_min=_num(env, "DEAD_AFTER_MIN", 10, int, w, 1, 1440),
        hot_size=_num(env, "HOT_SIZE", 3, int, w, 0, 20),
        hot_refresh_sec=_num(env, "HOT_REFRESH_SEC", 180, int, w, 30, 3600),
        jup_refresh_sec=_num(env, "JUP_REFRESH_SEC", 60, int, w, 10, 3600),
        snapshot_retention_h=_num(env, "SNAPSHOT_RETENTION_H", 3, int, w, 1, 720),
        candle_retention_h=_num(env, "CANDLE_RETENTION_H", 4, int, w, 1, 2160),
        market_retention_d=_num(env, "MARKET_RETENTION_D", 0.5, float, w, 0.1, 90),
        seen_retention_d=_num(env, "SEEN_RETENTION_D", 1.0, float, w, 1, 90),
        safety_min_score=_num(env, "SAFETY_MIN_SCORE", 70, int, w, 0, 100),
        safety_ttl_sec=_num(env, "SAFETY_TTL_SEC", 900, int, w, 60, 86400),
        safety_reject_ttl_sec=_num(env, "SAFETY_REJECT_TTL_SEC", 1800, int, w, 60, 86400),
        safety_quote_ttl_sec=_num(env, "SAFETY_QUOTE_TTL_SEC", 180, int, w, 30, 3600),
        safety_rc_per_min=_num(env, "SAFETY_RC_PER_MIN", 30, int, w, 1, 50),
        safety_quotes_per_min=_num(env, "SAFETY_QUOTES_PER_MIN", 8, int, w, 0, 20),
        safety_quote_min_vol5m=_num(env, "SAFETY_QUOTE_MIN_VOL5M", 500.0, float, w, 0, 1e9),
        safety_top1_crit_pct=_num(env, "SAFETY_TOP1_CRIT_PCT", 30.0, float, w, 1, 100),
        safety_top10_crit_pct=_num(env, "SAFETY_TOP10_CRIT_PCT", 60.0, float, w, 1, 100),
        safety_top1_danger_pct=_num(env, "SAFETY_TOP1_DANGER_PCT", 15.0, float, w, 1, 100),
        safety_top10_danger_pct=_num(env, "SAFETY_TOP10_DANGER_PCT", 40.0, float, w, 1, 100),
        safety_insider_crit_pct=_num(env, "SAFETY_INSIDER_CRIT_PCT", 25.0, float, w, 1, 100),
        safety_insider_danger_pct=_num(env, "SAFETY_INSIDER_DANGER_PCT", 10.0, float, w, 1, 100),
        safety_dev_hold_crit_pct=_num(env, "SAFETY_DEV_HOLD_CRIT_PCT", 20.0, float, w, 0, 100),
        safety_dev_hold_danger_pct=_num(env, "SAFETY_DEV_HOLD_DANGER_PCT", 5.0, float, w, 0, 100),
        safety_dev_buy_warn_sol=_num(env, "SAFETY_DEV_BUY_WARN_SOL", 5.0, float, w, 0, 1e6),
        safety_serial_creator=_num(env, "SAFETY_SERIAL_CREATOR", 3, int, w, 2, 10000),
        safety_serial_creator_crit=_num(env, "SAFETY_SERIAL_CREATOR_CRIT", 10, int, w, 2, 10000),
        safety_crit_liq_usd=_num(env, "SAFETY_CRIT_LIQ_USD", 1500.0, float, w, 0, 1e9),
        safety_min_liq_usd=_num(env, "SAFETY_MIN_LIQ_USD", 3000.0, float, w, 0, 1e9),
        safety_warn_liq_usd=_num(env, "SAFETY_WARN_LIQ_USD", 6000.0, float, w, 0, 1e9),
        safety_min_liq_usd_large=_num(env, "SAFETY_MIN_LIQ_USD_LARGE", 50000.0, float, w, 0, 1e10),
        safety_min_liq_mcap=_num(env, "SAFETY_MIN_LIQ_MCAP", 0.05, float, w, 0, 10),
        safety_max_impact_crit_pct=_num(env, "SAFETY_MAX_IMPACT_CRIT_PCT", 15.0, float, w, 1, 100),
        safety_max_impact_danger_pct=_num(env, "SAFETY_MAX_IMPACT_DANGER_PCT", 5.0, float, w, 0.1, 100),
        safety_max_sell_loss_pct=_num(env, "SAFETY_MAX_SELL_LOSS_PCT", 25.0, float, w, 1, 100),
        safety_require_rugcheck=_str(env, "SAFETY_REQUIRE_RUGCHECK", "true").lower() in ("1", "true", "yes", "on"),
        universes_enabled=_str(env, "UNIVERSES_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        universe_mcap_min=_num(env, "UNIVERSE_MCAP_MIN", 1_000_000.0, float, w, 10_000, 1e12),
        universe_mcap_min_liq=_num(env, "UNIVERSE_MCAP_MIN_LIQ", 25_000.0, float, w, 0, 1e10),
        universe_mcap_max=_num(env, "UNIVERSE_MCAP_MAX", 100, int, w, 0, 300),
        universe_volume_top_n=_num(env, "UNIVERSE_VOLUME_TOP_N", 30, int, w, 1, 200),
        universe_volume_max=_num(env, "UNIVERSE_VOLUME_MAX", 50, int, w, 0, 300),
        universe_volume_min_liq=_num(env, "UNIVERSE_VOLUME_MIN_LIQ", 20_000.0, float, w, 0, 1e10),
        universe_volume_min_mcap=_num(env, "UNIVERSE_VOLUME_MIN_MCAP", 100_000.0, float, w, 0, 1e12),
        universe_volume_min_vol_h24=_num(env, "UNIVERSE_VOLUME_MIN_VOL_H24", 300_000.0, float, w, 0, 1e12),
        universe_volume_min_vol_h1=_num(env, "UNIVERSE_VOLUME_MIN_VOL_H1", 25_000.0, float, w, 0, 1e12),
        universe_refresh_sec=_num(env, "UNIVERSE_REFRESH_SEC", 30, int, w, 10, 3600),
        universe_discover_sec=_num(env, "UNIVERSE_DISCOVER_SEC", 300, int, w, 60, 86400),
        universe_gt_pages=_num(env, "UNIVERSE_GT_PAGES", 1, int, w, 1, 10),
        universe_max_candidates=_num(env, "UNIVERSE_MAX_CANDIDATES", 300, int, w, 10, 3000),
        universe_allow_majors=_str(env, "UNIVERSE_ALLOW_MAJORS", "false").lower() in ("1", "true", "yes", "on"),
        universe_exclude_mints=_str(env, "UNIVERSE_EXCLUDE_MINTS", ""),
        signal_eval_sec=_num(env, "SIGNAL_EVAL_SEC", 15, int, w, 5, 600),
        signal_cooldown_min=_num(env, "SIGNAL_COOLDOWN_MIN", 10, int, w, 1, 1440),
        signal_hold_every_min=_num(env, "SIGNAL_HOLD_EVERY_MIN", 10, int, w, 1, 1440),
        quote_near_score=_num(env, "QUOTE_NEAR_SCORE", 55, int, w, 0, 100),
        judge_min_confidence=_num(env, "JUDGE_MIN_CONFIDENCE", 0.60, float, w, 0, 1),
        judge_max_impact_pct=_num(env, "JUDGE_MAX_IMPACT_PCT", 3.0, float, w, 0.1, 100),
        judge_max_sell_loss_pct=_num(env, "JUDGE_MAX_SELL_LOSS_PCT", 10.0, float, w, 0.1, 100),
        judge_min_liq_new=_num(env, "JUDGE_MIN_LIQ_NEW", 8_000.0, float, w, 0, 1e10),
        judge_min_liq_large=_num(env, "JUDGE_MIN_LIQ_LARGE", 75_000.0, float, w, 0, 1e10),
        judge_max_correlated=_num(env, "JUDGE_MAX_CORRELATED", 2, int, w, 0, 50),
        judge_max_strategy_dd_pct=_num(env, "JUDGE_MAX_STRATEGY_DD_PCT", 20.0, float, w, 1, 100),
        paper_start_balance=_num(env, "PAPER_START_BALANCE", 100.0, float, w, 1, 1e9),
        paper_trade_size=_num(env, "PAPER_TRADE_SIZE", 2.0, float, w, 0.01, 1e6),
        paper_buy_fee=_num(env, "PAPER_BUY_FEE", 0.0, float, w, 0, 1e6),
        paper_sell_fee=_num(env, "PAPER_SELL_FEE", 0.0, float, w, 0, 1e6),
        paper_slippage_pct=_num(env, "PAPER_SLIPPAGE_PCT", 3.0, float, w, 0, 50),
        paper_max_open=_num(env, "PAPER_MAX_OPEN", 5, int, w, 1, 50),
        paper_daily_target=_num(env, "PAPER_DAILY_TARGET", 50, int, w, 0, 10000),
        min_trade_score=_num(env, "MIN_TRADE_SCORE", 50, int, w, 30, 100),
        paper_min_slippage_pct=_num(env, "PAPER_MIN_SLIPPAGE_PCT", 0.5, float, w, 0, 50),
        paper_exit_quote_max_age_sec=_num(env, "PAPER_EXIT_QUOTE_MAX_AGE_SEC", 600, int, w, 10, 86400),
        paper_no_price_close_min=_num(env, "PAPER_NO_PRICE_CLOSE_MIN", 30, int, w, 1, 10080),
        paper_rug_liquidity_usd=_num(env, "PAPER_RUG_LIQUIDITY_USD", 500.0, float, w, 0, 1e9),
        paper_max_price_jump=_num(env, "PAPER_MAX_PRICE_JUMP", 50.0, float, w, 2, 1e9),
        paper_equity_every_sec=_num(env, "PAPER_EQUITY_EVERY_SEC", 60, int, w, 10, 86400),
        learn_enabled=_str(env, "LEARN_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        learn_every_min=_num(env, "LEARN_EVERY_MIN", 30, int, w, 1, 10080),
        learn_min_trades=_num(env, "LEARN_MIN_TRADES", 12, int, w, 5, 100000),
        learn_min_test=_num(env, "LEARN_MIN_TEST", 4, int, w, 2, 100000),
        learn_shrink_k=_num(env, "LEARN_SHRINK_K", 20.0, float, w, 0, 10000),
        learn_train_frac=_num(env, "LEARN_TRAIN_FRAC", 0.7, float, w, 0.3, 0.9),
        learn_max_step_pct=_num(env, "LEARN_MAX_STEP_PCT", 20.0, float, w, 1, 50),
        learn_min_improve_pct=_num(env, "LEARN_MIN_IMPROVE_PCT", 0.5, float, w, 0, 100),
        learn_cooldown_h=_num(env, "LEARN_COOLDOWN_H", 3.0, float, w, 0, 720),
        learn_rollback_min=_num(env, "LEARN_ROLLBACK_MIN", 6, int, w, 3, 100000),
        shadow_track=_bool(env, "SHADOW_TRACK", True),
        shadow_score_margin=_num(env, "SHADOW_SCORE_MARGIN", 15.0, float, w, 0, 50),
        trade_min_mcap_usd=_num(env, "TRADE_MIN_MCAP_USD", 1_000_000.0, float, w, 0, 1e13),
        eval_min_mcap_usd=_num(env, "EVAL_MIN_MCAP_USD", 750_000.0, float, w, 0, 1e13),
        trade_min_liq_usd=_num(env, "TRADE_MIN_LIQ_USD", 50_000.0, float, w, 0, 1e12),
        trade_min_liq_mcap=_num(env, "TRADE_MIN_LIQ_MCAP", 0.02, float, w, 0, 10),
        trade_min_age_min=_num(env, "TRADE_MIN_AGE_MIN", 10.0, float, w, 0, 100000),
        coins_per_day=_num(env, "COINS_PER_DAY", 1000, int, w, 0, 10_000_000),
        coins_per_day_min=_num(env, "COINS_PER_DAY_MIN", 500, int, w, 0, 10_000_000),
        eval_pace_floors=_str(env, "EVAL_PACE_FLOORS", "750000,250000,100000,50000,25000"),
        pace_fill_per_min=_num(env, "PACE_FILL_PER_MIN", 10, int, w, 1, 1000),
        fast_refresh_sec=_num(env, "FAST_REFRESH_SEC", 1.0, float, w, 0, 600),
        fast_max_mints=_num(env, "FAST_MAX_MINTS", 30, int, w, 1, 30),
        fast_near_sec=_num(env, "FAST_NEAR_SEC", 120, int, w, 0, 86400),
        fast_jup_sec=_num(env, "FAST_JUP_SEC", 5.0, float, w, 0, 600),
        learn_rollback_margin_pct=_num(env, "LEARN_ROLLBACK_MARGIN_PCT", 0.0, float, w, 0, 100),
        judge_tuning_enabled=_str(env, "JUDGE_TUNING_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        universe_backfill_sec=_num(env, "UNIVERSE_BACKFILL_SEC", 60, int, w, 15, 86400),
        universe_backfill_minutes=_num(env, "UNIVERSE_BACKFILL_MINUTES", 60, int, w, 10, 300),
        timezone=_str(env, "TIMEZONE", Settings.timezone) or Settings.timezone,
        db_busy_timeout_ms=_num(env, "DB_BUSY_TIMEOUT_MS", 10000, int, w, 100, 600000),
        db_max_mb=_num(env, "DB_MAX_MB", 150.0, float, w, 10, 100000),
        maintenance_sec=_num(env, "MAINTENANCE_SEC", 300, int, w, 10, 86400),
        optimize_every_h=_num(env, "OPTIMIZE_EVERY_H", 6.0, float, w, 0.1, 720),
        vacuum_every_h=_num(env, "VACUUM_EVERY_H", 24.0, float, w, 0.5, 720),
        vacuum_min_free_pct=_num(env, "VACUUM_MIN_FREE_PCT", 10.0, float, w, 0, 100),
        backup_dir=_str(env, "BACKUP_DIR", Settings.backup_dir) or Settings.backup_dir,
        backup_keep=_num(env, "BACKUP_KEEP", 7, int, w, 1, 365),
        signal_retention_d=_num(env, "SIGNAL_RETENTION_D", 30.0, float, w, 1, 3650),
        signal_reject_retention_d=_num(env, "SIGNAL_REJECT_RETENTION_D", 2.0, float, w, 0.1, 365),
        creator_blacklist_retention_d=_num(env, "CREATOR_BLACKLIST_RETENTION_D", 14.0, float, w, 1, 3650),
        seen_raw_retention_h=_num(env, "SEEN_RAW_RETENTION_H", 1.0, float, w, 0, 720),
        backup_mode=(_str(env, "BACKUP_MODE", "state").lower() if _str(env, "BACKUP_MODE", "state").lower() in ("state", "full") else "state"),
        signal_detail_retention_h=_num(env, "SIGNAL_DETAIL_RETENTION_H", 24.0, float, w, 1, 8760),
        paper_equity_retention_d=_num(env, "PAPER_EQUITY_RETENTION_D", 30.0, float, w, 1, 3650),
        warnings=tuple(w),
    )
    if s.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        s = _replace(s, log_level="INFO", warnings=s.warnings + (f"LOG_LEVEL={s.log_level} unknown, using INFO",))
    from .timeutil import set_timezone
    tz_warn = set_timezone(s.timezone)
    if tz_warn:
        s = _replace(s, warnings=s.warnings + (tz_warn,))
    for msg in s.warnings:
        log.warning(msg)
    return s


def _replace(s: Settings, **kw) -> Settings:
    from dataclasses import replace

    return replace(s, **kw)
