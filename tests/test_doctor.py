import asyncio
import json

import httpx
import pytest

from shadow import doctor
from shadow.config import load_settings
from shadow.sources import ENDPOINTS, Endpoint


@pytest.fixture
def no_pkg_check(monkeypatch):
    """Exit-code tests must not depend on pandas/numpy being installed on the test machine."""
    monkeypatch.setattr(doctor, "check_packages", lambda: [])


@pytest.fixture
def tmp_settings(tmp_path):
    return load_settings(env={"DB_PATH": str(tmp_path / "data" / "shadow.db"), "LOG_DIR": str(tmp_path / "logs")})


def test_python_check():
    assert doctor.check_python().status == doctor.OK
    assert doctor.check_python((99, 0)).status == doctor.FAIL


def test_package_check_reports_missing():
    res = {c.name: c for c in doctor.check_packages(("json", "definitely_not_a_pkg_123"))}
    assert res["json"].status == doctor.OK
    assert res["definitely_not_a_pkg_123"].status == doctor.FAIL


def test_paper_only_check(tmp_settings):
    assert doctor.check_paper_only(tmp_settings).status == doctor.OK


def test_write_access_and_sqlite_cycle(tmp_settings, tmp_path):
    assert all(c.status == doctor.OK for c in doctor.check_write_access(tmp_settings))
    c = doctor.check_sqlite(tmp_settings)
    assert c.status == doctor.OK, c.detail
    assert list((tmp_path / "data").iterdir()) == []  # scratch DB cleaned up


def test_database_check_creates_schema(tmp_settings):
    c = doctor.check_database(tmp_settings)
    from shadow.db import SCHEMA_VERSION
    assert c.status == doctor.OK and f"schema v{SCHEMA_VERSION}/{SCHEMA_VERSION}" in c.detail


def test_sqlite_fails_cleanly_on_unwritable_dir(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = load_settings(env={"DB_PATH": str(blocker / "sub" / "shadow.db")})
    assert doctor.check_sqlite(s).status == doctor.FAIL


def test_config_check_never_prints_token(tmp_settings):
    s = load_settings(env={"TELEGRAM_BOT_TOKEN": "999:TOPSECRET"})
    text = doctor.format_report(doctor.check_config(s))
    assert "TOPSECRET" not in text and "set" in text


def test_endpoint_registry_is_sane():
    names = [e.name for e in ENDPOINTS]
    assert len(names) == len(set(names))
    for e in ENDPOINTS:
        assert e.url.startswith(("https://", "wss://")), e.url
        assert "api-key=" not in e.url and "key=" not in e.url.split("?")[-1]
    req = {e.name for e in ENDPOINTS if e.required}
    assert {"pumpportal_ws", "dexscreener_tokens", "solana_rpc", "rugcheck_summary", "jupiter_quote"} <= req
    assert not any(e.required for e in ENDPOINTS if e.key_needed == "yes")


def _mock_transport(table):
    def handler(request: httpx.Request):
        for frag, (code, body) in table.items():
            if frag in str(request.url):
                return httpx.Response(code, text=body)
        return httpx.Response(404, text="nope")
    return httpx.MockTransport(handler)


def test_network_classification_with_mock(tmp_settings):
    eps = (
        Endpoint("good", "r", "https://good.example/x", expect="price"),
        Endpoint("rpc", "r", "https://rpc.example", kind="rpc", method="POST", rpc_method="getHealth", expect="ok"),
        Endpoint("cf", "r", "https://cf.example/x", expect="mint", required=False),
        Endpoint("down", "r", "https://down.example/x", expect="x"),
        Endpoint("slow", "r", "https://slow.example/x", expect="x"),
        Endpoint("keyed", "r", "https://keyed.example/x", required=False, key_needed="yes"),
        Endpoint("weird", "r", "https://weird.example/x", expect="price"),
    )
    t = _mock_transport({
        "good.example": (200, json.dumps({"price": 1})),
        "rpc.example": (200, '{"result":"ok"}'),
        "cf.example": (403, "<title>Just a moment...</title>"),
        "down.example": (500, "boom"),
        "slow.example": (429, "slow down"),
        "keyed.example": (401, "Unauthorized"),
        "weird.example": (200, "<html>maintenance</html>"),
    })
    res = {c.name: c for c in asyncio.run(doctor.check_network(tmp_settings, eps, transport=t))}
    assert res["good"].status == doctor.OK and res["good"].ms is not None
    assert res["rpc"].status == doctor.OK
    assert res["cf"].status == doctor.WARN and "Cloudflare" in res["cf"].detail
    assert res["down"].status == doctor.FAIL
    assert res["slow"].status == doctor.WARN
    assert res["keyed"].status == doctor.WARN and "API key" in res["keyed"].detail
    assert res["weird"].status == doctor.FAIL


def test_jupiter_key_header_only_when_set():
    e = next(x for x in ENDPOINTS if x.name == "jupiter_price")
    assert "x-api-key" not in doctor._endpoint_for_settings(e, load_settings(env={})).headers
    assert doctor._endpoint_for_settings(e, load_settings(env={"JUPITER_API_KEY": "k"})).headers["x-api-key"] == "k"


def test_custom_rpc_is_probed():
    e = next(x for x in ENDPOINTS if x.name == "solana_rpc")
    s = load_settings(env={"SOLANA_RPC_URL": "https://my-rpc.example"})
    assert doctor._endpoint_for_settings(e, s).url == "https://my-rpc.example"


def test_offline_run_and_exit_code(monkeypatch, tmp_settings, capsys, no_pkg_check):
    monkeypatch.setattr(doctor, "load_settings", lambda: tmp_settings)
    assert doctor.main(["--offline"]) == 0
    out = capsys.readouterr().out
    assert "READY" in out and "paper-only" in out


def test_exit_code_1_on_fail(monkeypatch, tmp_settings, no_pkg_check):
    monkeypatch.setattr(doctor, "load_settings", lambda: tmp_settings)
    monkeypatch.setattr(doctor, "check_python", lambda: doctor.Check("system", "python", doctor.FAIL, "old"))
    assert doctor.main(["--offline"]) == 1


def test_json_output(monkeypatch, tmp_settings, capsys, no_pkg_check):
    monkeypatch.setattr(doctor, "load_settings", lambda: tmp_settings)
    doctor.main(["--offline", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "sqlite" and c["status"] == "OK" for c in data)


def test_main_py_debug_doctor(monkeypatch, tmp_settings, no_pkg_check):
    import main

    monkeypatch.setattr(doctor, "load_settings", lambda: tmp_settings)
    assert main.main(["--debug", "doctor", "offline"]) == 0
    assert main.main(["--debug", "nope"]) == 2


def test_stubs_are_marked():
    import importlib
    for mod, ph in [("collector", 3), ("safety", 4), ("signals", 5), ("strategies", 5),
                    ("paper", 6), ("telegram_bot", 8), ("dashboard", 9)]:
        m = importlib.import_module(f"shadow.{mod}")
        assert m.PHASE == ph and m.IMPLEMENTED is False
        with pytest.raises(NotImplementedError):
            m.not_ready()


@pytest.mark.network
def test_live_required_sources_reachable(tmp_settings):
    """Real requests. Skip with: pytest -m 'not network'"""
    res = asyncio.run(doctor.check_network(tmp_settings))
    failed = [f"{c.name}: {c.detail}" for c in res if c.status == doctor.FAIL]
    assert not failed, failed
