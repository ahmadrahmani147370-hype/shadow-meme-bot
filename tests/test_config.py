from pathlib import Path

import pytest

from shadow import PAPER_ONLY
from shadow.config import PROJECT_ROOT, Settings, load_settings


def test_defaults_match_spec():
    s = load_settings(env={})
    assert s.paper_only is True and PAPER_ONLY is True
    assert s.paper_start_balance == 100.0
    assert s.paper_trade_size == 2.0
    assert s.paper_buy_fee == 0.0 and s.paper_sell_fee == 0.0  # no fees by default (Ahmed, phase 7)
    s2 = load_settings(env={"PAPER_BUY_FEE": "0.5", "PAPER_SELL_FEE": "0.25"})
    assert s2.paper_buy_fee == 0.5 and s2.paper_sell_fee == 0.25  # still configurable
    assert s.paper_max_open == 5
    assert s.paper_daily_target == 50
    assert s.min_trade_score == 65
    assert s.solana_rpc_url.startswith("https://")
    assert s.warnings == ()


def test_env_overrides():
    s = load_settings(env={"PAPER_MAX_OPEN": "3", "SCAN_INTERVAL_SEC": "30", "TELEGRAM_CHAT_ID": " 123 "})
    assert s.paper_max_open == 3 and s.scan_interval_sec == 30 and s.telegram_chat_id == "123"


@pytest.mark.parametrize("key,val", [("PAPER_MAX_OPEN", "abc"), ("PAPER_MAX_OPEN", "0"),
                                     ("MIN_TRADE_SCORE", "10"), ("HTTP_TIMEOUT_SEC", "-1")])
def test_bad_numbers_fall_back_to_defaults(key, val):
    s = load_settings(env={key: val})
    assert getattr(s, key.lower()) == getattr(Settings(), key.lower())
    assert any(key in w for w in s.warnings)


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "LIVE"])
def test_paper_only_cannot_be_disabled(val):
    s = load_settings(env={"PAPER_ONLY": val})
    assert s.paper_only is True
    assert any("PAPER_ONLY" in w for w in s.warnings)


def test_paper_only_field_not_settable():
    with pytest.raises(TypeError):
        Settings(paper_only=False)  # init=False


def test_private_keys_are_flagged_and_never_stored():
    s = load_settings(env={"WALLET_PRIVATE_KEY": "abc", "SEED_PHRASE": "a b c"})
    assert sum("never needs a private key" in w for w in s.warnings) == 2
    assert "abc" not in repr(s.redacted())


def test_redacted_hides_secrets():
    s = load_settings(env={"TELEGRAM_BOT_TOKEN": "123:SECRET", "JUPITER_API_KEY": "k"})
    r = s.redacted()
    assert r["telegram_bot_token"] == "set" and r["jupiter_api_key"] == "set"
    assert "SECRET" not in str(r)


def test_bad_log_level():
    assert load_settings(env={"LOG_LEVEL": "loud"}).log_level == "INFO"


def test_dotenv_file_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPER_MAX_OPEN", raising=False)
    f = tmp_path / ".env"
    f.write_text("PAPER_MAX_OPEN=4\n# comment\n")
    assert load_settings(env_file=f).paper_max_open == 4
    monkeypatch.delenv("PAPER_MAX_OPEN", raising=False)


def test_env_example_is_safe():
    lines = dict(l.split("=", 1) for l in (PROJECT_ROOT / ".env.example").read_text().splitlines()
                 if l and not l.startswith("#"))
    for secret in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "JUPITER_API_KEY", "RUGCHECK_API_KEY", "BIRDEYE_API_KEY"):
        assert lines[secret] == "", f"{secret} must be empty in .env.example"
    assert lines["PAPER_ONLY"] == "true"
    assert not any("PRIVATE" in k or "SEED" in k for k in lines)


def test_gitignore_protects_secrets_and_data():
    gi = (PROJECT_ROOT / ".gitignore").read_text().split()
    for pat in (".env", "*.db", "data/", "logs/"):
        assert pat in gi


def test_relative_paths_resolve_under_project():
    s = load_settings(env={})
    assert s.path(s.db_path) == PROJECT_ROOT / "data" / "shadow.db"
    assert s.path("/tmp/x.db") == Path("/tmp/x.db")
