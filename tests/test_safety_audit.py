"""Phase 10 safety audit as tests: paper-only lock, no private-key / signing / sending code path, read-only RPC,
no real-looking secrets in tracked files, Termux-friendly dependencies."""
import re
import subprocess
from pathlib import Path

import pytest

import shadow
from shadow.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
SRC = sorted((ROOT / "shadow").glob("*.py")) + [ROOT / "main.py"] + sorted((ROOT / "docs").glob("*.js"))


def code(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_paper_only_is_hard_locked():
    assert shadow.PAPER_ONLY is True
    for v in ("false", "0", "no", "off", "FALSE"):
        s = load_settings(env={"PAPER_ONLY": v})
        assert s.paper_only is True and any("PAPER_ONLY can not be disabled" in w for w in s.warnings)
    with pytest.raises(Exception):
        load_settings(env={}).__class__(paper_only=False)      # not even settable in code (init=False)


def test_private_key_variables_are_ignored():
    s = load_settings(env={"WALLET_PRIVATE_KEY": "abc", "SEED_PHRASE": "x y z", "SOLANA_KEYPAIR": "[1,2]"})
    assert sum("never needs a private key" in w for w in s.warnings) == 3
    assert not any("private" in f.lower() or "seed" in f.lower() or "keypair" in f.lower()
                   for f in s.__dataclass_fields__)


FORBIDDEN = [r"sendTransaction", r"sendRawTransaction", r"signTransaction", r"sign_transaction", r"Keypair",
             r"from_secret_key", r"from_base58_string", r"\bsolders\b", r"\bsolana\.rpc\b", r"\bnacl\b", r"mnemonic\(",
             r"/swap/v1/swap\b", r"swap-instructions", r"/ultra/v1/execute", r"/order/execute"]


def test_no_signing_or_transaction_sending_code():
    hits = [(p.name, pat) for p in SRC for pat in FORBIDDEN if re.search(pat, code(p))]
    assert hits == []


def test_rpc_methods_are_read_only():
    methods = set()
    for p in SRC:
        methods |= set(re.findall(r'"method":\s*"(\w+)"', code(p)))
        methods |= set(re.findall(r'rpc\w*\([^)]*?"(get\w+|send\w+|simulate\w+|request\w+)"', code(p)))
    rpc = {m for m in methods if not m.startswith("subscribe")}           # PumpPortal websocket subscriptions
    assert rpc and all(m.startswith("get") for m in rpc), rpc


def test_no_real_looking_secrets_in_tracked_files():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not files:
        pytest.skip("not a git checkout")
    pats = [re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33,}\b"), re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
            re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")]
    fake = ("Fake", "secret", "SECRET")
    bad = []
    for f in files:
        p = ROOT / f
        if p.suffix in (".png", ".gz", ".db") or not p.is_file():
            continue
        for pat in pats:
            for m in pat.findall(p.read_text(encoding="utf-8", errors="ignore")):
                if not any(x in m for x in fake):
                    bad.append((f, m[:12]))
    assert bad == []
    assert ".env" not in files and not any(f.endswith((".db", ".log")) for f in files)


def test_env_example_has_no_values_for_secrets():
    env = dict(l.split("=", 1) for l in (ROOT / ".env.example").read_text().splitlines() if l and not l.startswith("#"))
    for k in ("TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN", "JUPITER_API_KEY", "RUGCHECK_API_KEY", "BIRDEYE_API_KEY"):
        assert env[k] == ""


def test_runtime_dependencies_are_pure_python_and_few():
    req = [l.split(">")[0].split("=")[0].strip().lower() for l in (ROOT / "requirements.txt").read_text().splitlines()
           if l.strip() and not l.startswith("#")]
    assert req == ["httpx", "websockets", "python-dotenv"]
    banned = ("pandas", "numpy", "pydantic", "cryptography", "orjson", "uvloop", "lxml", "solders", "solana")
    imports = set()
    for p in sorted((ROOT / "shadow").glob("*.py")):
        for line in code(p).splitlines():
            m = re.match(r"\s*(?:import|from)\s+([a-zA-Z_]\w*)", line)
            if m:
                imports.add(m.group(1))
    top_level = imports - {"shadow"}
    assert not (top_level & set(banned) - {"pandas"})          # pandas only inside the optional candles_df helper
    assert "import pandas" in code(ROOT / "shadow" / "collector.py").split("def candles_df", 1)[1]


def test_scripts_are_valid_bash_and_setup_never_compiles():
    for f in ("start.sh", "setup_termux.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / f)]).returncode == 0
    setup = code(ROOT / "setup_termux.sh")
    assert "pip install pandas" not in setup.replace("never `pip install pandas`", "")
    assert "BUILD_EXTENSION=no" in setup and "bash ./start.sh" in setup
