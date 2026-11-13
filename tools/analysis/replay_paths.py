import sys,json,statistics,collections
sys.argv=['x']; exec(open('/workspace/analysis/an11.py').read().split('print("=== WATCH')[0])
W=[r for r in S if r['path_json'] and r['entry'] and abs(r['pnl_pct'] or 0)<500]
def sim(r,sl,tp,tmin,trail=None,act=None,be=None):
    if r['exit_reason']=='liquidity_gone': return r['pnl_pct'],'rug'
    e=r['entry']; pts=json.loads(r['path_json']); slp=e*(1-sl/100); peak=e
    for t,p in pts:
        if p<=slp: return (p/e-1)*100,'sl/trail'          # fills at the observed price (gap included)
        if p>=e*(1+tp/100): return (p/e-1)*100 if p< e*(1+tp/100)*1.5 else tp,'tp'
        peak=max(peak,p)
        if be is not None and (peak/e-1)*100>=be: slp=max(slp,e*1.002)
        if trail and (peak/e-1)*100>=act: slp=max(slp,peak*(1-trail/100))
        if t>=tmin*60: return (p/e-1)*100,'time'
    return (pts[-1][1]/e-1)*100 if pts else None,'end'
print("watches with paths:",len(W))
for lab,a in [("current SL12 TP30 20m",(12,30,20)),("current + trail10@15",(12,30,20,10,15)),("SL12 TP30 BE@10",(12,30,20,None,None,10)),
              ("SL12 TP30 BE@10 trail8@15",(12,30,20,8,15,10)),("SL8 TP20",(8,20,20)),("SL15 TP30",(15,30,20)),("SL12 TP30 30m trail8@15 BE@10",(12,30,30,8,15,10))]:
    res=[sim(r,*a) for r in W]; pn=[p for p,_ in res if p is not None]
    print(f"{lab:32} n={len(pn)} win={sum(p>0 for p in pn)/len(pn)*100:5.1f}% avg={statistics.fmean(pn):6.2f}% med={statistics.median(pn):6.2f}% {dict(collections.Counter(x for _,x in res))}")
print("\nfiner entry buckets (watch outcomes):")
def tb(nm,key):
    g=collections.defaultdict(list)
    for r in W: g[key(r)].append(r['pnl_pct'])
    for k in sorted(g,key=str): v=g[k]; print(f"  {nm} {str(k):12} n={len(v):2} win={sum(x>0 for x in v)/len(v)*100:5.1f}% med={statistics.median(v):6.1f}")
tb("age",lambda r:bucket(feat(r,'age_min'),[3,5,10,20],str))
tb("judge+score70",lambda r:(r['judge_verdict'],(r['score'] or 0)>=70))
tb("rug by liq",lambda r:(bucket(r['liquidity'],[20e3,40e3],K), r['exit_reason']=='liquidity_gone'))
