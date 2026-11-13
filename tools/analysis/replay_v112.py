"""v1.1.2 exit-policy replay: every strategy setup found by the shipped entry logic on recorded 1m candles
(all DBs), exits simulated candle by candle until the recorded data ends.
 - stop touched (low <= stop) -> filled at min(stop, candle open)  (gaps below the stop are honoured)
 - take profit touched -> filled at TP
 - rug: snapshot liquidity < $1k after entry -> -95%
 - data ends while still open -> marked at the last close and reported separately ("open@end")"""
import sqlite3, sys, collections, statistics, bisect
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from shadow.models import Candle, MarketData
from shadow.signals import compute_features, run_detectors, score_signal
from shadow.strategies import DEFAULT_PARAMS, Strategy

DBS = sys.argv[1:]          # usage: python3 tools/analysis/replay_v112.py data/shadow.db [more.db ...]
# v1.1 / v1.1.1 take-profit levels (DEFAULT_PARAMS now holds 1000%) - the "classic" baseline uses these
OLD_TP = {'early_entry': {'new': 30.0}, 'first_pullback': {'new': 25.0, 'mcap': 10.0, 'volume': 15.0},
          'migration_breakout': {'new': 35.0, 'mcap': 20.0, 'volume': 25.0},
          'momentum_breakout': {'new': 15.0, 'mcap': 10.0, 'volume': 12.0}}
def load(DB):
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    uni = {m: u for m, u in c.execute("select mint, universe from universe_members")}
    snaps = collections.defaultdict(list)
    for m, ts, a, l in c.execute("select mint, ts, market_cap, liquidity_usd from price_snapshots order by ts"):
        snaps[m].append((ts, a, l))
    cand = collections.defaultdict(list)
    for r in c.execute("select mint,ts,open,high,low,close,volume_usd,buys,sells,n_snapshots from candles_1m order by ts"):
        cand[r[0]].append(Candle(*r))
    return uni, snaps, cand

def at(sn, keys, ts):
    i = bisect.bisect_right(keys, ts + 30) - 1
    return sn[i] if i >= 0 else None

def tier_trail(gain, tiers):
    for lim, tr in tiers:
        if gain < lim: return tr
    return tiers[-1][1]

def sim(cs, i, sn, keys, sl, pol, p):
    r = _sim(cs, i, sn, keys, sl, pol, p)
    return r

def _sim(cs, i, sn, keys, sl, pol, p):
    e = cs[i].close; peak = e; t0 = cs[i].ts
    part = pol.get('partial') if pol else None      # (gain %, fraction sold)
    banked, frac = 0.0, 1.0
    stop = sl
    tp = e * (1 + (pol['tp'] if pol else p['tp_pct']) / 100)
    for j in range(i + 1, len(cs)):
        k = cs[j]
        s = at(sn, keys, k.ts + 59)
        if s and s[2] is not None and s[2] < 1000: return banked + frac * -95.0, 'rug', peak
        if k.low <= stop:
            fill = min(stop, k.open)
            why = 'stop_loss' if stop <= sl else ('breakeven' if pol and abs(stop - e * (1 + pol['be_lock'] / 100)) < 1e-12 else 'trail')
            return banked + frac * (fill / e - 1) * 100, why, peak
        if part and frac == 1.0 and k.high >= e * (1 + part[0] / 100):
            banked, frac = part[1] * part[0], 1 - part[1]
            stop = max(stop, e * (1 + pol['be_lock'] / 100))
        if k.high >= tp: return banked + frac * (tp / e - 1) * 100, 'take_profit', max(peak, tp)
        peak = max(peak, k.high); g = (peak / e - 1) * 100
        if pol:
            prot = pol['be'] is not None and g >= pol['be']
            if prot: stop = max(stop, e * (1 + pol['be_lock'] / 100))
            if g >= pol['trail_from']: stop = max(stop, peak * (1 - tier_trail(g, pol['tiers']) / 100))
            held = k.ts - t0
            if not prot and g < pol['trail_from'] and held >= p['time_stop_min'] * 60: return banked + frac * (k.close / e - 1) * 100, 'time', peak
            if held >= pol['max_hold_h'] * 3600: return banked + frac * (k.close / e - 1) * 100, 'max_hold', peak
        else:
            if p.get('trail_pct') and g >= p['trail_activate_pct']: stop = max(stop, peak * (1 - p['trail_pct'] / 100))
            if k.ts - t0 >= p['time_stop_min'] * 60: return banked + frac * (k.close / e - 1) * 100, 'time', peak
    return banked + frac * (cs[-1].close / e - 1) * 100, 'open@end', peak

T = lambda a, b, c: [(100, a), (300, b), (1e18, c)]
T4 = lambda a, b, c, d: [(50, a), (100, b), (300, c), (1e18, d)]
POL = {
 "v1.1 current (per-strategy TP/trail/time)": None,
 "TP1000 BE@25(+2) trail 25/30/35": dict(tp=1000, be=25, be_lock=2, trail_from=25, tiers=T(25, 30, 35), max_hold_h=24),
 "TP1000 BE@20(+2) trail 25/30/35": dict(tp=1000, be=20, be_lock=2, trail_from=20, tiers=T(25, 30, 35), max_hold_h=24),
 "TP1000 BE@30(+2) trail 25/30/35": dict(tp=1000, be=30, be_lock=2, trail_from=30, tiers=T(25, 30, 35), max_hold_h=24),
 "TP1000 BE@25(+2) trail 20/25/30": dict(tp=1000, be=25, be_lock=2, trail_from=25, tiers=T(20, 25, 30), max_hold_h=24),
 "TP1000 BE@25(+2) trail 30/35/40": dict(tp=1000, be=25, be_lock=2, trail_from=25, tiers=T(30, 35, 40), max_hold_h=24),
 "TP1000 BE@25(+2) trail 15/20/25": dict(tp=1000, be=25, be_lock=2, trail_from=25, tiers=T(15, 20, 25), max_hold_h=24),
 "TP1000 BE@15(+2) trail 15/25/30": dict(tp=1000, be=15, be_lock=2, trail_from=15, tiers=T(15, 25, 30), max_hold_h=24),
 "TP1000 no BE, trail 25/30/35 from +25": dict(tp=1000, be=None, be_lock=0, trail_from=25, tiers=T(25, 30, 35), max_hold_h=24),
 "TP1000 50%@+30 then BE, trail 25/30/35": dict(tp=1000, be=25, be_lock=2, trail_from=25, tiers=T(25, 30, 35), max_hold_h=24, partial=(30, .5)),
 "TP1000 50%@+100 then BE, trail 25/30/35": dict(tp=1000, be=25, be_lock=2, trail_from=25, tiers=T(25, 30, 35), max_hold_h=24, partial=(100, .5)),
 "TP1000 BE@15 trail 10/15/25/30 (tiers 50/100/300)": dict(tp=1000, be=15, be_lock=2, trail_from=15, tiers=T4(10, 15, 25, 30), max_hold_h=24),
 "TP1000 50%@+30, BE@15, trail 10/15/25/30": dict(tp=1000, be=15, be_lock=2, trail_from=15, tiers=T4(10, 15, 25, 30), max_hold_h=24, partial=(30, .5)),
 "TP1000 50%@+30, BE@15, trail 15/20/25/30": dict(tp=1000, be=15, be_lock=2, trail_from=15, tiers=T4(15, 20, 25, 30), max_hold_h=24, partial=(30, .5)),
 "TP1000 50%@+30, BE@15, trail 15/25/30/35": dict(tp=1000, be=15, be_lock=2, trail_from=15, tiers=T4(15, 25, 30, 35), max_hold_h=24, partial=(30, .5)),
}
strats = {n: Strategy(n, 1, d['universes'], d['params']) for n, d in DEFAULT_PARAMS.items()}
setups = []        # (group, cs, i, sn, keys, sl, params)
for DB in DBS:
    uni, snaps, cand = load(DB)
    for m, cs in cand.items():
        if len(cs) < 12: continue
        u = uni.get(m, 'new'); sn = snaps.get(m, []); keys = [x[0] for x in sn]; last = {}
        for i in range(10, len(cs) - 1):
            ts = cs[i].ts + 59; s = at(sn, keys, ts)
            mcap = s[1] if s and s[1] else 0; liq = s[2] if s else None
            md = MarketData(mint=m, price_usd=cs[i].close, liquidity_usd=liq, market_cap=mcap)
            f = compute_features(cs[:i + 1], md, u, ts); det = run_detectors(f, u); score, _ = score_signal(det, f, 80)
            for n, st in strats.items():
                if not st.applies(u): continue
                ok, _ = st.entry_conditions(u, det, f)
                if not ok or ts - last.get(n, 0) < 1800: continue
                p = {**st.p(u), 'tp_pct': OLD_TP[n][u]}
                if score < p['min_score']: continue
                last[n] = ts
                lv = st.levels(u, cs[i].close, f)
                big = mcap >= 1e6 and liq and liq >= 50e3 and liq / mcap >= 0.02
                setups.append(("ALL", cs, i, sn, keys, lv['sl'], p, n))
                if big: setups.append((">=1M+gates", cs, i, sn, keys, lv['sl'], p, n))
for grp in ("ALL", ">=1M+gates"):
    ss = [x for x in setups if x[0] == grp]
    print(f"\n=== {grp}: {len(ss)} setups (strategies {dict(collections.Counter(x[7] for x in ss))})")
    for lab, pol in POL.items():
        res = [sim(cs, i, sn, keys, sl, pol, p) for _, cs, i, sn, keys, sl, p, _ in ss]
        closed = [r for r in res if r[1] != 'open@end']; pn = [r[0] for r in closed]
        if not pn: print(lab, "no closed"); continue
        peaks = [(r[2] / 1) for r in res]
        al = [r[0] for r in res]; op = [r[0] for r in res if r[1] == 'open@end']
        print(f"{lab:46} ALL(open marked) n={len(al)} win={sum(x > 0 for x in al) / len(al) * 100:5.1f}% avg={statistics.fmean(al):7.2f}% "
              f"sum={sum(al):8.1f} | open@end n={len(op)} avg={statistics.fmean(op) if op else 0:6.1f}% | closed n={len(pn)} win={sum(x > 0 for x in pn) / len(pn) * 100:4.1f}% avg={statistics.fmean(pn):6.2f}% best={max(al):6.1f}%")
        continue
        print(f"{lab:42} closed n={len(pn):3} win={sum(x > 0 for x in pn) / len(pn) * 100:5.1f}% avg={statistics.fmean(pn):7.2f}% "
              f"med={statistics.median(pn):6.2f}% sum={sum(pn):8.1f} best={max(pn):7.1f}% open@end={len(res) - len(closed)} "
              f"exits={dict(collections.Counter(r[1] for r in closed))}")
