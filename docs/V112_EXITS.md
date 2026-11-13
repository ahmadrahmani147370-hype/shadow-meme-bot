# v1.1.2 - let winners run, protect the profit (13 Nov 2026)

Ahmed: *"Entry lo aur bas jo profit me chale chalne do 1000% / profit rakho"* - once a trade is in profit, let it
run up to +1000% and protect the profit. All strategies, paper trades, virtual watch outcomes and the
self-learning replay use the same rules (`shadow/exits.py`).

## Exact exit rules (EXIT_POLICY=run, the default)

Checked every price tick (~1 s for open positions), in this order:

1. **Rug guard** (unchanged): pool liquidity drained -> `liquidity_gone` exit at the last trusted price.
2. **Stop**:
   * not yet protected (peak < +15%): the strategy's stop loss, as before -> `stop_loss`
   * protected (peak has reached **+15%**): stop = max(strategy SL, entry **+2%**, peak - trail) where the trail
     widens with the peak gain: **10%** below +50%, **15%** from +50 to +100%, **25%** from +100 to +300%,
     **30%** from +300%. -> `trailing_stop` (or `breakeven_stop` if the +2% floor is the higher one; with
     these tiers the trail is always higher, so the floor is only a safety net)
3. **Take profit at +1000%** (11x the entry) -> `take_profit`. No low TP caps any more.
4. **Partial profit**: once, at **+30%**, **50%** of the position is sold (Telegram: "🟡 SELL 50% (partial
   profit)"); the other half keeps running with the protected stop. Cash comes in at once; the P&L of the
   whole position is booked when the rest closes.
5. **Time stop only while NOT protected** (strategy `time_stop_min`, a flat trade frees its slot). Protected
   trades: **max hold 24 h** -> `max_hold`.
6. Pattern exits (dump / exhaustion SELL signals) only while not protected; for winners the trailing stop
   decides.

Self-learning: `tp_pct` is no longer tunable in run mode and its limit is 1000..100000, so learning can never
pull the TP back below +1000%. Live strategy versions were migrated once (`migrate_v112`: new version with
`tp_pct=1000`, every other learned param kept, recorded in `/strategies`).

SELL messages now show **Peak reached: +X%**, any partial sold earlier, and the total P&L of both sells.

Config (`.env`): `EXIT_POLICY` (run|classic), `EXIT_TP_PCT=1000`, `EXIT_BE_TRIGGER_PCT=15`, `EXIT_BE_LOCK_PCT=2`,
`EXIT_TRAIL_TIERS=50:10,100:15,300:25,inf:30`, `EXIT_PARTIAL_PCT=30`, `EXIT_PARTIAL_FRAC=0.5` (0 = off),
`EXIT_MAX_HOLD_H=24`. **Rollback:** `EXIT_POLICY=classic` restores v1.1.1 exits (per-strategy TP / trail / time).

## Replay evidence (honest)

`python3 tools/analysis/replay_v112.py data/*.db` - every setup the shipped entry logic finds on the recorded
1-minute candles of all runs (119 setups), exits simulated candle by candle (stops filled at min(stop, open),
rug = snapshot liquidity < $1k -> -95%). Positions still open when the data ends are marked at the last close.
Full output: `docs/V112_REPLAY_OUTPUT.txt`.

| Exit policy | win | avg / trade | sum | best |
|---|---|---|---|---|
| v1.1.1 classic (per-strategy TP 10-35%, trail, time stop) | 56.3% | **-0.74%** | -88 | +30% |
| Ahmed's example: BE@+25%, trail 25/30/35% | 39.5% | -7.58% | -902 | +131% |
| BE@+15%, trail 15/25/30% | 44.5% | -4.38% | -521 | +131% |
| BE@+15%, trail 10/15/25/30% (no partial) | 47.9% | -2.87% | -342 | +131% |
| 50%@+30% then BE, trail 25/30/35% | 40.3% | -4.19% | -498 | +81% |
| 50%@+100% then BE, trail 25/30/35% | 42.9% | -4.29% | -510 | +116% |
| **chosen: 50%@+30%, BE@+15%, trail 10/15/25/30%** | 47.9% | **-1.59%** | -189 | +81% |

What this means:
* **No recorded setup went above about +145%.** The recordings are a few hours long, so the +1000% upside
  cannot be seen in this data at all. The table cannot show what a real 10x runner would add.
* On the recorded data, **letting winners run is worse than the old take-profit levels**: coins that touched the
  old TP often dumped or rugged later, and wide trails give back most of a small pump. Narrow first tiers
  (10% trail below +50%) plus banking half at +30% recover most of that loss. That is why this variant was
  chosen over the example 25/30/35% tiers.
* The >=$1M + liquidity-gate subset (what the bot actually trades) has only **7 setups**: classic +1.39% vs
  let-it-run +0.38%. That is too few to conclude anything.
* It is implemented because Ahmed asked for it, using the best-replaying let-it-run variant. Live paper results
  will show whether real runners make up for it; `EXIT_POLICY=classic` is a one-line rollback.
