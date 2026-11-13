"""Build tests/fixtures/candles_recorded.json from REAL data only:
  * 1m candles (with buys/sells) of new launches recorded by the phase-4 live test (data/live_p4.db)
  * 1m GeckoTerminal OHLCV of current MCAP / TOP VOLUME pools (fetched now, keyless, paced)
Windows where a detector fires are stored per detector, so every detector test runs on recorded data."""
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shadow.models import Candle, MarketData  # noqa: E402
from shadow.universes import meme_filter  # noqa: E402
from shadow.signals import DETECTORS, compute_features, run_detectors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
out = {"recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "series": {}, "windows": {}}

for dbname in ("live_p4.db", "live_p5.db"):
    if not (ROOT / "data" / dbname).exists():
        continue
    con = sqlite3.connect(ROOT / "data" / dbname)
    con.row_factory = sqlite3.Row
    markets = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM token_market")}
    migrated = {r["mint"]: r["migrated_ts"] for r in con.execute("SELECT mint, migrated_ts FROM seen_tokens WHERE migrated_ts IS NOT NULL")}
    unis = {}
    if dbname != "live_p4.db":
        unis = {r["mint"]: r["universe"] for r in con.execute("SELECT mint, universe FROM universe_members ORDER BY universe='volume'")}
    for (mint,) in con.execute("SELECT mint FROM candles_1m GROUP BY mint HAVING count(*)>=3"):
        rows = [dict(r) for r in con.execute("SELECT * FROM candles_1m WHERE mint=? ORDER BY ts", (mint,))]
        m = markets.get(mint, {})
        out["series"][f"{dbname[:-3]}:{mint}"] = {
            "mint": mint, "universe": unis.get(mint, "new"), "source": dbname, "symbol": m.get("symbol") or "",
            "liquidity_usd": m.get("liquidity_usd"), "launch_ts": m.get("launch_ts"), "migrated_ts": migrated.get(mint),
            "candles": [[r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume_usd"], r["buys"], r["sells"]]
                        for r in rows]}

uni = json.load(open(ROOT / "tests/fixtures/universe_recorded.json"))
pools = []
for key in ("gt_top_volume", "gt_trending"):
    for p in uni[key]["data"][:6]:
        a = p["attributes"]
        pools.append((p["id"].split("_", 1)[1], a.get("name"), float(a.get("reserve_in_usd") or 0),
                      p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1],
                      float(a.get("market_cap_usd") or a.get("fdv_usd") or 0), float(a.get("base_token_price_usd") or 0)))
seen = set()
with httpx.Client(timeout=20, headers={"accept": "application/json"}) as c:
    for addr, name, liq, mint, mcap, price in pools:
        if mint in seen or len(seen) >= 8 or meme_filter(mint, name.split(" /")[0], "", price, False, set()):
            continue
        r = c.get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{addr}/ohlcv/minute",
                  params={"aggregate": 1, "limit": 120, "currency": "usd", "token": "base"})
        time.sleep(7)
        if r.status_code != 200:
            print("skip", name, r.status_code)
            continue
        lst = sorted(r.json()["data"]["attributes"]["ohlcv_list"])
        if len(lst) < 30:
            continue
        seen.add(mint)
        u = "mcap" if mcap >= 1e6 else "volume"
        out["series"][f"gt:{mint}"] = {"mint": mint, "migrated_ts": None, "universe": u, "source": f"geckoterminal ohlcv pool {addr}", "symbol": name,
                               "liquidity_usd": liq, "launch_ts": None,
                               "candles": [[x[0], x[1], x[2], x[3], x[4], x[5], 0, 0] for x in lst]}
        print("gt", name, u, len(lst))


def to_candles(mint, ser):
    return [Candle(mint, t, o, h, l, cl, v, b, s, 1, "fixture") for t, o, h, l, cl, v, b, s in ser["candles"]]


for key, ser in out["series"].items():
    mint = ser["mint"]
    cs = to_candles(mint, ser)
    md = MarketData(mint, symbol=ser["symbol"], liquidity_usd=ser["liquidity_usd"], launch_ts=ser["launch_ts"])
    for end in range(3, len(cs) + 1):
        win = cs[max(0, end - 60):end]
        f = compute_features(win, md, ser["universe"], win[-1].ts + 60, ser["migrated_ts"])
        for k, d in run_detectors(f, ser["universe"]).items():
            if d.fired and len(out["windows"].setdefault(k, [])) < 3 and \
                    not any(w["series"] == key for w in out["windows"][k]):
                out["windows"][k].append({"series": key, "universe": ser["universe"], "start": max(0, end - 60),
                                          "end": end, "detail": d.detail})
print({k: len(out["windows"].get(k, [])) for k in DETECTORS})
json.dump(out, open(ROOT / "tests/fixtures/candles_recorded.json", "w"), separators=(",", ":"))
print("series", len(out["series"]), "bytes", (ROOT / "tests/fixtures/candles_recorded.json").stat().st_size)
