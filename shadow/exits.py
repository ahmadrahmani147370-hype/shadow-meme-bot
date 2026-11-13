"""v1.1.2 - "let winners run, protect the profit" exit policy (Ahmed, 13 Nov 2026), shared by the paper engine,
the virtual watch outcomes and the self-learning replay so all three follow the same rules.

EXIT_POLICY=run (default):
  * take profit at +EXIT_TP_PCT (1000%) - no low TP caps
  * stop loss: the strategy's SL until the position is protected (unchanged for losing trades)
  * protected once the peak reaches +EXIT_BE_TRIGGER_PCT (15%): stop >= entry +EXIT_BE_LOCK_PCT (2%,
    covers exit slippage) - a winner can no longer turn into a loss (except a gap / rug)
  * trailing stop from the peak, widening with the gain: EXIT_TRAIL_TIERS "50:10,100:15,300:25,inf:30"
    = trail 10% while the peak gain is < +50%, 15% < +100%, 25% < +300%, 30% above
  * partial profit: EXIT_PARTIAL_FRAC (50%) sold once at +EXIT_PARTIAL_PCT (30%); the rest rides (0 = off)
  * time stop only while NOT protected (a flat trade still frees its slot); protected trades: max
    EXIT_MAX_HOLD_H (24 h)
  * pattern exits (dump / exhaustion SELL signals) only while not protected - the trailing stop handles winners
  * rug guard (drained pool -> liquidity_gone) is unchanged and always first
EXIT_POLICY=classic: v1.1 behaviour (per-strategy TP / trailing / time stop).

Replay (tools/analysis/replay_v112.py, 119 setups from all recorded runs, open positions marked at the last
price): classic avg -0.74%/trade (56% win); this policy avg -1.59% (48% win, best let-it-run variant);
BE@25 + trail 25/30/35 without partial avg -7.6% (40% win). In practice the 10% trail sits above the +2%
breakeven floor as soon as the trade is protected, so protected exits show as trailing_stop. No recorded setup peaked above +145% (the data horizon is a few hours), so
the 1000% upside could not be observed. See docs/V112_EXITS.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def parse_tiers(text: str) -> list[tuple[float, float]]:
    out = []
    for part in (text or "").replace(" ", "").split(","):
        if ":" not in part:
            continue
        lim, tr = part.split(":", 1)
        try:
            out.append((float("inf") if lim.lower() in ("inf", "max", "") else float(lim), float(tr)))
        except ValueError:
            continue
    out.sort()
    return out or [(float("inf"), 30.0)]


@dataclass
class ExitPolicy:
    mode: str = "run"
    tp_pct: float = 1000.0
    be_trigger_pct: float = 15.0
    be_lock_pct: float = 2.0
    tiers: list = field(default_factory=lambda: parse_tiers("50:10,100:15,300:25,inf:30"))
    partial_pct: float = 30.0
    partial_frac: float = 0.5
    max_hold_h: float = 24.0

    @classmethod
    def from_settings(cls, s) -> "ExitPolicy":
        return cls(mode=getattr(s, "exit_policy", "run"), tp_pct=getattr(s, "exit_tp_pct", 1000.0),
                   be_trigger_pct=getattr(s, "exit_be_trigger_pct", 15.0), be_lock_pct=getattr(s, "exit_be_lock_pct", 2.0),
                   tiers=parse_tiers(getattr(s, "exit_trail_tiers", "50:10,100:15,300:25,inf:30")),
                   partial_pct=getattr(s, "exit_partial_pct", 30.0), partial_frac=getattr(s, "exit_partial_frac", 0.5),
                   max_hold_h=getattr(s, "exit_max_hold_h", 24.0))

    @property
    def run(self) -> bool:
        return self.mode == "run"

    def trail_for(self, peak_gain_pct: float) -> float:
        for lim, tr in self.tiers:
            if peak_gain_pct < lim:
                return tr
        return self.tiers[-1][1]

    def protected(self, entry: float, peak: float) -> bool:
        return bool(entry) and (peak / entry - 1) * 100 >= self.be_trigger_pct - 1e-9   # float-safe

    def stop(self, entry: float, peak: float, sl: float | None) -> tuple[float, str]:
        """Effective stop price and what it is: 'sl' / 'breakeven' / 'trail'."""
        stop, kind = (sl or 0.0), "sl"
        if not entry or not self.protected(entry, peak):
            return stop, kind
        be = entry * (1 + self.be_lock_pct / 100)
        if be > stop:
            stop, kind = be, "breakeven"
        tr = peak * (1 - self.trail_for((peak / entry - 1) * 100) / 100)
        if tr > stop:
            stop, kind = tr, "trail"
        return stop, kind

    def summary(self) -> str:
        """One line for Telegram / README: the exact exit rules in force."""
        if not self.run:
            return "classic exits (per-strategy TP / trail / time stop)"
        tiers, lo, parts = self.tiers, 0.0, []
        for lim, tr in tiers:
            parts.append(f"{tr:g}% (peak {'≥' if lim == float('inf') else '<'}+{(lo if lim == float('inf') else lim):g}%)")
            lo = lim
        s = (f"TP +{self.tp_pct:g}% | SL kept | after +{self.be_trigger_pct:g}%: stop ≥ entry+{self.be_lock_pct:g}% "
             f"& trail " + ", ".join(parts))
        if self.partial_frac > 0:
            s += f" | sell {self.partial_frac * 100:.0f}% at +{self.partial_pct:g}%"
        return s + f" | max hold {self.max_hold_h:g}h"

    def tp_price(self, entry: float) -> float:
        return entry * (1 + self.tp_pct / 100)

    def decide(self, entry: float, price: float, peak: float, sl: float | None, tp: float | None, held_s: float,
               time_stop_min: float | None, partial_done: bool) -> str | None:
        """'stop_loss' / 'breakeven_stop' / 'trailing_stop' / 'take_profit' / 'partial' / 'time_stop' /
        'max_hold' / None. `peak` must already include `price`."""
        stop, kind = self.stop(entry, peak, sl)
        if stop and price <= stop:
            return {"sl": "stop_loss", "breakeven": "breakeven_stop", "trail": "trailing_stop"}[kind]
        if price >= max(tp or 0.0, self.tp_price(entry)):
            return "take_profit"
        if not partial_done and self.partial_frac > 0 and price >= entry * (1 + self.partial_pct / 100):
            return "partial"
        if self.protected(entry, peak):
            if self.max_hold_h and held_s >= self.max_hold_h * 3600:
                return "max_hold"
        elif time_stop_min and held_s >= time_stop_min * 60:
            return "time_stop"
        return None

    def replay(self, entry: float, pts: list, sl: float, time_stop_min: float) -> tuple[float, str] | None:
        """Replay a recorded path [(t_seconds_since_entry, price), ...]: (gross % incl. the partial, reason).
        None when the path ends before the trade is decided."""
        peak, banked, frac = entry, 0.0, 1.0
        for t, price in pts:
            peak = max(peak, price)
            r = self.decide(entry, price, peak, sl, None, t, time_stop_min, frac < 1.0)
            if r == "partial":
                banked, frac = self.partial_frac * (price / entry - 1) * 100, 1 - self.partial_frac
                continue
            if r:
                return banked + frac * (price / entry - 1) * 100, r
        return None


__all__ = ["ExitPolicy", "parse_tiers"]
