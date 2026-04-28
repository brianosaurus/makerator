"""Signal-driven quoting decisions for makerator.

Pure logic — takes signals + market state + inventory, returns quote intents.
No on-chain calls, no I/O. The runner translates intents into BatchUpdate
ix calls.

v1 scope (per locked decisions):
  - Manifest LST/SOL markets only (5 markets)
  - 2-token baskets only (memory: 3+ are unprofitable)
  - PostOnly orders at the touch (or 0.5 bp inside on strong signals)
  - Quote only when |z| > entry_zscore — most ticks produce zero intents

The cointegration sign convention (from signals.py canonicalization):
  hedge_ratios[0] = +1.0, hedge_ratios[1] = -hr  (always for 2-token baskets)
  spread = log(P0) - hr·log(P1)
  ENTRY_LONG  (z < -thresh): spread below mean → P0 underpriced vs P1
                             → BUY P0, SELL P1
  ENTRY_SHORT (z > +thresh): spread above mean → P0 overpriced vs P1
                             → SELL P0, BUY P1
"""
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

from solders.pubkey import Pubkey

from constants import (
    JUPSOL_MINT, JITOSOL_MINT, VSOL_MINT, DSOL_MINT, BONKSOL_MINT,
)
from signals import Signal, SignalType


# The 5 LST/SOL Manifest markets that v1 trades on.
# Addresses verified live on 2026-04-26.
MANIFEST_LST_SOL_MARKETS: Dict[str, Pubkey] = {
    JUPSOL_MINT:  Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv"),
    JITOSOL_MINT: Pubkey.from_string("7ecvmhGKVcK4SgxeGQJG6yVwVAhbQxLrBuaMoUmpRZ6i"),
    VSOL_MINT:    Pubkey.from_string("2jieFDtgChGY76j62EfpNCRGKLGwSicRJndWWe4UobXa"),
    DSOL_MINT:    Pubkey.from_string("2eu49YAcLy6BNyi9zoiCnuPotaeufu2HrDuHNWEqeGuT"),
    BONKSOL_MINT: Pubkey.from_string("E6ErbWTKJG5GghniWakNogYCNmw8SkMCmrop94iJT7tg"),
}

LST_SYMBOLS = {
    JUPSOL_MINT: "jupSOL",
    JITOSOL_MINT: "jitoSOL",
    VSOL_MINT: "vSOL",
    DSOL_MINT: "dSOL",
    BONKSOL_MINT: "bonkSOL",
}


@dataclass
class TopOfBook:
    best_bid: Optional[float]   # human price (quote per base, i.e. SOL per LST)
    best_ask: Optional[float]
    best_bid_size: float = 0.0  # base atoms / 10^base_decimals
    best_ask_size: float = 0.0


@dataclass
class QuoteIntent:
    market: Pubkey
    market_label: str            # e.g. "jupSOL/SOL"
    side: Literal["bid", "ask"]
    price: float                 # human-readable
    size_base: float             # human-readable base units
    pair_label: str              # e.g. "jupSOL/jitoSOL z=+2.34"
    z: float
    urgency: Literal["normal", "must_land"] = "normal"
    reason: str = ""


def _signed_quote_sides(sig: Signal) -> Optional[tuple[str, str]]:
    """Return (mint_to_buy, mint_to_sell) for a 2-token entry signal.

    Returns None if the signal isn't a 2-token entry."""
    if sig.basket_size != 2:
        return None
    if sig.signal_type == SignalType.ENTRY_LONG:
        # spread below mean → buy P0, sell P1
        return sig.mints[0], sig.mints[1]
    if sig.signal_type == SignalType.ENTRY_SHORT:
        # spread above mean → sell P0, buy P1
        return sig.mints[1], sig.mints[0]
    return None


def _price_with_inside_offset(
    side: Literal["bid", "ask"],
    tob: TopOfBook,
    inside_bps: float,
) -> Optional[float]:
    """Place at touch ± inside_bps, never crossing the opposite side."""
    if tob.best_bid is None or tob.best_ask is None:
        return None
    if side == "bid":
        price = tob.best_bid * (1 + inside_bps / 1e4)
        # Never cross the ask
        max_safe = tob.best_ask * (1 - 0.01 / 1e4)
        return min(price, max_safe)
    else:
        price = tob.best_ask * (1 - inside_bps / 1e4)
        min_safe = tob.best_bid * (1 + 0.01 / 1e4)
        return max(price, min_safe)


def _inside_bps_for_z(z: float, regime_caution: bool) -> float:
    """How aggressively to quote inside the touch as a function of z.

    Conservative defaults:
      |z| <= 1.5: at touch (0 bp inside) — minimum-risk join
      1.5 < |z| <= 2.5: 0.2 bp inside — modest aggression
      |z| > 2.5: 0.5 bp inside — strong signal, lean in
      regime caution: halve all of the above
    """
    az = abs(z)
    if az <= 1.5:
        base = 0.0
    elif az <= 2.5:
        base = 0.2
    else:
        base = 0.5
    if regime_caution:
        base *= 0.5
    return base


def decide_quotes(
    config,
    signals: List[Signal],
    books: Dict[str, TopOfBook],   # keyed by market pubkey (str)
    inventory_skew_sol: Dict[str, float] = None,  # mint → SOL-eq net position
    regime_caution: bool = False,
) -> List[QuoteIntent]:
    """Translate signals + market state into quote intents.

    A single signal on a 2-LST basket produces up to 2 intents (one per LST,
    each on the relevant Manifest LST/SOL market). If either LST lacks a
    Manifest LST/SOL market, the whole signal is skipped — both sides need
    to fire together for the spread bet to make sense.
    """
    inventory_skew_sol = inventory_skew_sol or {}
    intents: List[QuoteIntent] = []
    quote_size_sol = float(getattr(config, 'quote_size_sol', 0.5))
    entry_z = float(config.entry_zscore)
    max_inv_skew = float(getattr(config, 'max_inventory_skew_sol', 2.0))

    for sig in signals:
        if abs(sig.zscore) < entry_z:
            continue

        sides = _signed_quote_sides(sig)
        if sides is None:
            continue
        mint_buy, mint_sell = sides

        # Both LSTs must trade on Manifest LST/SOL
        if mint_buy not in MANIFEST_LST_SOL_MARKETS:
            continue
        if mint_sell not in MANIFEST_LST_SOL_MARKETS:
            continue

        # Inventory gates: don't pile on one side if we're already lopsided
        # (e.g. net long jupSOL by >2 SOL → skip the BID half of any signal
        # that would buy more jupSOL).
        skip_buy = inventory_skew_sol.get(mint_buy, 0.0) > max_inv_skew
        skip_sell = inventory_skew_sol.get(mint_sell, 0.0) < -max_inv_skew

        inside_bps = _inside_bps_for_z(sig.zscore, regime_caution)
        size_base = quote_size_sol  # 0.5 SOL of each LST (LSTs trade ~1:1 with SOL)

        pair_label = f"{LST_SYMBOLS.get(sig.mints[0], sig.mints[0][:6])}/" \
                     f"{LST_SYMBOLS.get(sig.mints[1], sig.mints[1][:6])} " \
                     f"z={sig.zscore:+.2f}"

        if not skip_buy:
            tob = books.get(str(MANIFEST_LST_SOL_MARKETS[mint_buy]))
            if tob:
                price = _price_with_inside_offset("bid", tob, inside_bps)
                if price is not None:
                    intents.append(QuoteIntent(
                        market=MANIFEST_LST_SOL_MARKETS[mint_buy],
                        market_label=f"{LST_SYMBOLS.get(mint_buy, mint_buy[:6])}/SOL",
                        side="bid",
                        price=price,
                        size_base=size_base,
                        pair_label=pair_label,
                        z=sig.zscore,
                        reason=f"buy underpriced leg of {pair_label} @ touch+{inside_bps}bp",
                    ))

        if not skip_sell:
            tob = books.get(str(MANIFEST_LST_SOL_MARKETS[mint_sell]))
            if tob:
                price = _price_with_inside_offset("ask", tob, inside_bps)
                if price is not None:
                    intents.append(QuoteIntent(
                        market=MANIFEST_LST_SOL_MARKETS[mint_sell],
                        market_label=f"{LST_SYMBOLS.get(mint_sell, mint_sell[:6])}/SOL",
                        side="ask",
                        price=price,
                        size_base=size_base,
                        pair_label=pair_label,
                        z=sig.zscore,
                        reason=f"sell overpriced leg of {pair_label} @ touch-{inside_bps}bp",
                    ))

    return intents
