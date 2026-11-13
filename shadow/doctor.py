"""SHADOW MEME self-check.

    python -m shadow.doctor            # full check incl. network
    python -m shadow.doctor --offline  # skip network
    python main.py --debug doctor

Exit code 0 = no FAIL (warnings allowed), 1 = at least one FAIL.
Never prints secrets.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import PAPER_ONLY, __version__
from .config import Settings, load_settings
from .sources import ENDPOINTS, Endpoint

OK, WARN, FAIL = "OK", "WARN", "FAIL"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}

REQUIRED_PACKAGES = ("httpx", "websockets", "dotenv", "pandas", "numpy")
UA = "ShadowMeme/0.1 (+https://github.com/ahmadrahmani147370-hype/shadow-meme-bot)"


@dataclass
class Check:
    group: str
    name: str
    status: str
    detail: str = ""
    ms: int | None = None


# ---------------------------------------------------------------- local checks
def check_python(min_version=(3, 10)) -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) >= min_version
    return Check("system", "python", OK if ok else FAIL,
                 f"{platform.python_version()} ({'>=' if ok else '<'} {min_version[0]}.{min_version[1]} needed)")


def check_packages(names=REQUIRED_PACKAGES) -> list[Check]:
    out = []
    for n in names:
        try:
            m = importlib.import_module(n)
            out.append(Check("packages", n, OK, getattr(m, "__version__", "installed")))
        except Exception as e:  # ImportError, or a broken native build
            hint = "pkg install python-numpy" if n == "numpy" else (
                "pkg install tur-repo && pkg install python-pandas" if n == "pandas" else f"pip install {'python-dotenv' if n == 'dotenv' else n}")
            out.append(Check("packages", n, FAIL, f"missing ({type(e).__name__}) -> {hint}"))
    return out


def check_paper_only(s: Settings) -> Check:
    ok = PAPER_ONLY is True and s.paper_only is True
    return Check("safety", "paper_only", OK if ok else FAIL,
                 "paper trading only; no live trading code, no wallet, no private key" if ok else "PAPER_ONLY is not True!")


def check_config(s: Settings) -> list[Check]:
    out = [Check("config", "settings", WARN if s.warnings else OK,
                 "; ".join(s.warnings) if s.warnings else "loaded (.env + defaults)")]
    out.append(Check("config", "telegram_token", OK if s.telegram_bot_token else WARN,
                     "set" if s.telegram_bot_token else "empty - REQUIRED: the bot runs only with Telegram (nano .env)"))
    out.append(Check("config", "telegram_chat_id", OK if s.telegram_chat_id else WARN,
                     "set" if s.telegram_chat_id else "empty - REQUIRED (your own chat id)"))
    return out


def check_write_access(s: Settings) -> list[Check]:
    out = []
    for label, rel in (("data_dir", str(Path(s.db_path).parent)), ("log_dir", s.log_dir)):
        d = s.path(rel)
        try:
            d.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=d, prefix=".doctor-", delete=True) as f:
                f.write(b"ok")
                f.flush()
            out.append(Check("storage", label, OK, f"{d} writable"))
        except OSError as e:
            out.append(Check("storage", label, FAIL, f"{d}: {e}"))
    return out


def check_disk(s: Settings, min_mb: int = 200) -> Check:
    try:
        free = shutil.disk_usage(s.path(".")).free // (1024 * 1024)
        return Check("storage", "disk_free", OK if free >= min_mb else WARN, f"{free} MB free")
    except OSError as e:
        return Check("storage", "disk_free", WARN, str(e))


def check_sqlite(s: Settings) -> Check:
    """Full create -> write -> read -> update -> reopen -> delete cycle on a scratch DB next to the real one."""
    d = s.path(str(Path(s.db_path).parent))
    t0 = time.perf_counter()
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / f".doctor-{os.getpid()}.db"
        con = sqlite3.connect(p)
        mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, mint TEXT UNIQUE, price REAL)")
        con.execute("INSERT INTO t (mint, price) VALUES (?, ?)", ("TestMint111", 0.000123))
        con.commit()
        con.execute("UPDATE t SET price = price * 2 WHERE mint = ?", ("TestMint111",))
        con.commit()
        con.close()
        con = sqlite3.connect(p)  # reopen: data must survive (like open positions after a restart)
        row = con.execute("SELECT mint, price FROM t").fetchone()
        con.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(p) + suffix).unlink(missing_ok=True)
        if row != ("TestMint111", 0.000246):
            return Check("storage", "sqlite", FAIL, f"read back {row!r}")
        return Check("storage", "sqlite", OK, f"sqlite {sqlite3.sqlite_version}, journal={mode}, create/write/read/update/reopen ok",
                     int((time.perf_counter() - t0) * 1000))
    except Exception as e:
        return Check("storage", "sqlite", FAIL, f"{type(e).__name__}: {e}")


def check_database(s: Settings) -> Check:
    """Open (or create) the real DB and apply schema migrations."""
    try:
        from .db import SCHEMA_VERSION, Database

        d = Database(s.path(s.db_path))
        v, n = d.version, d.count_seen().get("total", 0)
        d.close()
        return Check("storage", "database", OK if v == SCHEMA_VERSION else FAIL,
                     f"{s.db_path} schema v{v}/{SCHEMA_VERSION}, {n} mints in seen_tokens")
    except Exception as e:
        return Check("storage", "database", FAIL, f"{type(e).__name__}: {e}")


def check_android() -> list[Check]:
    prefix = os.environ.get("PREFIX", "")
    if "com.termux" not in prefix:
        return [Check("android", "termux", OK, f"not Termux ({platform.system()}) - Android checks skipped")]
    out = [Check("android", "termux", OK, prefix)]
    for tool, why in (("termux-wake-lock", "keeps CPU awake (pkg install termux-api not needed for this one)"),
                      ("termux-battery-status", "optional, needs Termux:API app + pkg install termux-api")):
        out.append(Check("android", tool, OK if shutil.which(tool) else WARN, "found" if shutil.which(tool) else f"missing: {why}"))
    boot = Path.home() / ".termux" / "boot"
    out.append(Check("android", "termux_boot", OK if boot.is_dir() else WARN,
                     "~/.termux/boot exists" if boot.is_dir() else "Termux:Boot not set up (optional auto-start after reboot)"))
    return out


# ---------------------------------------------------------------- network checks
def _endpoint_for_settings(e: Endpoint, s: Settings) -> Endpoint:
    if e.name == "solana_rpc":
        return replace(e, url=s.solana_rpc_url)
    if e.name == "solana_rpc_fallback":
        return replace(e, url=s.solana_rpc_fallback)
    if e.name == "pumpportal_ws":
        return replace(e, url=s.pumpportal_ws)
    if e.name == "telegram_api" and s.telegram_api_base:
        return replace(e, url=s.telegram_api_base.rstrip("/") + "/")
    if e.name.startswith("jupiter") and s.jupiter_api_key:
        return replace(e, headers={**e.headers, "x-api-key": s.jupiter_api_key})
    if e.name.startswith("rugcheck") and s.rugcheck_api_key:
        return replace(e, headers={**e.headers, "X-API-KEY": s.rugcheck_api_key})
    if e.name == "birdeye" and s.birdeye_api_key:
        return replace(e, headers={**e.headers, "X-API-KEY": s.birdeye_api_key}, required=False)
    return e


TELEGRAM_HINT = ("Telegram can't be reached from this network (common on some Pakistani mobile networks). "
                 "Try: 1) another network (Wi-Fi / other SIM), 2) the free 1.1.1.1 (Cloudflare WARP) app, "
                 "3) advanced: TELEGRAM_API_BASE=<your https Bot API mirror> in .env. The bot keeps scanning + paper trading while Telegram is unreachable.")


def _classify(e: Endpoint, code: int | None, body: str, err: str = "") -> tuple[str, str]:
    bad = FAIL if e.required else WARN
    if err:
        if e.name == "telegram_api" and any(k in err for k in ("ConnectTimeout", "ConnectError", "ReadTimeout")):
            return bad, f"{err.split(':')[0]} -> {TELEGRAM_HINT}"
        return bad, err
    if code == 429:
        return WARN, "429 rate limited (reachable, slow down)"
    if code is None or code >= 400:
        snippet = "Cloudflare challenge" if "Just a moment" in body else body[:80].replace("\n", " ")
        if e.name == "rugcheck_summary" and code == 400 and "unable to generate report" in body:
            # RugCheck answered with its own JSON error: the API is up, it just has no report for the probe
            # mint right now (seen for BONK/JUP/WIF on 13 Nov 2026 while new pump.fun mints worked).
            return WARN, "HTTP 400: API reachable, no cached report for the probe mint"
        if e.key_needed == "yes" and code in (401, 403):
            return WARN, f"HTTP {code}: needs API key (not used by default)"
        return bad, f"HTTP {code}: {snippet}"
    if e.expect and e.expect not in body:
        return bad, f"HTTP {code} but unexpected body: {body[:80]!r}"
    return OK, f"HTTP {code}"


async def _probe_http(client, e: Endpoint) -> Check:
    t0 = time.perf_counter()
    try:
        if e.kind == "rpc":
            payload = {"jsonrpc": "2.0", "id": 1, "method": e.rpc_method, "params": list(e.rpc_params)}
            r = await client.post(e.url, json=payload, headers=e.headers)
        else:
            r = await client.get(e.url, headers=e.headers)
        status, detail = _classify(e, r.status_code, r.text)
    except Exception as ex:
        status, detail = _classify(e, None, "", f"{type(ex).__name__}: {ex}"[:120])
    return Check("network", e.name, status, detail, int((time.perf_counter() - t0) * 1000))


async def _probe_ws(e: Endpoint, timeout: float) -> Check:
    t0 = time.perf_counter()
    try:
        import websockets

        async with websockets.connect(e.url, open_timeout=timeout, close_timeout=3, user_agent_header=UA) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        status, detail = _classify(e, 200, str(msg))
        if status == OK:
            detail = "connected + subscribeNewToken acknowledged"
    except Exception as ex:
        status, detail = _classify(e, None, "", f"{type(ex).__name__}: {ex}"[:120])
    return Check("network", e.name, status, detail, int((time.perf_counter() - t0) * 1000))


async def check_network(s: Settings, endpoints=ENDPOINTS, transport=None) -> list[Check]:
    import httpx

    eps = [_endpoint_for_settings(e, s) for e in endpoints]
    async with httpx.AsyncClient(timeout=s.http_timeout_sec, follow_redirects=True, transport=transport,
                                 headers={"User-Agent": UA, "Accept": "application/json"}) as client:
        tasks = [(_probe_ws(e, s.http_timeout_sec) if e.kind == "ws" else _probe_http(client, e)) for e in eps]
        return list(await asyncio.gather(*tasks))


# ---------------------------------------------------------------- runner
def run_checks(s: Settings | None = None, offline: bool = False) -> list[Check]:
    s = s or load_settings()
    checks = [check_python(), *check_packages(), check_paper_only(s), *check_config(s),
              *check_write_access(s), check_disk(s), check_sqlite(s), check_database(s), *check_android()]
    if offline:
        checks.append(Check("network", "skipped", WARN, "--offline"))
    else:
        try:
            checks += asyncio.run(check_network(s))
        except ImportError as e:
            checks.append(Check("network", "httpx", FAIL, f"cannot run network checks: {e}"))
    return checks


def format_report(checks: list[Check]) -> str:
    lines = [f"SHADOW MEME doctor v{__version__}  (paper-only)"]
    group = None
    for c in checks:
        if c.group != group:
            group = c.group
            lines.append(f"\n[{group}]")
        ms = f" {c.ms}ms" if c.ms is not None else ""
        lines.append(f" {ICON[c.status]} {c.name:<24}{ms:>8}  {c.detail}")
    n = {k: sum(c.status == k for c in checks) for k in (OK, WARN, FAIL)}
    lines.append(f"\nResult: {n[OK]} OK, {n[WARN]} WARN, {n[FAIL]} FAIL -> {'READY' if not n[FAIL] else 'FIX THE FAIL LINES'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m shadow.doctor", description="SHADOW MEME self-check")
    ap.add_argument("--offline", action="store_true", help="skip network checks")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    checks = run_checks(offline=a.offline)
    if a.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2, ensure_ascii=False))
    else:
        print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
