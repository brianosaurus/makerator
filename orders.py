"""Order lifecycle manager for makerator.

Pure logic. Inputs:
  - quoter intents (what we WANT to be quoting)
  - current open orders (what we ARE quoting on chain, by sequence_number)
  - per-market sets of sequence_numbers currently in the book (for cleanup)

Outputs:
  - PlaceAction[] / CancelAction[] for the runner to submit

Single source of truth = the local `open_orders` dict, populated by the runner
after each successful place tx (`register_placed`) and proactively cleared
before each cancel tx (`register_cancelled`). Books are polled to clean up
local state when orders disappear from the chain (filled or expired by chain).

v1 simplifications:
  - One slot per (market, side). If multiple intents collide there, the one
    with the largest |z| wins.
  - Presence-based detection: order gone from book = it's not ours anymore.
    Partial-fill tracking is deferred (the runner can read inventory directly
    from chain state for accurate balance accounting).
  - No persistence across restarts. Bootstrap from chain by walking seats +
    resting orders for our trader_index (runner concern, not this module's).
"""
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from solders.pubkey import Pubkey

from quoter import QuoteIntent


class CancelReason(str, Enum):
    NO_INTENT = "no_intent"        # signal disappeared
    PRICE_STALE = "price_stale"    # fair value moved > requote_threshold_bps
    TTL = "ttl"                    # order older than ttl_seconds
    INVENTORY_LIMIT = "inventory"   # we're now over-skewed on this side


@dataclass
class OpenOrder:
    market: Pubkey
    market_label: str
    side: str                          # "bid" / "ask"
    price: float
    size_base: float
    sequence_number: int               # u64 from BatchUpdate program return
    order_index_hint: int              # u32, for fast cancel
    placed_at: float                   # unix time
    intent_pair: str                   # logging label (e.g. "jupSOL/jitoSOL z=+2.34")


@dataclass
class PlaceAction:
    intent: QuoteIntent


@dataclass
class CancelAction:
    market: Pubkey
    market_label: str
    sequence_number: int
    order_index_hint: int
    reason: CancelReason
    side: str
    price: float


@dataclass
class ReconcileResult:
    cancels: List[CancelAction] = field(default_factory=list)
    places: List[PlaceAction] = field(default_factory=list)
    kept: int = 0                          # orders that already match an intent
    cleaned_from_book: List[int] = field(default_factory=list)  # seqs gone from chain


class OrderManager:
    def __init__(self, config):
        self.config = config
        self.open_orders: Dict[int, OpenOrder] = {}        # by sequence_number
        self._pending_cancel: Set[int] = set()             # we initiated cancel; ignore disappearance

    # ── runner-callable mutations ──────────────────────────────────────────

    def register_placed(self, intent: QuoteIntent,
                        sequence_number: int, order_index_hint: int,
                        now: Optional[float] = None) -> None:
        self.open_orders[sequence_number] = OpenOrder(
            market=intent.market,
            market_label=intent.market_label,
            side=intent.side,
            price=intent.price,
            size_base=intent.size_base,
            sequence_number=sequence_number,
            order_index_hint=order_index_hint,
            placed_at=now if now is not None else time.time(),
            intent_pair=intent.pair_label,
        )

    def register_cancelled(self, sequence_number: int) -> None:
        """Mark a cancel as in-flight so book-presence cleanup doesn't double-fire."""
        self._pending_cancel.add(sequence_number)

    # ── main step ─────────────────────────────────────────────────────────

    def reconcile(
        self,
        intents: List[QuoteIntent],
        live_seqs_by_market_side: Dict[Tuple[str, str], Set[int]],
        now: Optional[float] = None,
        inventory_skew_sol: Optional[Dict[str, float]] = None,
    ) -> ReconcileResult:
        """One tick of reconciliation.

        Args:
            intents: quoter output for this tick.
            live_seqs_by_market_side: per-(market_pubkey_str, side) set of
                sequence_numbers currently in the book. Used for cleanup.
            now: unix time (defaults to time.time()).
            inventory_skew_sol: per-mint net SOL position, for inv-skew cancels.
        """
        if now is None:
            now = time.time()
        result = ReconcileResult()

        # 1. Clean up open_orders no longer in the book.
        for seq in list(self.open_orders.keys()):
            order = self.open_orders[seq]
            key = (str(order.market), order.side)
            live = live_seqs_by_market_side.get(key, set())
            if seq in live:
                continue
            # Order vanished from chain — we cancelled it OR it was filled/expired.
            self._pending_cancel.discard(seq)
            del self.open_orders[seq]
            result.cleaned_from_book.append(seq)

        # 2. Build (market_str, side) → highest-|z| intent map.
        slot_intent: Dict[Tuple[str, str], QuoteIntent] = {}
        for it in intents:
            key = (str(it.market), it.side)
            cur = slot_intent.get(key)
            if cur is None or abs(it.z) > abs(cur.z):
                slot_intent[key] = it

        # 3. Walk current open orders, match against intents.
        requote_thresh_bps = float(getattr(self.config, "requote_threshold_bps", 2.0))
        ttl_secs = float(getattr(self.config, "order_ttl_seconds", 60.0))

        for seq, order in list(self.open_orders.items()):
            key = (str(order.market), order.side)
            intent = slot_intent.get(key)

            if intent is None:
                result.cancels.append(CancelAction(
                    market=order.market,
                    market_label=order.market_label,
                    sequence_number=seq,
                    order_index_hint=order.order_index_hint,
                    reason=CancelReason.NO_INTENT,
                    side=order.side,
                    price=order.price,
                ))
                continue

            # Has matching intent — check freshness
            diff_bps = abs(intent.price - order.price) / order.price * 10_000
            age = now - order.placed_at

            if diff_bps > requote_thresh_bps:
                result.cancels.append(CancelAction(
                    market=order.market, market_label=order.market_label,
                    sequence_number=seq, order_index_hint=order.order_index_hint,
                    reason=CancelReason.PRICE_STALE,
                    side=order.side, price=order.price,
                ))
                result.places.append(PlaceAction(intent=intent))
                slot_intent.pop(key, None)
                continue

            if age > ttl_secs:
                result.cancels.append(CancelAction(
                    market=order.market, market_label=order.market_label,
                    sequence_number=seq, order_index_hint=order.order_index_hint,
                    reason=CancelReason.TTL,
                    side=order.side, price=order.price,
                ))
                result.places.append(PlaceAction(intent=intent))
                slot_intent.pop(key, None)
                continue

            # Order is fresh; intent matches. Keep both — drop intent so it doesn't double-place.
            result.kept += 1
            slot_intent.pop(key, None)

        # 4. Any leftover intents need fresh placements.
        for it in slot_intent.values():
            result.places.append(PlaceAction(intent=it))

        return result

    # ── helpers for the runner ────────────────────────────────────────────

    def open_count(self) -> int:
        return len(self.open_orders)

    def by_market(self) -> Dict[str, List[OpenOrder]]:
        out: Dict[str, List[OpenOrder]] = {}
        for o in self.open_orders.values():
            out.setdefault(str(o.market), []).append(o)
        return out
