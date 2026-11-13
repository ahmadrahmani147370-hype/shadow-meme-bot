import sqlite3,collections,sys,itertools
DBS=sys.argv[1:] or ['data/shadow.db']
res=collections.defaultdict(lambda:[0,0])
for f in DBS:
    c=sqlite3.connect(f'file:{f}?mode=ro',uri=True)
    created={}
    for m,a,b in c.execute("select mint,pair_created_ts,launch_ts from token_market"): created[m]=a or b
    for m,a in c.execute("select mint,first_seen_ts from seen_tokens"): created.setdefault(m,a)
    d=collections.defaultdict(list)
    for m,ts,mc,l in c.execute("select mint,ts,market_cap,liquidity_usd from price_snapshots where source='dexscreener' order by ts"): d[m].append((ts,mc or 0,l or 0))
    for (minliq,ratio,age) in itertools.product([20e3,50e3,100e3],[0,0.02,0.05],[0,10,30,60]):
        for m,v in d.items():
            q=None; peakl=0
            for i,(ts,mc,l) in enumerate(v):
                cr=created.get(m)
                if q is None and mc>=1e6 and l>=minliq and l/mc>=ratio and (age==0 or (cr and ts-cr>=age*60)):
                    q=i
                    break
            if q is None: continue
            # rugged after qualifying: liquidity < 1k or < 10% of liq at qualification
            l0=v[q][2]; rug=any(x[2]<max(1000,0.1*l0) for x in v[q+1:])
            res[(minliq,ratio,age)][0]+=1; res[(minliq,ratio,age)][1]+=rug
for k in sorted(res):
    n,r=res[k]; print(f"liq>={k[0]/1e3:.0f}k liq/mcap>={k[1]:.2f} age>={k[2]:2}m : coins={n:3} rugged={r:3} ({r/n*100 if n else 0:4.0f}%)")
