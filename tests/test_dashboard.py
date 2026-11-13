"""Phase 9 dashboard: snapshot (sanitized, real data only), URL summary, GitHub publisher (mocked), Telegram
/dashboard + menu button, and the Mini App rendered in headless Chrome."""
import asyncio
import json
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from shadow import dashboard as dash
from shadow.config import load_settings
from shadow.db import Database
from shadow.paper import PaperEngine
from tests.test_paper import NOW, PARAMS, Chk, Src, rep, sig

ROOT = Path(__file__).resolve().parents[1]
SECRETS = {"TELEGRAM_BOT_TOKEN": "123456789:AAsecretTelegramTokenValue_zzzzzzzzzzzz",
           "GITHUB_TOKEN": "github_pat_SECRETSECRETSECRET1234567890",
           "JUPITER_API_KEY": "jup-secret-key-123456", "RUGCHECK_API_KEY": "rug-secret-key-123456",
           "BIRDEYE_API_KEY": "bird-secret-key-123456"}


def settings(tmp_path, **env):
    return load_settings(env={"DB_PATH": str(tmp_path / "d.db"), **SECRETS, **{k.upper(): str(v) for k, v in env.items()}})


def filled(tmp_path):
    s = settings(tmp_path)
    db = Database(str(tmp_path / "d.db"))
    clock = {"t": NOW}
    src, chk = Src(), Chk()
    pe = PaperEngine(s, db, src=src, checker=chk, clock=lambda: clock["t"])
    a, b = "A" * 44, "B" * 44
    for m in (a, b):
        chk.reps[m] = rep(m)
    pa = pe.open_from_signal(sig(a, 1), chk.reps[a], PARAMS)
    pe.open_from_signal({**sig(b, 2), "symbol": "<img src=x onerror=alert(1)>"}, chk.reps[b], PARAMS)
    clock["t"] += 600
    pe.close(next(p for p in pe.open_positions() if p["id"] == pa), 1.2, "take_profit")
    db.add_signal({"ts": NOW, "mint": a, "symbol": "AAAA", "universe": "new", "strategy": "early_entry",
                   "alert_type": "BUY_WATCH", "score": 77, "judge_verdict": "APPROVE", "judge_confidence": 0.71,
                   "reason": "early move"})
    db.add_signal({"ts": NOW, "mint": b, "symbol": "BBBB", "universe": "mcap", "alert_type": "REJECT", "reason": "x"})
    db.add_strategy_update("param_change", "early_entry", 1, 2, {"tp_pct": [30, 33]}, "better on unseen data",
                           {"before": {"version": 1, "score": 0.4, "n": 41}, "after": {"version": 2, "score": 1.3, "n": 41},
                            "secret_field": "nope"})
    for i in range(5):
        db.con.execute("INSERT INTO paper_equity (ts, cash, open_value, equity, n_open, realized_pnl) VALUES (?,?,?,?,?,?)",
                       (NOW + i * 60, 96, 4, 100 + i * 0.1, 2, 0))
    return s, db, clock


# ------------------------------------------------------------------ snapshot
def test_empty_db_snapshot_has_only_empty_states(tmp_path):
    s = settings(tmp_path)
    db = Database(str(tmp_path / "d.db"))
    snap = dash.build_snapshot(db, s, now=NOW)
    assert snap["paper_only"] is True and snap["schema"] == 1 and snap["status"]["state"] == "offline"
    assert snap["positions"] == [] and snap["history"] == [] and snap["equity_curve"] == []
    assert snap["signals"]["recent"] == [] and snap["signals"]["today"] == {} and snap["updates"] == []
    assert snap["account"]["equity"] == s.paper_start_balance and snap["account"]["closed"]["n"] == 0
    assert snap["scanner"]["live"] is False and snap["scanner"]["mode"] is None and snap["scanner"]["tokens_24h"] == 0


def test_snapshot_with_real_rows(tmp_path):
    s, db, clock = filled(tmp_path)
    live = {"scanner_mode": "websocket+polling", "uptime_s": 600, "new_tokens": 12, "tracked_new": 4,
            "safety_checked": 9, "errors_recent": 1, "telegram": "ok",
            "last_errors": ["https://rpc.example/?api-key=SHOULD_NOT_LEAK"]}
    snap = dash.build_snapshot(db, s, live, now=clock["t"], paused=True)
    assert snap["status"] == {"state": "paused", "paused": True, "source": "live"}
    assert len(snap["positions"]) == 1 and len(snap["history"]) == 1
    h = snap["history"][0]
    assert h["exit_reason"] == "take_profit" and h["pnl_usd"] > 0
    assert snap["signals"]["today"]["new"]["BUY_WATCH"] == 1 and snap["signals"]["today"]["mcap"]["REJECT"] == 1
    assert [r["type"] for r in snap["signals"]["recent"]] == ["BUY_WATCH"]          # REJECTs only counted
    u = snap["updates"][0]
    assert u["changes"] == {"tp_pct": [30, 33]} and u["metrics"]["after"]["n"] == 41 and "secret_field" not in u["metrics"]
    assert snap["equity_curve"][-1][1] == snap["account"]["equity"]                # curve ends at the current equity
    assert set(snap["strategies"]) >= {"early_entry", "first_pullback", "migration_breakout"}
    assert snap["scanner"]["mode"] == "websocket+polling" and snap["scanner"]["live"] is True
    assert snap["instance"] == dash.build_snapshot(db, s)["instance"]              # stable per install


def test_snapshot_never_contains_secrets_or_error_texts(tmp_path):
    s, db, clock = filled(tmp_path)
    snap = dash.build_snapshot(db, s, {"scanner_mode": "x", "last_errors": ["api-key=SHOULD_NOT_LEAK"]}, now=clock["t"])
    raw = dash.snapshot_json(snap).decode()
    for v in SECRETS.values():
        assert v not in raw
    assert "SHOULD_NOT_LEAK" not in raw and "api-key" not in raw and "secret_field" not in raw
    assert set(snap) == {"schema", "app", "version", "paper_only", "instance", "generated_ts", "tz", "status", "account",
                         "equity_curve", "positions", "history", "signals", "judge", "strategies", "judge_params",
                         "updates", "scanner"}
    url = dash.dashboard_url(s, snap)
    for v in SECRETS.values():
        assert v not in url


def test_snapshot_is_compact_when_db_is_big(tmp_path):
    s = settings(tmp_path)
    db = Database(str(tmp_path / "d.db"))
    with db.tx():
        for i in range(3000):
            db.con.execute("INSERT INTO paper_equity (ts, cash, open_value, equity, n_open, realized_pnl) VALUES (?,?,?,?,?,?)",
                           (NOW + i * 60, 100, 0, 100 + (i % 7), 0, 0))
        for i in range(400):
            db.con.execute("INSERT INTO paper_positions (mint, symbol, universe, strategy, status, open_ts, exit_ts, size_usd, "
                           "pnl_usd, pnl_pct, exit_reason, entry_price, exit_price, buy_fee, sell_fee, proceeds) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,2)", (f"M{i:043d}", "X" * 80, "new", "early_entry", "closed",
                                                                     NOW + i, NOW + i + 60, 2, 0.1, 5, "tp", 1, 1.05))
        for i in range(500):
            db.con.execute("INSERT INTO signals (ts, mint, symbol, universe, alert_type, reason) VALUES (?,?,?,?,?,?)",
                           (NOW + i, f"S{i}", "Y", "volume", "BUY_WATCH", "r" * 500))
    snap = dash.build_snapshot(db, s, now=NOW + 200000)
    b = dash.snapshot_json(snap)
    assert len(snap["equity_curve"]) <= 241 and len(snap["history"]) == 50 and len(snap["signals"]["recent"]) == 40
    assert len(b) < 120_000
    assert all(len(h["symbol"]) <= dash.MAX_SYMBOL for h in snap["history"])
    assert all(len(r["reason"]) <= dash.MAX_REASON for r in snap["signals"]["recent"])
    assert len(dash.dashboard_url(s, snap)) < 1100


def test_summary_roundtrip(tmp_path):
    s, db, clock = filled(tmp_path)
    snap = dash.build_snapshot(db, s, {}, now=clock["t"])
    q = dash.dashboard_url(s, snap).split("?s=", 1)[1]
    d = dash.decode_summary(q)
    assert d["eq"] == round(snap["account"]["equity"], 2) and d["o"] == 1 and d["cl"] == 1 and d["w"] == 1
    assert d["i"] == snap["instance"] and d["st"] == "r" and d["sg"]["new"] == [1, 0, 0] and d["ec"]
    assert len(q) <= dash.MAX_SUMMARY_CHARS and "=" not in q and "+" not in q and "/" not in q
    assert dash.dashboard_url(s) == "https://ahmadrahmani147370-hype.github.io/shadow-meme-bot/"


# ------------------------------------------------------------------ publisher (mocked GitHub API)
class FakeGH:
    def __init__(self, ref_exists=True, status=None):
        self.calls, self.ref_exists, self.status = [], ref_exists, status

    def handler(self, req: httpx.Request):
        body = json.loads(req.content or b"{}")
        self.calls.append((req.method, req.url.path, body, req.headers.get("authorization")))
        if self.status:
            return httpx.Response(self.status, json={"message": "Bad credentials"})
        if req.url.path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "t" * 40})
        if req.url.path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "c" * 40})
        if req.method == "PATCH":
            return httpx.Response(200 if self.ref_exists else 422, json={"message": "Reference does not exist"})
        if req.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": body["ref"]})
        return httpx.Response(404, json={})


def pub(fake):
    return dash.GitHubPublisher(SECRETS["GITHUB_TOKEN"], "o/r", client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))


def test_publisher_force_pushes_one_orphan_commit():
    fake = FakeGH()
    sha = asyncio.run(pub(fake).publish(b'{"a":1}', "snap"))
    assert sha == "c" * 40
    m = [(c[0], c[1]) for c in fake.calls]
    assert m == [("POST", "/repos/o/r/git/trees"), ("POST", "/repos/o/r/git/commits"),
                 ("PATCH", "/repos/o/r/git/refs/heads/dashboard-data")]
    tree = fake.calls[0][2]["tree"]
    assert tree[0] == {"path": "snapshot.json", "mode": "100644", "type": "blob", "content": '{"a":1}'}
    assert fake.calls[1][2]["parents"] == [] and fake.calls[2][2] == {"sha": "c" * 40, "force": True}
    assert fake.calls[0][3] == f"Bearer {SECRETS['GITHUB_TOKEN']}"


def test_publisher_creates_branch_when_missing():
    fake = FakeGH(ref_exists=False)
    asyncio.run(pub(fake).publish(b"{}"))
    assert fake.calls[-1][1] == "/repos/o/r/git/refs" and fake.calls[-1][2]["ref"] == "refs/heads/dashboard-data"


def test_publisher_auth_error_disables_and_hides_token():
    fake = FakeGH(status=401)
    p = pub(fake)
    with pytest.raises(dash.GitHubError) as ei:
        asyncio.run(p.publish(b"{}"))
    assert ei.value.code == 401 and p.disabled and SECRETS["GITHUB_TOKEN"] not in str(ei.value)
    n = len(fake.calls)
    with pytest.raises(dash.GitHubError):
        asyncio.run(p.publish(b"{}"))
    assert len(fake.calls) == n                                         # no more requests with a bad token


def test_publisher_network_error_is_redacted():
    def boom(req):
        raise httpx.ConnectTimeout(f"timeout {SECRETS['GITHUB_TOKEN']}")
    p = dash.GitHubPublisher(SECRETS["GITHUB_TOKEN"], "o/r", client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(dash.GitHubError) as ei:
        asyncio.run(p.publish(b"{}"))
    assert SECRETS["GITHUB_TOKEN"] not in str(ei.value) and p.failures == 1 and not p.disabled


# ------------------------------------------------------------------ Telegram /dashboard + menu button
def test_dashboard_command_sends_web_app_button(tmp_path):
    from tests.test_telegram import CHAT, FakeApp, drain, mk_bot

    class App(FakeApp):
        def dashboard_link(self):
            return "https://ahmadrahmani147370-hype.github.io/shadow-meme-bot/?s=abc"

        def dashboard_info(self):
            return {"summary": "Equity $100.00 | open 0"}
    bot, fake, db, s = mk_bot(tmp_path, app=App())
    asyncio.run(bot.run_command("dashboard"))
    asyncio.run(drain(bot))
    m = fake.sent()[0]["body"]
    assert m["chat_id"] == CHAT and "PAPER ONLY" in m["text"] and "GITHUB_TOKEN" in m["text"]
    btn = m["reply_markup"]["inline_keyboard"][0][0]
    assert btn["web_app"]["url"].endswith("?s=abc") and "dashboard" in btn["text"].lower()


def test_menu_button_set_once_per_url_and_never_raises(tmp_path):
    from tests.test_telegram import mk_bot
    bot, fake, db, s = mk_bot(tmp_path)
    url = "https://ahmadrahmani147370-hype.github.io/shadow-meme-bot/?s=1"
    assert asyncio.run(bot.refresh_menu_button(url)) is True
    assert asyncio.run(bot.refresh_menu_button(url)) is False
    c = [x for x in fake.calls if x["method"] == "setChatMenuButton"]
    assert len(c) == 1 and c[0]["body"]["menu_button"] == {"type": "web_app", "text": "📊 Dashboard", "web_app": {"url": url}}
    fake.fail = [400]
    assert asyncio.run(bot.refresh_menu_button(url + "2")) is False and bot.stats["menu_button_fail"] == 1
    bot2, *_ = mk_bot(tmp_path / "x", telegram_menu_button="false")
    assert asyncio.run(bot2.refresh_menu_button(url)) is False


def test_app_publish_dashboard_local_without_token_and_github_with(tmp_path):
    from shadow.app import App
    s = load_settings(env={"DB_PATH": str(tmp_path / "a.db")})

    async def go():
        app = App(s, telegram=False)
        assert app.publisher is None
        r = await app.publish_dashboard()
        assert r["published"] is False and Path(r["local"]).exists()
        snap = json.loads(Path(r["local"]).read_text())
        assert snap["status"]["state"] == "running" and snap["scanner"]["live"] is True
        assert "?s=" in app.dashboard_link() and "full" not in app.dashboard_info()
        fake = FakeGH()
        app.publisher = pub(fake)
        r = await app.publish_dashboard()
        assert r["published"] and r["commit"] == "c" * 40 and "updated" in app.dashboard_info()["full"]
        await app.client.aclose()
        app.db.close()
    asyncio.run(go())


# ------------------------------------------------------------------ Mini App in headless Chrome
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


@pytest.fixture
def site(tmp_path):
    d = tmp_path / "site"
    shutil.copytree(ROOT / "docs", d)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(d)))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield d, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def dom(url):
    out = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000",
                          "--dump-dom", url], capture_output=True, text=True, timeout=90)
    return out.stdout


needs_chrome = pytest.mark.skipif(CHROME is None, reason="no headless Chrome/Chromium")


@needs_chrome
def test_page_renders_full_snapshot_and_escapes_token_names(tmp_path, site):
    d, base = site
    s, db, clock = filled(tmp_path)
    (d / "snap.json").write_bytes(dash.snapshot_json(dash.build_snapshot(db, s, {"scanner_mode": "websocket+polling"},
                                                                          now=clock["t"])))
    html = dom(f"{base}/index.html?data=snap.json&repo=o/r")
    for t in ("PAPER ONLY", "Paper account", "Open positions", "Trade history", "Signals per universe", "TOP VOLUME",
              "Judge", "early_entry v1", "strategy_update history", "param change", "tp_pct 30 → 33",
              "better on unseen data", "Scanner health", "websocket+polling", "take_profit", "RUNNING"):
        assert t in html, t
    assert "&lt;img src=x onerror=alert" in html and "<img src=x" not in html   # text only (symbol cut to 24 chars)
    assert "No open paper positions" not in html


@needs_chrome
def test_page_empty_snapshot_shows_empty_states(tmp_path, site):
    d, base = site
    s = settings(tmp_path)
    db = Database(str(tmp_path / "d.db"))
    (d / "e.json").write_bytes(dash.snapshot_json(dash.build_snapshot(db, s, now=NOW)))
    html = dom(f"{base}/index.html?data=e.json")
    for t in ("No open paper positions.", "No closed paper trades yet.", "No BUY WATCH / SELL signals yet.",
              "No judge decisions yet", "No self-learning changes yet", "Not enough equity points yet",
              "Snapshot made from the database while the bot was not running."):
        assert t in html, t


@needs_chrome
def test_page_summary_only_and_no_data(tmp_path, site):
    d, base = site
    s, db, clock = filled(tmp_path)
    import time as _t
    snap = dash.build_snapshot(db, s, {}, now=_t.time())
    q = dash.dashboard_url(s, snap).split("?", 1)[1]
    html = dom(f"{base}/index.html?{q}&data=missing.json")
    assert "Summary only" in html and "GITHUB_TOKEN" in html and f"${snap['account']['equity']:.2f}" in html
    assert "Open positions needs the full data" in html
    html = dom(f"{base}/index.html?data=missing.json")
    assert "No data yet" in html and "NO DATA" in html


@needs_chrome
def test_page_hides_snapshot_from_another_install(tmp_path, site):
    d, base = site
    s, db, clock = filled(tmp_path)
    import time as _t
    snap = dash.build_snapshot(db, s, {}, now=_t.time())
    other = dict(snap, instance="ffffffffffff")
    (d / "o.json").write_bytes(dash.snapshot_json(other))
    q = dash.dashboard_url(s, snap).split("?", 1)[1]
    html = dom(f"{base}/index.html?{q}&data=o.json")
    assert "different bot install" in html and "Summary only" in html


@needs_chrome
def test_page_flags_stale_data(tmp_path, site):
    d, base = site
    s, db, clock = filled(tmp_path)
    import time as _t
    (d / "old.json").write_bytes(dash.snapshot_json(dash.build_snapshot(db, s, {}, now=_t.time() - 3600)))
    html = dom(f"{base}/index.html?data=old.json")
    assert "NO UPDATE" in html and "may be stopped" in html and "RUNNING" not in html


# ------------------------------------------------------------------ gh CLI publisher (no token in .env)
FAKE_GH = r'''#!/usr/bin/env python3
import json, sys, os
args = sys.argv[1:]
body = json.loads(sys.stdin.read() or "{}")
log = os.environ["FAKE_GH_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "body": body}) + "\n")
method, path = args[args.index("-X") + 1], args[args.index("-X") + 2]
missing = os.environ.get("FAKE_GH_REF_MISSING") == "1"
if path.endswith("/git/trees"):
    code, out = 201, {"sha": "t" * 40}
elif path.endswith("/git/commits"):
    code, out = 201, {"sha": "c" * 40}
elif method == "PATCH":
    code, out = (422, {"message": "Reference does not exist"}) if missing else (200, {"object": {"sha": "c" * 40}})
else:
    code, out = 201, {"ref": body.get("ref")}
sys.stdout.write(f"HTTP/2.0 {code} X\r\nContent-Type: application/json\r\n\r\n" + json.dumps(out))
'''


@pytest.mark.parametrize("missing", ["0", "1"])
def test_gh_cli_publisher(tmp_path, monkeypatch, missing):
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_REF_MISSING", missing)
    p = dash.GhCliPublisher("o/r", gh=str(gh))
    assert asyncio.run(p.publish(b'{"x":1}')) == "c" * 40 and p.pushes == 1
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    paths = [c["args"][c["args"].index("-X") + 2] for c in calls]
    assert paths[:3] == ["repos/o/r/git/trees", "repos/o/r/git/commits", "repos/o/r/git/refs/heads/dashboard-data"]
    assert (paths[-1] == "repos/o/r/git/refs") == (missing == "1")
    assert calls[1]["body"]["parents"] == [] and calls[0]["body"]["tree"][0]["content"] == '{"x":1}'


def test_gh_cli_publisher_missing_binary_is_a_clean_error():
    p = dash.GhCliPublisher("o/r", gh="/nonexistent/gh")
    with pytest.raises(dash.GitHubError):
        asyncio.run(p.publish(b"{}"))
    assert p.failures == 1


def test_make_publisher_modes(tmp_path):
    base = {"DB_PATH": str(tmp_path / "x.db")}
    assert dash.make_publisher(load_settings(env=base)) is None
    assert isinstance(dash.make_publisher(load_settings(env={**base, "GITHUB_TOKEN": "t" * 20})), dash.GitHubPublisher)
    assert type(dash.make_publisher(load_settings(env={**base, "DASHBOARD_PUBLISHER": "gh"}))) is dash.GhCliPublisher
    assert dash.make_publisher(load_settings(env={**base, "GITHUB_TOKEN": "t" * 20, "DASHBOARD_PUBLISHER": "off"})) is None
