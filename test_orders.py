"""Unit tests for orders.OrderManager — pure logic, no I/O."""
from solders.pubkey import Pubkey

from quoter import QuoteIntent
from orders import OrderManager, CancelReason


JUP_MARKET = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")
JITO_MARKET = Pubkey.from_string("7ecvmhGKVcK4SgxeGQJG6yVwVAhbQxLrBuaMoUmpRZ6i")


class FakeConfig:
    requote_threshold_bps = 2.0
    order_ttl_seconds = 60.0


def make_intent(market, side, price, z=2.0, size=0.5, label="jupSOL/jitoSOL"):
    return QuoteIntent(
        market=market,
        market_label=f"{label.split('/')[0]}/SOL",
        side=side, price=price, size_base=size,
        pair_label=f"{label} z={z:+.2f}", z=z,
    )


def test_empty_state_places_intent():
    mgr = OrderManager(FakeConfig())
    intent = make_intent(JUP_MARKET, "ask", 1.183050)
    res = mgr.reconcile([intent], live_seqs_by_market_side={}, now=1000)
    assert len(res.places) == 1
    assert res.places[0].intent.price == 1.183050
    assert len(res.cancels) == 0
    print("  empty + 1 intent: 1 place, 0 cancels ✓")


def test_existing_order_matches_kept():
    mgr = OrderManager(FakeConfig())
    intent = make_intent(JUP_MARKET, "ask", 1.183050)
    mgr.register_placed(intent, sequence_number=100, order_index_hint=4160, now=1000)

    # New tick: same intent, order still in book
    res = mgr.reconcile(
        [make_intent(JUP_MARKET, "ask", 1.183050)],  # exact same price
        live_seqs_by_market_side={(str(JUP_MARKET), "ask"): {100}},
        now=1010,  # 10s after place
    )
    assert res.kept == 1
    assert len(res.places) == 0
    assert len(res.cancels) == 0
    print("  unchanged intent + still-in-book: kept=1 ✓")


def test_price_drift_triggers_requote():
    mgr = OrderManager(FakeConfig())
    intent = make_intent(JUP_MARKET, "ask", 1.183050)
    mgr.register_placed(intent, 100, 4160, now=1000)

    # Price moved 5 bps — over the 2 bp requote threshold
    new_price = 1.183050 * (1 + 5 / 1e4)
    res = mgr.reconcile(
        [make_intent(JUP_MARKET, "ask", new_price)],
        live_seqs_by_market_side={(str(JUP_MARKET), "ask"): {100}},
        now=1010,
    )
    assert len(res.cancels) == 1
    assert res.cancels[0].reason == CancelReason.PRICE_STALE
    assert res.cancels[0].sequence_number == 100
    assert len(res.places) == 1
    assert res.places[0].intent.price == new_price
    print("  price drift 5bp > 2bp threshold: cancel+place ✓")


def test_ttl_expiry():
    mgr = OrderManager(FakeConfig())
    intent = make_intent(JUP_MARKET, "ask", 1.183050)
    mgr.register_placed(intent, 100, 4160, now=1000)

    # Same price (within threshold) but 65s old > 60s TTL
    res = mgr.reconcile(
        [make_intent(JUP_MARKET, "ask", 1.183050)],
        live_seqs_by_market_side={(str(JUP_MARKET), "ask"): {100}},
        now=1065,
    )
    assert len(res.cancels) == 1
    assert res.cancels[0].reason == CancelReason.TTL
    assert len(res.places) == 1
    print("  age 65s > ttl 60s: cancel+place (refresh) ✓")


def test_signal_disappeared_cancels():
    mgr = OrderManager(FakeConfig())
    intent = make_intent(JUP_MARKET, "ask", 1.183050)
    mgr.register_placed(intent, 100, 4160, now=1000)

    # No intents this tick (z dropped below threshold)
    res = mgr.reconcile(
        [],
        live_seqs_by_market_side={(str(JUP_MARKET), "ask"): {100}},
        now=1010,
    )
    assert len(res.cancels) == 1
    assert res.cancels[0].reason == CancelReason.NO_INTENT
    assert len(res.places) == 0
    print("  intent gone: cancel ✓")


def test_book_cleanup_removes_filled():
    """When our order vanishes from the book, it's gone — drop it locally."""
    mgr = OrderManager(FakeConfig())
    mgr.register_placed(make_intent(JUP_MARKET, "ask", 1.183050), 100, 4160, now=1000)
    assert mgr.open_count() == 1

    # Order disappears from the book
    res = mgr.reconcile(
        [make_intent(JUP_MARKET, "ask", 1.183050)],
        live_seqs_by_market_side={(str(JUP_MARKET), "ask"): set()},  # gone
        now=1010,
    )
    assert mgr.open_count() == 0
    assert 100 in res.cleaned_from_book
    # Intent still pending → fresh place
    assert len(res.places) == 1
    print("  order gone from book: cleaned + intent re-placed ✓")


def test_collision_picks_highest_z():
    """Two intents on the same (market, side): the larger |z| wins."""
    mgr = OrderManager(FakeConfig())
    weak = make_intent(JUP_MARKET, "ask", 1.183055, z=1.5, label="jupSOL/jitoSOL")
    strong = make_intent(JUP_MARKET, "ask", 1.183040, z=2.8, label="jupSOL/vSOL")
    res = mgr.reconcile([weak, strong], live_seqs_by_market_side={}, now=1000)
    assert len(res.places) == 1
    # Strong wins
    assert res.places[0].intent.price == 1.183040
    print("  collision on (market, side): strongest z wins ✓")


def test_multi_market_independent():
    """Orders on different markets don't interfere."""
    mgr = OrderManager(FakeConfig())
    mgr.register_placed(make_intent(JUP_MARKET, "ask", 1.183050), 100, 4160, now=1000)
    mgr.register_placed(make_intent(JITO_MARKET, "bid", 1.274600), 200, 5000, now=1000)

    # Only the jito intent disappears this tick
    res = mgr.reconcile(
        [make_intent(JUP_MARKET, "ask", 1.183050)],
        live_seqs_by_market_side={
            (str(JUP_MARKET), "ask"): {100},
            (str(JITO_MARKET), "bid"): {200},
        },
        now=1010,
    )
    cancels_by_seq = {c.sequence_number for c in res.cancels}
    assert 200 in cancels_by_seq, "jitoSOL bid should cancel"
    assert 100 not in cancels_by_seq, "jupSOL ask should remain"
    assert res.kept == 1
    print("  multi-market: cancel only the orphaned one ✓")


if __name__ == "__main__":
    print("orders.py tests:")
    test_empty_state_places_intent()
    test_existing_order_matches_kept()
    test_price_drift_triggers_requote()
    test_ttl_expiry()
    test_signal_disappeared_cancels()
    test_book_cleanup_removes_filled()
    test_collision_picks_highest_z()
    test_multi_market_independent()
    print("all ok")
