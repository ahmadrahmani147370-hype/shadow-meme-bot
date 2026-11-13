# SHADOW MEME v1.1 - why the win rate was low, and what the data supports

Data: the live box database since 14:30 PKT on 13 Nov 2026 plus every earlier recorded run (phase 6/7 live
tests and the phase 10 fresh-clone run). Scripts: `tools/analysis/` (they read the box databases read-only).
All numbers below are measured from those files; sample sizes are given because they are small.

## 1. What happened (before v1.1)

| Set | Trades | Wins | Win rate |
|---|---|---|---|
| Live box session (14:30-17:47 PKT) | 6 | 1 | 17% |
| All recorded paper trades | 20 | 5 | 25% |
| All recorded BUY WATCH outcomes (virtual) | 54 | 30 | 56% |

The 6 live trades, all in the NEW universe at $11k-53k market cap:

| Coin | Exit | P&L |
|---|---|---|
| SolanaOS | stop loss (SL -13.2%, filled -16%: gap) | -16.0% |
| SolanaOS | take profit | +48.8% |
| Intel AI | liquidity gone (rug) | -100% |
| gone | stop loss, at resume after a 494 s box suspension | -36.7% |
| MORNING | liquidity gone (rug), same resume | -100% |
| Frgavel | liquidity gone - rugged during a 55 min box suspension | -100% |

## 2. Root causes

1. **Rugs on tiny new launches.** By exit reason (all runs): `liquidity_gone` 9 trades, average -67.9%; stop
   loss 10, average -44%. In the market snapshots 73 coins went from >= $20k liquidity to < $1k, and 69 of
   them did it **in one step** (the LP was pulled at once), so no exit rule can get out in time. Rug rate by
   peak market cap: < $100k 67%, $100-300k 58%.
2. **Weak entries.** Watches with score < 70 won 46%; 70-75 61%; 75-80 71%. Liquidity < $20k won 25%.
   Entries 20-40 min after launch won 17% (10-20 min: 68%).
3. **Box suspensions.** The box paused for 494 s, 3317 s, 3089 s, ... (watchdog log). Positions cannot be
   managed while the machine is paused; two losses closed at a resume. Nothing in the bot can prevent this.
4. **Not the cause:** fees are $0, exit slippage was 0.5-3%. Trailing / breakeven exit variants replayed on
   the 50 new-launch watch paths did not help (average -3% to -4.6% vs -2.2% now): the rugs dominate.
5. The judge separated poorly on this data (APPROVE 60% win vs VETO 53%).

## 3. Evidence for each v1.1 rule

* **Market cap >= $1M:** blocks all 20 recorded paper trades and all 54 watches (every one was < $1M),
  including all 9 rug losses - and also the 5 winners. ">= $1M" alone is not enough: 7 of 10 coins that
  *showed* >= $1M rugged, mostly fake market caps (dust prices after a drain, or fake-supply launches at
  $50-72M "mcap" on ~$100k pools).
* **Liquidity/mcap >= 2% and pool >= $50k:** of the rugged >= $1M coins with a safety report, safety rejected
  3 of 5; the other 2 had liquidity/mcap 0.76% and 1.2%. The 2% rule blocks all of them and keeps 70-77% of
  the legitimate MCAP / TOP VOLUME universe coins.
* **Age >= 10 min:** in line with the age buckets above; the >= $1M sample is too small to prove it (n=23).
* **early_entry min score 70:** from the score buckets above.
* **momentum_breakout (new):** accurate replay with the shipped code on point-in-time snapshots of the
  >= $1M coins: TOP VOLUME universe n=10 closed setups, 50% win, average -2.16%; with the MCAP parameters n=8,
  75% win, average +1.31%. **No reliable edge yet** (n = 8-10); higher scores did not do better in this tiny
  sample. It is a starting point; shadow tracking + the learner decide from here.

## 4. After v1.1 - what to expect

* Far fewer trades: in 3 hours only 27 coins were >= $1M (14 of them new launches crossing $1M), and the
  MCAP universe produced no signal in ~3 h. Days with 0 BUY messages are normal.
* The before/after win rate on *recorded* trades cannot be compared like for like: the new rules would
  have taken none of the old trades. The honest "after" number will come from the live bot over the next
  days (`/report`, `/strategies`, dashboard).

## 5. Price refresh (measured)

* Live bot, first 5 min: 300 DexScreener fast calls, 0 errors, 0 HTTP 429; refresh interval p50 1.00 s,
  p95 1.01 s per hot coin; tick incl. exit checks p50 15 ms.
* Standalone 3-minute run of the same code on 16 coins >= $1M: 180 calls, 2864 refreshes, p50 1.00 s, p95
  1.01 s, 0 errors. **But DexScreener's price for a coin changed only every ~31 s (p50)**; Jupiter price v3
  (polled every 3 s) changed 13.8 times per coin in 3 min vs 2.4 for DexScreener. Hence the Jupiter sub-lane
  (every 5 s for open positions/watches - keyless Jupiter allows 30 requests/min shared with sell quotes,
  so 1 s Jupiter polling is not possible for free).
