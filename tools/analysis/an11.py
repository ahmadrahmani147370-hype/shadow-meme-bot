import sqlite3,json,collections,sys
DBS={'live':'data/shadow.db','p6':'data/live_p6.db','p7':'data/live_p7.db','p7e':'data/live_p7e.db','p10':'/tmp/fresh/data/shadow.db'}
def rows(sql):
    out=[]
    for k,f in DBS.items():
        c=sqlite3.connect(f'file:{f}?mode=ro',uri=True); c.row_factory=sqlite3.Row
        for r in c.execute(sql): d=dict(r); d['db']=k; out.append(d)
    return out
S=rows("select * from signals where alert_type='BUY_WATCH' and status='closed'")
P=rows("select p.*, s.mcap s_mcap, s.liquidity s_liq, s.features_json, s.judge_verdict jv from paper_positions p left join signals s on s.id=p.signal_id where p.status='closed'")
def feat(r,k):
    try: return json.loads(r.get('features_json') or '{}').get(k)
    except: return None
def bucket(v,edges,fmt):
    if v is None: return 'n/a'
    for e in edges:
        if v<e: return f"<{fmt(e)}"
    return f">={fmt(edges[-1])}"
K=lambda x:f"{x/1e3:.0f}k" if x<1e6 else f"{x/1e6:.0f}M"
def table(name,data,key,pnl='pnl_pct'):
    g=collections.defaultdict(list)
    for r in data: g[key(r)].append(r[pnl] or 0)
    print(f"\n{name}")
    for k in sorted(g,key=str):
        v=g[k]; w=sum(x>0 for x in v)
        print(f"  {str(k):22} n={len(v):3} win={w/len(v)*100:5.1f}% avg={sum(v)/len(v):7.1f}% med={sorted(v)[len(v)//2]:7.1f}%")
print("=== WATCH outcomes (every BUY_WATCH signal, virtual, before fees) n=",len(S))
for nm,f in [("strategy",lambda r:r['strategy']),("universe",lambda r:r['universe']),("exit",lambda r:r['exit_reason']),
             ("judge",lambda r:r['judge_verdict']),("score",lambda r:bucket(r['score'],[65,70,75,80],str)),
             ("mcap",lambda r:bucket(r['mcap'],[50e3,100e3,250e3,1e6,5e6],K)),("liq",lambda r:bucket(r['liquidity'],[20e3,50e3,100e3,250e3],K)),
             ("age_min",lambda r:bucket(feat(r,'age_min'),[10,20,40,60,120],str)),("db",lambda r:r['db'])]:
    table(nm,S,f)
print("\n=== PAPER trades (real paper fills incl. fees/slippage) n=",len(P))
for nm,f in [("strategy",lambda r:r['strategy']),("universe",lambda r:r['universe']),("exit",lambda r:r['exit_reason']),
             ("mcap",lambda r:bucket(r['s_mcap'],[50e3,100e3,250e3,1e6,5e6],K)),("liq",lambda r:bucket(r['s_liq'],[20e3,50e3,100e3,250e3],K)),
             ("age_min",lambda r:bucket(feat(r,'age_min'),[10,20,40,60,120],str)),("db",lambda r:r['db'])]:
    table(nm,P,f)
print("\nslippage entry/exit avg:", round(sum(r['entry_slippage_pct'] or 0 for r in P)/len(P),2), round(sum(r['exit_slippage_pct'] or 0 for r in P)/len(P),2))
print("max_up of losers:", sorted(round(r['max_up_pct'] or 0,1) for r in P if (r['pnl_pct'] or 0)<=0))
print("watch max_up of losers:", sorted(round(r['max_up_pct'] or 0,1) for r in S if (r['pnl_pct'] or 0)<=0))
