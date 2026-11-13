import asyncio, sqlite3, time, json, statistics, sys
sys.path.insert(0, "/workspace/shadow-meme-bot")
import httpx
from types import SimpleNamespace
from shadow.config import load_settings
from shadow.fastprice import FastPricer
from shadow.models import MarketData
from shadow.net import RateLimiter, fetch_json, UA

c = sqlite3.connect("file:/workspace/shadow-meme-bot/data/shadow.db?mode=ro", uri=True, timeout=30)
rows = c.execute("""SELECT mint, MAX(COALESCE(market_cap,fdv)) mc, MAX(liquidity_usd) l FROM token_market
                    WHERE updated_ts > strftime('%s','now')-3600 GROUP BY mint HAVING mc >= 1e6 AND l >= 50000 ORDER BY l DESC LIMIT 30""").fetchall() \
    if c.execute("SELECT name FROM sqlite_master WHERE name='token_market'").fetchone() else []
mints = [r[0] for r in rows]
print("mints", len(mints))
class Col:
    def __init__(s): s.tracked = {m: SimpleNamespace(market=MarketData(m, price_usd=0)) for m in mints}
src = SimpleNamespace(owner=lambda m: col)
col = Col()
now0 = time.time()
async def noop(): pass
eng = SimpleNamespace(paper=None, open={}, proximity={m: (80, 1e12) for m in mints}, fast_tick=noop)
async def jup_loop(cl, dur, out):
    lim = RateLimiter("jup_measure", 20)
    last = {}
    t_end = time.time() + dur
    while time.time() < t_end:
        try:
            d = await fetch_json(cl, lim, "https://lite-api.jup.ag/price/v3?ids=" + ",".join(mints[:30]))
            t = time.time(); out["calls"] += 1
            for m, v in (d or {}).items():
                p = (v or {}).get("usdPrice")
                if p is None: continue
                if m in last and p != last[m][1]: out["chg"].append(t - last[m][0]); last[m] = (t, p)
                elif m not in last: last[m] = (t, p)
        except Exception as e:
            out["err"].append(type(e).__name__ + str(e)[:60])
async def main():
    s = load_settings(env={})
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": UA}) as cl:
        fp = FastPricer(s, src, eng, client=cl, limiters={"dexscreener_fast": RateLimiter("dsf", 70)})
        jo = {"calls": 0, "chg": [], "err": []}
        dur = 180
        jt = asyncio.create_task(jup_loop(cl, dur, jo))
        t_end = time.time() + dur
        while time.time() < t_end:
            t0 = time.monotonic(); await fp.tick()
            await asyncio.sleep(max(0.05, 1.0 - (time.monotonic() - t0)))
        await jt
        st = fp.stats(); print("DEX", json.dumps(st))
        ch = sorted(jo["chg"])
        print("JUP calls", jo["calls"], "changes", len(ch), "p50", ch[len(ch)//2] if ch else None, "errors", jo["err"][:3])
        per_coin = len(fp.change_intervals) / max(1, len(mints))
        print("DEX price changes per coin in", dur, "s:", round(per_coin, 1), "| JUP:", round(len(ch)/max(1,len(mints)),1))
asyncio.run(main())
