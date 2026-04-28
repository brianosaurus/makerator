"""Unit-style tests for quoter.decide_quotes — synthetic signals + books.

Validates end-to-end quoter logic without needing 30min of signal warmup.
No on-chain calls.
"""
import time

from constants import JUPSOL_MINT, JITOSOL_MINT, VSOL_MINT, DSOL_MINT, BONKSOL_MINT
from signals import Signal, SignalType
import quoter


def make_signal(zscore: float, mint_a: str, mint_b: str, sym_a: str, sym_b: str,
                signal_type: SignalType) -> Signal:
    """Mints sorted; hedge_ratios canonicalized as [+1, -hr] with hr ≈ 1 for LSTs."""
    return Signal(
        signal_type=signal_type,
        pair_key=f"{mint_a},{mint_b}",
        basket_size=2,
        mints=[mint_a, mint_b],
        symbols=[sym_a, sym_b],
        hedge_ratios=[1.0, -1.0],  # near-1 for LST/LST
        zscore=zscore,
        spread=0.005,
        spread_mean=0.0,
        spread_std=0.001,
        timestamp=int(time.time()),
        slot=0,
        half_life_secs=180.0,
        eg_p_value=0.01,
        buffer_count=100,
    )


def make_books() -> dict:
    """Books matching what we saw live on frankfurt."""
    def tob_for(market_key, bid, ask):
        return (str(quoter.MANIFEST_LST_SOL_MARKETS[market_key]),
                quoter.TopOfBook(best_bid=bid, best_ask=ask,
                                 best_bid_size=2.0, best_ask_size=2.0))
    pairs = [
        tob_for(JUPSOL_MINT,  1.183014, 1.183063),
        tob_for(JITOSOL_MINT, 1.274577, 1.274641),
        tob_for(VSOL_MINT,    1.148132, 1.148190),
        tob_for(DSOL_MINT,    1.187803, 1.188504),
        tob_for(BONKSOL_MINT, 1.174112, 1.174646),
    ]
    return dict(pairs)


class FakeConfig:
    entry_zscore = 1.0
    quote_size_sol = 0.5
    max_inventory_skew_sol = 2.0


def test_below_threshold_no_intents():
    """|z| < entry_z should produce nothing."""
    sig = make_signal(0.5, JUPSOL_MINT, JITOSOL_MINT, "jupSOL", "jitoSOL",
                      SignalType.ENTRY_SHORT)
    intents = quoter.decide_quotes(FakeConfig(), [sig], make_books())
    assert intents == [], f"expected 0 intents, got {len(intents)}"
    print("  z=0.5 (below threshold): 0 intents ✓")


def test_entry_short_jupsol_jitosol():
    """ENTRY_SHORT on (jupSOL, jitoSOL) z=+2.0:
       spread = log(jupSOL) - log(jitoSOL) above mean
       → jupSOL overpriced relative to jitoSOL
       → ASK on jupSOL, BID on jitoSOL"""
    sig = make_signal(2.0, JUPSOL_MINT, JITOSOL_MINT, "jupSOL", "jitoSOL",
                      SignalType.ENTRY_SHORT)
    intents = quoter.decide_quotes(FakeConfig(), [sig], make_books())
    assert len(intents) == 2, f"expected 2 intents, got {len(intents)}"
    by_market = {it.market_label: it for it in intents}
    assert "jupSOL/SOL" in by_market and by_market["jupSOL/SOL"].side == "ask"
    assert "jitoSOL/SOL" in by_market and by_market["jitoSOL/SOL"].side == "bid"
    # Inside-bps logic: |z|=2.0 ≤ 2.5 → 0.2 bp inside
    jup_intent = by_market["jupSOL/SOL"]
    expected_ask = 1.183063 * (1 - 0.2 / 1e4)
    assert abs(jup_intent.price - expected_ask) < 1e-7, (
        f"jupSOL ask: {jup_intent.price} vs expected {expected_ask}"
    )
    print(f"  z=+2.0 short: ASK jupSOL@{jup_intent.price:.9f} + "
          f"BID jitoSOL@{by_market['jitoSOL/SOL'].price:.9f} ✓")


def test_entry_long_swaps_sides():
    """ENTRY_LONG on (jupSOL, jitoSOL) z=-2.0:
       spread below mean → jupSOL underpriced
       → BID on jupSOL, ASK on jitoSOL"""
    sig = make_signal(-2.0, JUPSOL_MINT, JITOSOL_MINT, "jupSOL", "jitoSOL",
                      SignalType.ENTRY_LONG)
    intents = quoter.decide_quotes(FakeConfig(), [sig], make_books())
    assert len(intents) == 2
    by_market = {it.market_label: it for it in intents}
    assert by_market["jupSOL/SOL"].side == "bid"
    assert by_market["jitoSOL/SOL"].side == "ask"
    print(f"  z=-2.0 long:  BID jupSOL@{by_market['jupSOL/SOL'].price:.9f} + "
          f"ASK jitoSOL@{by_market['jitoSOL/SOL'].price:.9f} ✓")


def test_inside_bps_scales_with_z():
    """Stronger signals → quote further inside the touch."""
    cfg = FakeConfig()
    weak = make_signal(1.5, JUPSOL_MINT, JITOSOL_MINT, "jupSOL", "jitoSOL",
                       SignalType.ENTRY_SHORT)
    strong = make_signal(3.0, JUPSOL_MINT, JITOSOL_MINT, "jupSOL", "jitoSOL",
                         SignalType.ENTRY_SHORT)
    weak_ask = next(i for i in quoter.decide_quotes(cfg, [weak], make_books())
                    if i.market_label == "jupSOL/SOL").price
    strong_ask = next(i for i in quoter.decide_quotes(cfg, [strong], make_books())
                      if i.market_label == "jupSOL/SOL").price
    assert strong_ask < weak_ask, (
        f"strong signal should price more aggressively (lower ask): "
        f"weak={weak_ask:.9f} strong={strong_ask:.9f}"
    )
    print(f"  z=1.5 ask {weak_ask:.9f} > z=3.0 ask {strong_ask:.9f} (more aggressive) ✓")


def test_skips_when_token_not_on_manifest():
    """Signal involving an LST not on Manifest LST/SOL → skipped entirely."""
    from constants import MSOL_MINT, BSOL_MINT
    # mSOL and bSOL aren't in MANIFEST_LST_SOL_MARKETS
    sig = make_signal(2.5, JUPSOL_MINT, MSOL_MINT, "jupSOL", "mSOL",
                      SignalType.ENTRY_SHORT)
    intents = quoter.decide_quotes(FakeConfig(), [sig], make_books())
    assert intents == [], "should skip baskets with non-Manifest tokens"
    print("  jupSOL/mSOL signal (mSOL not on Manifest): 0 intents ✓")


def test_inventory_skew_blocks_buy():
    """If we're already net long jupSOL > max_skew, skip the BID half."""
    sig = make_signal(-2.0, JUPSOL_MINT, JITOSOL_MINT, "jupSOL", "jitoSOL",
                      SignalType.ENTRY_LONG)  # would BID jupSOL
    inv = {JUPSOL_MINT: 3.0, JITOSOL_MINT: 0.0}  # over the 2.0 max_skew
    intents = quoter.decide_quotes(FakeConfig(), [sig], make_books(),
                                   inventory_skew_sol=inv)
    market_labels = {it.market_label for it in intents}
    assert "jupSOL/SOL" not in market_labels, "should skip jupSOL bid (already long)"
    assert "jitoSOL/SOL" in market_labels, "should still post jitoSOL ask"
    print("  inventory long jupSOL=3 SOL: jupSOL BID skipped, jitoSOL ASK kept ✓")


def test_3_token_basket_ignored():
    """v1 only handles 2-token baskets."""
    sig = make_signal(2.5, JUPSOL_MINT, JITOSOL_MINT, "jupSOL", "jitoSOL",
                      SignalType.ENTRY_SHORT)
    sig.basket_size = 3
    sig.mints = [JUPSOL_MINT, JITOSOL_MINT, VSOL_MINT]
    sig.hedge_ratios = [1.0, -0.5, -0.5]
    intents = quoter.decide_quotes(FakeConfig(), [sig], make_books())
    assert intents == [], "should skip 3-token baskets"
    print("  3-token basket: 0 intents ✓")


if __name__ == "__main__":
    print("quoter logic tests:")
    test_below_threshold_no_intents()
    test_entry_short_jupsol_jitosol()
    test_entry_long_swaps_sides()
    test_inside_bps_scales_with_z()
    test_skips_when_token_not_on_manifest()
    test_inventory_skew_blocks_buy()
    test_3_token_basket_ignored()
    print("all ok")
