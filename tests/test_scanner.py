import asyncio
import json
from pathlib import Path

import httpx
import pytest

from shadow import scanner as sc
from shadow.config import load_settings
from shadow.db import SCHEMA_VERSION, Database
from shadow.models import AMM, BONDING, MIGRATED, MIGRATION, NEW_TOKEN, UNKNOWN, TokenEvent
from shadow.net import RateLimiter, RateLimitError, fetch_json

FIX = Path(__file__).parent / "fixtures"
PP = json.loads((FIX / "pumpportal_messages.json").read_text())       # recorded 2026-11-13
GT = json.loads((FIX / "gt_new_pools.json").read_text())
RC = json.loads((FIX / "rugcheck_new_tokens.json").read_text())
CREATES = [m for m in PP if m.get("txType") == "create"]
MIGRATE = next(m for m in PP if m.get("txType") == "migrate")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def settings(tmp_path):
    return load_settings(env={"DB_PATH": str(tmp_path / "t.db")})


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- parsing
def test_parse_pumpportal_create():
    m = CREATES[0]
    ev = sc.parse_pumpportal(m, now=1000.0)
    assert ev.kind == NEW_TOKEN and ev.mint == m["mint"] and ev.source == "pumpportal"
    assert ev.name == m["name"].strip() and ev.symbol == m["symbol"].strip()
    assert ev.creator == m["traderPublicKey"] and ev.dev_buy_sol == m["solAmount"]
    assert ev.dev_buy_tokens == m["initialBuy"] and ev.initial_mcap_sol == pytest.approx(m["marketCapSol"])
    assert ev.status == BONDING and ev.launchpad == "pump" and ev.launch_ts == 1000.0
    assert ev.bonding_curve == m["bondingCurveKey"]


def test_parse_pumpportal_migrate_and_acks():
    ev = sc.parse_pumpportal(MIGRATE)
    assert ev.kind == MIGRATION and ev.status == MIGRATED and ev.pool == "pump-amm"
    assert sc.parse_pumpportal(PP[0]) is None  # "Successfully subscribed..." ack
    assert sc.parse_pumpportal({"txType": "buy", "mint": "x"}) is None


def test_parse_geckoterminal_new_pools():
    evs = sc.parse_gt_new_pools(GT, now=1.0)
    assert len(evs) == len(GT["data"])
    by_status = {e.status for e in evs}
    assert BONDING in by_status and AMM in by_status
    first = evs[0]
    assert first.mint == "4CorCgEhTp97TFUM3zXMV1JR217CJC2r2Az54YGgpump"
    assert first.name == "Ryutaro" and first.symbol == "Ryutaro" and first.launchpad == "pump-fun"
    assert first.launch_ts and first.initial_mcap_usd and first.pool_address
    amm = next(e for e in evs if e.status == AMM)
    assert amm.pool == "meteora-damm-v2" and amm.launchpad == ""


def test_gt_reversed_pair_and_quote_only_pools_are_handled():
    payload = {"data": [
        {"attributes": {"address": "P1", "name": "SOL / XYZ"}, "relationships": {
            "base_token": {"data": {"id": f"solana_{sc.WSOL}"}}, "quote_token": {"data": {"id": "solana_XYZmint"}},
            "dex": {"data": {"id": "pumpswap"}}}},
        {"attributes": {"address": "P2", "name": "SOL / USDC"}, "relationships": {
            "base_token": {"data": {"id": f"solana_{sc.WSOL}"}},
            "quote_token": {"data": {"id": "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}},
            "dex": {"data": {"id": "orca"}}}}]}
    evs = sc.parse_gt_new_pools(payload)
    assert [e.mint for e in evs] == ["XYZmint"] and evs[0].status == AMM


def test_parse_rugcheck_new_tokens():
    evs = sc.parse_rugcheck_new(RC)
    assert len(evs) == len(RC)
    assert evs[0].mint == RC[0]["mint"] and evs[0].creator == RC[0]["creator"] and evs[0].status == UNKNOWN
    assert evs[0].launch_ts and evs[0].launch_ts > 1.7e9  # nanosecond ISO parsed


def test_iso_ts_variants():
    assert sc._iso_ts("2026-11-13T01:47:32Z") == sc._iso_ts("2026-11-13T01:47:32.000000000Z")
    assert sc._iso_ts("2026-11-13T01:49:11.878141333Z") == pytest.approx(sc._iso_ts("2026-11-13T01:49:11Z") + 0.878141, abs=1e-5)
    assert sc._iso_ts("garbage") is None and sc._iso_ts(None) is None


# ---------------------------------------------------------------- dedup + db
def test_dedup_new_tokens_and_restart(tmp_path, settings):
    d1 = Database(tmp_path / "t.db")
    s1 = sc.Scanner(settings, d1)
    for m in CREATES:
        s1.handle_ws_raw(json.dumps(m))
    n_unique = len({m["mint"] for m in CREATES})
    assert s1.stats["new_tokens"] == n_unique and s1.queue.qsize() == n_unique
    s1.handle_ws_raw(json.dumps(CREATES[0]))
    assert s1.stats["duplicates"] == 1
    d1.close()
    d2 = Database(tmp_path / "t.db")  # "restart"
    s2 = sc.Scanner(settings, d2)
    for m in CREATES:
        s2.handle_ws_raw(json.dumps(m))
    assert s2.stats["new_tokens"] == 0 and s2.stats["duplicates"] == len(CREATES)
    d2.close()


def test_migration_flow(db, settings):
    s = sc.Scanner(settings, db)
    create = dict(CREATES[0], mint=MIGRATE["mint"])
    assert s.handle_ws_raw(json.dumps(create)).status == BONDING
    ev = s.handle_ws_raw(json.dumps(MIGRATE))
    assert ev.kind == MIGRATION and db.status_of(MIGRATE["mint"]) == MIGRATED
    assert db.get_seen(MIGRATE["mint"])["pool"] == "pump-amm"
    assert s.handle_ws_raw(json.dumps(MIGRATE)) is None
    assert s.stats["migrations"] == 1 and s.stats["duplicate_migrations"] == 1


def test_migration_of_unseen_token_is_recorded(db, settings):
    s = sc.Scanner(settings, db)
    assert s.handle_ws_raw(json.dumps(MIGRATE)).kind == MIGRATION
    assert db.status_of(MIGRATE["mint"]) == MIGRATED


def test_bonding_token_on_new_amm_pool_counts_as_migration(db, settings):
    s = sc.Scanner(settings, db)
    s.handle(TokenEvent(NEW_TOKEN, "M1", "pumpportal", status=BONDING))
    ev = s.handle(TokenEvent(NEW_TOKEN, "M1", "geckoterminal", status=AMM, pool="pumpswap", pool_address="PA"))
    assert ev.kind == MIGRATION and db.get_seen("M1")["pool_address"] == "PA"
    assert s.handle(TokenEvent(NEW_TOKEN, "M1", "geckoterminal", status=AMM, pool="pumpswap")) is None


def test_later_source_fills_missing_name(db, settings):
    s = sc.Scanner(settings, db)
    s.handle(TokenEvent(NEW_TOKEN, "M2", "rugcheck", symbol="ABC"))
    s.handle(TokenEvent(NEW_TOKEN, "M2", "geckoterminal", name="Alpha Beta", symbol="ABC2", creator="DEV"))
    row = db.get_seen("M2")
    assert row["name"] == "Alpha Beta" and row["symbol"] == "ABC" and row["creator"] == "DEV"


def test_db_migrations_are_versioned_and_idempotent(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE something_else (x)")
    con.commit()
    con.close()
    d = Database(p)
    assert d.version == SCHEMA_VERSION
    d.close()
    d = Database(p)
    assert d.version == SCHEMA_VERSION and d.count_seen() == {"total": 0}
    d.close()


def test_queue_drops_oldest_when_full(db, settings):
    s = sc.Scanner(settings, db, queue_size=2)
    for i in range(3):
        s.handle(TokenEvent(NEW_TOKEN, f"Q{i}", "pumpportal"))
    assert s.stats["queue_dropped"] == 1 and s.queue.get_nowait().mint == "Q1"


def test_bad_ws_messages_counted(db, settings):
    s = sc.Scanner(settings, db)
    s.handle_ws_raw("not json")
    s.handle_ws_raw("[1,2]")
    s.handle_ws_raw(json.dumps({"errors": "rate limited"}))
    assert s.stats["ws_bad_json"] == 1 and s.stats["ws_unknown"] == 1 and s.stats["ws_server_errors"] == 1


# ---------------------------------------------------------------- backoff + reconnect
def test_backoff_sequence_and_cap():
    seq = [sc.backoff_delay(i, 1, 60, rand=lambda: 0.5) for i in range(9)]
    assert seq == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert 0.8 <= sc.backoff_delay(0, rand=lambda: 0.0) and sc.backoff_delay(0, rand=lambda: 1.0) <= 1.2


class FakeWS:
    def __init__(self, messages, end="close", gap=0.0):
        self.messages, self.end, self.gap, self.sent = list(messages), end, gap, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(json.loads(m))

    async def recv(self):
        if self.messages:
            if self.gap:
                await asyncio.sleep(self.gap)
            return json.dumps(self.messages.pop(0))
        if self.end == "hang":
            await asyncio.sleep(3600)
        raise ConnectionResetError("server closed")


def scripted_connect(script):
    """script items: an Exception instance (connect fails) or a FakeWS."""
    calls = []

    def connect():
        item = script.pop(0) if script else OSError("no more")
        calls.append(item)
        if isinstance(item, Exception):
            raise item
        return item
    return connect, calls


async def _run_for(scanner, seconds):
    scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()


def test_reconnect_with_backoff_then_receive(db, settings):
    ws = FakeWS(PP[:2] + CREATES[:3], end="hang")
    connect, calls = scripted_connect([OSError("dns"), OSError("refused"), ws])
    s = sc.Scanner(settings, db, connect=connect, backoff_base=0.01, backoff_max=0.05,
                   fallback_after=999, poll_always=False, rand=lambda: 0.5)
    run(_run_for(s, 0.3))
    assert s.stats["ws_errors"] == 2 and s.stats["ws_connects"] == 1
    assert s.backoffs[:2] == [0.01, 0.02]
    assert ws.sent == [{"method": "subscribeNewToken"}, {"method": "subscribeMigration"}]
    assert s.stats["new_tokens"] == 3 and s.stats["ws_acks"] == 2 and s.ws_up


def test_backoff_resets_after_stable_session(db, settings):
    script = [FakeWS(CREATES[:1]), FakeWS(CREATES[1:2]), FakeWS(CREATES[2:3], end="hang")]
    connect, _ = scripted_connect(script)
    s = sc.Scanner(settings, db, connect=connect, backoff_base=0.01, stable_after=0.0, fallback_after=999, poll_always=False,
                   rand=lambda: 0.5)
    run(_run_for(s, 0.3))
    assert s.backoffs[:2] == [0.01, 0.01]  # each session counted as healthy -> attempt reset
    assert s.stats["ws_disconnects"] == 2 and s.stats["new_tokens"] == 3


def test_idle_watchdog_reconnects(db, settings):
    connect, calls = scripted_connect([FakeWS(PP[:1], end="hang"), FakeWS([], end="hang")])
    s = sc.Scanner(settings, db, connect=connect, idle_timeout=0.05, backoff_base=0.01, fallback_after=999, poll_always=False)
    run(_run_for(s, 0.3))
    assert s.stats["ws_idle_timeouts"] >= 1 and len(calls) >= 2


# ---------------------------------------------------------------- fallback switching
def fixture_transport(counter):
    def handler(req: httpx.Request):
        counter[req.url.host] = counter.get(req.url.host, 0) + 1
        if "geckoterminal" in req.url.host:
            return httpx.Response(200, json=GT)
        if "rugcheck" in req.url.host:
            return httpx.Response(200, json=RC)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def fast_limiters():
    return {"geckoterminal": RateLimiter("geckoterminal", 6000), "rugcheck": RateLimiter("rugcheck", 6000)}


def test_fallback_starts_when_ws_down_and_stops_when_back(db, settings):
    counter = {}

    async def scenario():
        client = httpx.AsyncClient(transport=fixture_transport(counter))
        # ws fails 3 times (~0.2 s), then streams one create every 20 ms for a long time
        live = FakeWS(PP[:2] + CREATES * 30, gap=0.02, end="hang")
        connect, _ = scripted_connect([OSError("down")] * 3 + [live])
        s = sc.Scanner(settings, db, connect=connect, client=client, limiters=fast_limiters(), poll_always=False,
                       backoff_base=0.05, backoff_max=0.05, fallback_after=0.05, gt_interval=0.02,
                       rc_interval=0.02, tick=0.01, rand=lambda: 0.5)
        s.start()
        await asyncio.sleep(0.6)
        polls_when_back = dict(counter)
        await asyncio.sleep(0.2)
        await s.stop()
        await client.aclose()
        return s, polls_when_back

    s, polls_when_back = run(scenario())
    assert s.stats["fallback_on"] == 1 and s.stats["fallback_off"] == 1
    assert s.stats["new_geckoterminal"] > 0 and s.stats["new_rugcheck"] > 0
    assert counter == polls_when_back  # no polling after the websocket came back
    assert s.mode == "websocket"


def test_no_fallback_while_websocket_healthy(db, settings):
    counter = {}

    async def scenario():
        client = httpx.AsyncClient(transport=fixture_transport(counter))
        connect, _ = scripted_connect([FakeWS(PP[:2] + CREATES * 20, gap=0.01, end="hang")])
        s = sc.Scanner(settings, db, connect=connect, client=client, limiters=fast_limiters(), poll_always=False,
                       fallback_after=0.2, tick=0.01)
        await _run_for(s, 0.4)
        await client.aclose()
        return s

    s = run(scenario())
    assert counter == {} and s.stats["fallback_on"] == 0


def test_poll_always_supplements_healthy_websocket(db, settings):
    counter = {}

    async def scenario():
        client = httpx.AsyncClient(transport=fixture_transport(counter))
        connect, _ = scripted_connect([FakeWS(PP[:2] + CREATES * 20, gap=0.01, end="hang")])
        s = sc.Scanner(settings, db, connect=connect, client=client, limiters=fast_limiters(),
                       fallback_after=0.2, gt_interval=0.05, rc_interval=0.05, tick=0.01)
        assert s.poll_always is True  # default from settings
        await _run_for(s, 0.4)
        await client.aclose()
        return s

    s = run(scenario())
    assert counter and s.stats["fallback_on"] == 0 and s.ws_up
    assert s.stats["new_pumpportal"] > 0 and s.stats["new_geckoterminal"] > 0 and s.mode == "websocket+polling"


def test_scanner_reads_intervals_from_settings(db):
    s2 = load_settings(env={"GT_POLL_SEC": "30", "RC_POLL_SEC": "10", "SCANNER_POLL_ALWAYS": "false",
                            "WS_IDLE_TIMEOUT_SEC": "90", "FALLBACK_AFTER_SEC": "5"})
    s = sc.Scanner(s2, db)
    assert (s.gt_interval, s.rc_interval, s.poll_always, s.idle_timeout, s.fallback_after) == (30, 10, False, 90, 5)


def test_forced_fallback_and_rate_limit_handling(db, settings):
    async def scenario():
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if "rugcheck" in req.url.host:
                return httpx.Response(429, headers={"retry-after": "30"})
            return httpx.Response(200, json=GT)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        connect, _ = scripted_connect([])
        s = sc.Scanner(settings, db, connect=connect, client=client, limiters=fast_limiters(), force_fallback=True,
                       backoff_base=10, gt_interval=0.02, rc_interval=0.02, tick=0.01)
        await _run_for(s, 0.3)
        await client.aclose()
        return s

    s = run(scenario())
    unique = {e.mint for e in sc.parse_gt_new_pools(GT)}
    assert s.stats["new_geckoterminal"] == len(unique) and s.stats["duplicates"] >= len(GT["data"]) - len(unique)
    assert s.stats["poll_rugcheck_429"] == 1  # second attempt waits behind the 30 s penalty
    assert s.mode == "fallback(forced)"


def test_stream_yields_events(db, settings):
    async def scenario():
        connect, _ = scripted_connect([FakeWS(PP[:2] + CREATES[:2], end="hang")])
        s = sc.Scanner(settings, db, connect=connect, fallback_after=999, poll_always=False)
        got = []
        async for ev in s.stream():
            got.append(ev)
            if len(got) == 2:
                break
        await s.stop()
        return got
    got = run(scenario())
    assert [g.mint for g in got] == [m["mint"] for m in CREATES[:2]]


# ---------------------------------------------------------------- rate limiter
def test_rate_limiter_spaces_requests():
    async def scenario():
        rl = RateLimiter("x", 600)  # 0.1 s apart
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        stamps = []
        for _ in range(3):
            await rl.acquire()
            stamps.append(loop.time() - t0)
        return stamps
    st = run(scenario())
    assert st[1] >= 0.09 and st[2] >= 0.19


def test_fetch_json_429_penalizes_queue():
    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
        rl = RateLimiter("x", 6000)
        with pytest.raises(RateLimitError):
            await fetch_json(client, rl, "https://x.example/")
        await client.aclose()
        return rl
    rl = run(scenario())
    assert rl._next_at - rl.clock() > 0.005
