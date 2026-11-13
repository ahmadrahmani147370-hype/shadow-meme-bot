"""Validate the shipped v1.1 entry logic on recorded candles: real compute_features / run_detectors /
score_signal / Strategy.entry_conditions + the v1.1 trade gates; exits simulated on 1m high/low
(stop first when a candle touches both = conservative), trailing as in PaperEngine."""
import sqlite3, sys, collections, statistics, bisect
sys.path.insert(0, '/workspace/shadow-meme-bot')
from shadow.models import Candle, MarketData
from shadow.signals import compute_features, run_detectors, score_signal
from shadow.strategies import DEFAULT_PARAMS, Strategy
G = dict(min_mcap=1e6, min_liq=50e3, min_ratio=0.02)
def load(DB):
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    mc, lq, uni = {}, {}, {}
    for m, a, l in c.execute("select mint, market_cap, liquidity_usd from token_market"):
        mc[m], lq[m] = a or 0, l
    for m, u, a, l in c.execute("select mint, universe, mcap, liquidity_usd from universe_members"):
        uni[m] = u; mc[m] = max(mc.get(m) or 0, a or 0); lq.setdefault(m, l)
    snaps = collections.defaultdict(list)
    for m, ts, a, l in c.execute("select mint, ts, market_cap, liquidity_usd from price_snapshots order by ts"):
        snaps[m].append((ts, a, l))
    cand = collections.defaultdict(list)
    for r in c.execute("select mint,ts,open,high,low,close,volume_usd,buys,sells,n_snapshots from candles_1m order by ts"):
        cand[r[0]].append(Candle(*r))
    return mc, lq, uni, snaps, cand
def at(snaps, keys, ts):
    i = bisect.bisect_right(keys, ts + 30) - 1
    return snaps[i] if i >= 0 else None
def sim(cs, i, p, lv):
    e = cs[i].close; sl = lv['sl']; tp = lv['tp']; peak = e
    for j in range(i + 1, len(cs)):
        k = cs[j]
        if k.low <= sl: return (sl / e - 1) * 100, 'stop' if sl < e else 'trail'
        if k.high >= tp: return (tp / e - 1) * 100, 'tp'
        peak = max(peak, k.high)
        if p.get('trail_pct') and (peak / e - 1) * 100 >= p['trail_activate_pct']:
            sl = max(sl, peak * (1 - p['trail_pct'] / 100))
        if k.ts - cs[i].ts >= p['time_stop_min'] * 60: return (k.close / e - 1) * 100, 'time'
    return None, 'open'
strats = {n: Strategy(n, 1, d['universes'], d['params']) for n, d in DEFAULT_PARAMS.items()}
allres = collections.defaultdict(list); scores = collections.defaultdict(list); gated = collections.Counter()
for DB in sys.argv[1:]:
    mc, lq, uni, snaps, cand = load(DB)
    for m, cs in cand.items():
        if len(cs) < 12: continue
        u = uni.get(m, 'new'); sn = snaps.get(m, []); keys = [x[0] for x in sn]
        last = {}
        for i in range(10, len(cs) - 1):
            ts = cs[i].ts + 59
            s = at(sn, keys, ts)
            mcap = (s[1] if s and s[1] else (mc.get(m) or 0) * cs[i].close / cs[-1].close)
            liq = (s[2] if s and s[2] is not None else lq.get(m))
            if mcap < G['min_mcap']: continue
            md = MarketData(mint=m, price_usd=cs[i].close, liquidity_usd=liq, market_cap=mcap)
            f = compute_features(cs[:i + 1], md, u, ts)
            det = run_detectors(f, u)
            score, _ = score_signal(det, f, 80)
            for n, st in strats.items():
                if not st.applies(u): continue
                ok, why = st.entry_conditions(u, det, f)
                if not ok or ts - last.get(n, 0) < 1800: continue
                last[n] = ts
                if not liq or liq < G['min_liq'] or liq / mcap < G['min_ratio']:
                    gated[(n, 'liquidity gate')] += 1; continue
                p = st.p(u); lv = st.levels(u, cs[i].close, f)
                scores[n].append(score)
                pn, why = sim(cs, i, p, lv)
                allres[(n, u)].append((pn, why, score))
print("gated (liquidity rules):", dict(gated))
for k, v in sorted(allres.items()):
    pn = [x[0] for x in v if x[0] is not None]
    if not pn: print(k, "no closed outcomes", len(v)); continue
    print(f"{k[0]:20} {k[1]:7} n={len(pn):3} win={sum(x>0 for x in pn)/len(pn)*100:5.1f}% avg={statistics.fmean(pn):6.2f}% "
          f"exits={dict(collections.Counter(x[1] for x in v))}")
    for thr in (50, 55, 60, 65):
        q = [x[0] for x in v if x[0] is not None and x[2] >= thr]
        if q: print(f"     score>={thr}: n={len(q)} win={sum(x>0 for x in q)/len(q)*100:.0f}% avg={statistics.fmean(q):.2f}%")
for n, sc in scores.items(): print(n, "score quartiles", [round(sorted(sc)[int(q*(len(sc)-1))]) for q in (0, .25, .5, .75, 1)])
