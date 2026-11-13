"""Price sanity for exits (phase 7 fix, found in the phase 7 live run).

When a pool is drained (rug: LP pulled), DexScreener keeps reporting a "price" computed from the dust
left in the pool. Live example 13 Nov 2026, BTCCNDL: liquidity $112k -> $0.11 -> $9.93 and price
6.8e-05 -> 95.27 USD (+150,000,000%). Nobody can sell into that. Rules used by the paper engine and by
the signal engine's virtual outcomes (judge accuracy, learning paths):
- DexScreener liquidity below PAPER_RUG_LIQUIDITY_USD -> treated as a rug and closed: valued at the last
  trusted price before the drain (never the dust price), sold into what is left of the pool (constant
  product on the remaining quote side, so a $10 pool returns ~$1.4 for a $2 position);
- a price that jumps more than PAPER_MAX_PRICE_JUMP x from the last trusted price in one update is
  ignored (not used for TP / SL / trailing / paths).
"""
from __future__ import annotations


def drained(liquidity_usd: float | None, floor_usd: float, source: str | None = "dexscreener") -> bool:
    """Only pool liquidity reported by DexScreener counts: Jupiter's fill-in for bonding-curve coins is an
    estimate (~$100 typical) and would trigger false rug exits."""
    if source not in (None, "", "dexscreener"):
        return False
    return liquidity_usd is not None and liquidity_usd < floor_usd


def implausible_jump(prev: float | None, price: float | None, max_jump: float) -> bool:
    if not prev or not price or prev <= 0:
        return False
    r = price / prev
    return r > max_jump or r < 1.0 / (max_jump * 1000)


def capped_value(value_usd: float, liquidity_usd: float | None) -> float:
    """A sell can't pull out more than about half of what is in the pool."""
    if liquidity_usd is None:
        return value_usd
    return max(min(value_usd, 0.5 * liquidity_usd), 0.0)


def amm_out(value_usd: float, liquidity_usd: float | None) -> float:
    """What selling `value_usd` (at the last trusted price) into a constant-product pool with liquidity L
    returns: quote reserve Q = L/2, out = Q*V/(Q+V). Used for drained pools, where the reported price is dust
    math and only the remaining quote side can be pulled out."""
    if liquidity_usd is None:
        return value_usd
    q = max(liquidity_usd, 0.0) / 2
    return q * value_usd / (q + value_usd) if q + value_usd > 0 else 0.0


def rug_pnl_pct(entry: float, last_trusted: float, notional_usd: float, liquidity_usd: float | None) -> float:
    """Virtual outcome of a drained pool for a notional trade size (watches have no size). Conservative:
    valued at the last trusted price before the drain (never at the drained pool's garbage price), and at
    most half the remaining liquidity."""
    value = amm_out(notional_usd * last_trusted / entry, liquidity_usd)
    return (value / notional_usd - 1) * 100


__all__ = ["drained", "implausible_jump", "capped_value", "amm_out", "rug_pnl_pct"]
