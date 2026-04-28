"""Manifest CLOB instruction builders.

Encodes ix data + account lists for the Manifest program
(MNFSTqtC93rEfYHB6hF82sKdZpUDFWkViLByLd1k1Ms). No public Python SDK exists;
this module is the canonical reference for makerator.

Spec: see ../../.claude/projects/.../memory/manifest_spec.md (derived from
CKS-Systems/manifest source on github).

Mandatory flow per (wallet, market):
  1. ClaimSeat (once)   — disc 1
  2. Deposit            — disc 2
  3. BatchUpdate        — disc 6 (place / cancel orders)
  4. Withdraw           — disc 3 (pull settled fills back to ATA)

All builders return solders.instruction.Instruction. Wire format is Borsh,
with `coption<T>` = 1-byte tag (0=None, 1=Some) + payload-if-present.
"""
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional

from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey

PROGRAM_ID = Pubkey.from_string("MNFSTqtC93rEfYHB6hF82sKdZpUDFWkViLByLd1k1Ms")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

# Discriminators (single u8 prefix on ix data)
DISC_CLAIM_SEAT = 1
DISC_DEPOSIT = 2
DISC_WITHDRAW = 3
DISC_BATCH_UPDATE = 6

NIL = 0xFFFF_FFFF  # sentinel DataIndex meaning "no node"
NO_EXPIRATION = 0  # last_valid_slot=0 disables expiry

# MarketFixed
MARKET_FIXED_DISCRIMINANT = 4859840929024028656
MARKET_FIXED_HEADER_BYTES = 256
MARKET_BLOCK_SIZE = 80
RBNODE_HEADER_BYTES = 16


class OrderType(IntEnum):
    LIMIT = 0
    IOC = 1
    POST_ONLY = 2  # makerator default
    GLOBAL = 3
    REVERSE = 4
    REVERSE_TIGHT = 5


@dataclass
class PlaceOrderParams:
    """19-byte Borsh-encoded payload per order in a BatchUpdate.

    Verified against programs/manifest/src/program/processor/batch_update.rs:
    no #[repr(...)], pure Borsh (try_from_slice), so wire bytes = sum of fields."""
    base_atoms: int          # u64 — order size in base lamports
    price_mantissa: int      # u32 — price = mantissa × 10^exponent
    price_exponent: int      # i8, range [-18, +8]
    is_bid: bool             # True = buy, False = sell
    last_valid_slot: int = NO_EXPIRATION  # u32, 0 = no expiry
    order_type: OrderType = OrderType.POST_ONLY

    def pack(self) -> bytes:
        # struct fmt: u64 base_atoms | u32 mantissa | i8 exp | u8 is_bid | u32 slot | u8 type
        return struct.pack(
            "<QIbBIB",
            self.base_atoms,
            self.price_mantissa,
            self.price_exponent,
            1 if self.is_bid else 0,
            self.last_valid_slot,
            int(self.order_type),
        )


@dataclass
class CancelOrderParams:
    """8-13 byte payload per cancel in a BatchUpdate."""
    order_sequence_number: int             # u64
    order_index_hint: Optional[int] = None  # u32, optional speedup

    def pack(self) -> bytes:
        out = struct.pack("<Q", self.order_sequence_number)
        out += _coption_u32(self.order_index_hint)
        return out


def _coption_u32(value: Optional[int]) -> bytes:
    """Manifest's coption: 1 tag byte (0=None, 1=Some) + u32 payload-if-Some.

    NOTE: this is NOT Borsh-standard option encoding. Borsh's standard option
    is also tag+payload but Manifest's discriminator may differ — verify on
    the live program if mismatches appear."""
    if value is None:
        return b"\x00"
    return b"\x01" + struct.pack("<I", value)


def _encode_price(human_price: float, base_decimals: int, quote_decimals: int) -> tuple[int, int]:
    """Convert a human price (quote per base) into (mantissa u32, exponent i8).

    Manifest's price = mantissa × 10^exponent, in QUOTE ATOMS PER BASE ATOM.
    So we adjust for the decimals difference: atom-price = human × 10^(quote_dec - base_dec).

    Picks the largest mantissa that fits in u32 to maximize precision."""
    atom_price = human_price * (10 ** (quote_decimals - base_decimals))
    if atom_price <= 0:
        raise ValueError(f"price must be positive, got {atom_price}")

    # Find the exponent that lands mantissa in [10^9, 10^9.5] for ~9 sig figs of precision
    # while staying inside u32 (max 4_294_967_295 ≈ 4.29 × 10^9).
    import math
    target_mantissa_exp = 9
    log10 = math.log10(atom_price)
    exponent = int(math.floor(log10)) - target_mantissa_exp
    if exponent < -18:
        exponent = -18
    if exponent > 8:
        exponent = 8
    mantissa = round(atom_price / (10 ** exponent))
    if mantissa > 0xFFFFFFFF:
        # bump exponent to fit
        exponent += 1
        mantissa = round(atom_price / (10 ** exponent))
    if mantissa < 1 or mantissa > 0xFFFFFFFF:
        raise ValueError(
            f"could not encode price={human_price} into u32 mantissa with "
            f"base_dec={base_decimals} quote_dec={quote_decimals}"
        )
    return mantissa, exponent


# ───────────────────────────── ix builders ─────────────────────────────

def build_claim_seat_ix(payer: Pubkey, market: Pubkey) -> Instruction:
    """One-time per (wallet, market). Creates a ClaimedSeat node holding
    the trader's running base/quote balances. Cheap (~2-3k CU)."""
    accounts = [
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=market, is_signer=False, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(
        program_id=PROGRAM_ID,
        accounts=accounts,
        data=bytes([DISC_CLAIM_SEAT]),
    )


def build_deposit_ix(
    payer: Pubkey,
    market: Pubkey,
    trader_token_account: Pubkey,
    market_vault: Pubkey,
    mint: Pubkey,
    amount_atoms: int,
    trader_index_hint: Optional[int] = None,
    token_program_id: Pubkey = TOKEN_PROGRAM_ID,
) -> Instruction:
    """Move tokens from the trader's ATA into the market vault, crediting their seat.

    `amount_atoms` is in raw token units (human × 10^decimals).
    `trader_index_hint` is the DataIndex of the caller's ClaimedSeat for fast
    seat lookup; pass None on first call, cache it from prior parses afterward."""
    accounts = [
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=market, is_signer=False, is_writable=True),
        AccountMeta(pubkey=trader_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=market_vault, is_signer=False, is_writable=True),
        AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
    ]
    data = bytes([DISC_DEPOSIT]) + struct.pack("<Q", amount_atoms) + _coption_u32(trader_index_hint)
    return Instruction(program_id=PROGRAM_ID, accounts=accounts, data=data)


def build_withdraw_ix(
    payer: Pubkey,
    market: Pubkey,
    trader_token_account: Pubkey,
    market_vault: Pubkey,
    mint: Pubkey,
    amount_atoms: int,
    trader_index_hint: Optional[int] = None,
    token_program_id: Pubkey = TOKEN_PROGRAM_ID,
) -> Instruction:
    """Pull settled balances from the seat back to the trader's ATA.

    Wire format identical to deposit; only the discriminator differs.
    `amount_atoms` may be larger than the trader's balance — the program
    will withdraw the available amount (verify on first live use)."""
    accounts = [
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=market, is_signer=False, is_writable=True),
        AccountMeta(pubkey=trader_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=market_vault, is_signer=False, is_writable=True),
        AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
    ]
    data = bytes([DISC_WITHDRAW]) + struct.pack("<Q", amount_atoms) + _coption_u32(trader_index_hint)
    return Instruction(program_id=PROGRAM_ID, accounts=accounts, data=data)


def build_batch_update_ix(
    payer: Pubkey,
    market: Pubkey,
    cancels: Optional[List[CancelOrderParams]] = None,
    orders: Optional[List[PlaceOrderParams]] = None,
    trader_index_hint: Optional[int] = None,
) -> Instruction:
    """Place new orders and/or cancel existing ones in a single tx.

    For PostOnly/Limit/IOC against the trader's deposited inventory, only
    payer + market + system_program accounts are required (verified MEDIUM —
    confirm against live mainnet behavior on first run). Global order types
    additionally require base/quote mint, global, vault, and token_program
    accounts — not implemented here; add when needed."""
    cancels = cancels or []
    orders = orders or []

    accounts = [
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=market, is_signer=False, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]

    parts = [bytes([DISC_BATCH_UPDATE])]
    parts.append(_coption_u32(trader_index_hint))
    parts.append(struct.pack("<I", len(cancels)))
    for c in cancels:
        parts.append(c.pack())
    parts.append(struct.pack("<I", len(orders)))
    for o in orders:
        parts.append(o.pack())

    return Instruction(program_id=PROGRAM_ID, accounts=accounts, data=b"".join(parts))


# Re-export the price encoder so the order-lifecycle layer can convert
# human prices to (mantissa, exponent) pairs without re-importing structs.
encode_price = _encode_price


# ───────────────────────────── account parsers ─────────────────────────────
#
# MarketFixed account layout (256-byte fixed header + dynamic 80-byte blocks).
# See manifest_spec.md for full byte offsets. Only fields needed for
# top-of-book + own-orders walks are parsed here.

@dataclass
class MarketHeader:
    base_mint: Pubkey
    quote_mint: Pubkey
    base_vault: Pubkey
    quote_vault: Pubkey
    base_decimals: int
    quote_decimals: int
    order_sequence_number: int
    bids_root: int       # DataIndex of root bid node, or NIL
    bids_best: int       # DataIndex of best (highest-price) bid, or NIL
    asks_root: int
    asks_best: int       # best (lowest-price) ask
    claimed_seats_root: int
    claimed_seats_best: int
    free_list_head: int
    quote_volume: int


@dataclass
class RestingOrder:
    """A single resting order parsed from a bid/ask tree node."""
    data_index: int                # DataIndex (relative to dynamic region start)
    price_inner: int               # raw u128 inner value = mantissa × 10^(18+exp)
    num_base_atoms: int
    sequence_number: int
    trader_index: int              # DataIndex of the owning ClaimedSeat
    last_valid_slot: int
    is_bid: bool
    order_type: int                # OrderType u8

    def price_human(self, base_decimals: int, quote_decimals: int) -> float:
        """Decode price_inner → human (quote per base) ratio.

        Matches the TS SDK's deserializeMarketBuffer:
          tokenPrice = (inner / 10^18) × 10^(base_decimals - quote_decimals)
        Verified against client/ts/src/market.ts. The on-chain encoding is
        inner = mantissa × 10^(18 + exponent) in quote-atoms/base-atom units."""
        return (self.price_inner / 1e18) * (10 ** (base_decimals - quote_decimals))


def _abs(idx: int) -> int:
    """DataIndex → absolute byte offset in the account.

    Critical: DataIndex is measured from the start of the *dynamic region*
    (byte 256), NOT byte 0. Failing to add 256 reads at the wrong block
    position and yields garbage."""
    return MARKET_FIXED_HEADER_BYTES + idx


def parse_market_header(data: bytes) -> MarketHeader:
    """Parse the 256-byte fixed header. Verifies the discriminant first."""
    if len(data) < MARKET_FIXED_HEADER_BYTES:
        raise ValueError(f"market account too short: {len(data)}B (need ≥256)")
    disc = struct.unpack_from("<Q", data, 0)[0]
    if disc != MARKET_FIXED_DISCRIMINANT:
        raise ValueError(
            f"not a Manifest market: discriminant {disc} != {MARKET_FIXED_DISCRIMINANT}"
        )
    base_dec = data[9]
    quote_dec = data[10]
    base_mint = Pubkey.from_bytes(data[16:48])
    quote_mint = Pubkey.from_bytes(data[48:80])
    base_vault = Pubkey.from_bytes(data[80:112])
    quote_vault = Pubkey.from_bytes(data[112:144])
    seq = struct.unpack_from("<Q", data, 144)[0]
    # Seven DataIndex fields starting at offset 156, then a u32 padding,
    # then quote_volume at offset 188.
    (bids_root, bids_best, asks_root, asks_best,
     seats_root, seats_best, free_head) = struct.unpack_from("<IIIIIII", data, 156)
    quote_vol = struct.unpack_from("<Q", data, 188)[0]
    return MarketHeader(
        base_mint=base_mint, quote_mint=quote_mint,
        base_vault=base_vault, quote_vault=quote_vault,
        base_decimals=base_dec, quote_decimals=quote_dec,
        order_sequence_number=seq,
        bids_root=bids_root, bids_best=bids_best,
        asks_root=asks_root, asks_best=asks_best,
        claimed_seats_root=seats_root, claimed_seats_best=seats_best,
        free_list_head=free_head, quote_volume=quote_vol,
    )


def parse_resting_order_at(data: bytes, idx: int) -> Optional[RestingOrder]:
    """Read the 80-byte block at DataIndex `idx` and parse as a RestingOrder.

    Returns None if `idx` is NIL or out of range. `idx` is the on-chain
    DataIndex value (offset relative to the start of the dynamic region)."""
    if idx == NIL:
        return None
    base = _abs(idx)
    if base + MARKET_BLOCK_SIZE > len(data):
        return None
    # RBNode header at base+0..16; RestingOrder payload at base+16..80.
    p = base + RBNODE_HEADER_BYTES
    price_inner = int.from_bytes(data[p:p + 16], "little")
    num_base_atoms = struct.unpack_from("<Q", data, p + 16)[0]
    seq = struct.unpack_from("<Q", data, p + 24)[0]
    trader_idx = struct.unpack_from("<I", data, p + 32)[0]
    last_valid_slot = struct.unpack_from("<I", data, p + 36)[0]
    is_bid = bool(data[p + 40])
    order_type = data[p + 41]
    return RestingOrder(
        data_index=idx,
        price_inner=price_inner,
        num_base_atoms=num_base_atoms,
        sequence_number=seq,
        trader_index=trader_idx,
        last_valid_slot=last_valid_slot,
        is_bid=is_bid,
        order_type=order_type,
    )


def _node_children(data: bytes, idx: int) -> tuple[int, int]:
    """Return (left, right) child DataIndexes for the RB node at DataIndex `idx`."""
    return struct.unpack_from("<II", data, _abs(idx))


def walk_orders_inorder(data: bytes, root: int):
    """Yield RestingOrder for each node reachable from `root`, in tree
    in-order. For a buy book, that's ascending price; for an ask book,
    ascending price too (lowest first). Use `bids_best`/`asks_best` for the
    direct top-of-book if you only need one side's best."""
    if root == NIL:
        return
    stack: list[int] = []
    cur = root
    while stack or cur != NIL:
        while cur != NIL:
            stack.append(cur)
            left, _ = _node_children(data, cur)
            cur = left
        cur = stack.pop()
        order = parse_resting_order_at(data, cur)
        if order is not None:
            yield order
        _, right = _node_children(data, cur)
        cur = right


def top_of_book(data: bytes) -> tuple[Optional[RestingOrder], Optional[RestingOrder]]:
    """Return (best_bid, best_ask) using the cached *_best_index pointers."""
    h = parse_market_header(data)
    return (
        parse_resting_order_at(data, h.bids_best),
        parse_resting_order_at(data, h.asks_best),
    )


# ───────────────────────────── ClaimedSeat parsing ─────────────────────

@dataclass
class ClaimedSeat:
    data_index: int     # DataIndex of the seat node
    trader: Pubkey
    base_balance_atoms: int
    quote_balance_atoms: int


def parse_claimed_seat_at(data: bytes, idx: int) -> Optional[ClaimedSeat]:
    """Read the 80-byte block at DataIndex `idx` as a ClaimedSeat."""
    if idx == NIL:
        return None
    base = _abs(idx)
    if base + MARKET_BLOCK_SIZE > len(data):
        return None
    p = base + RBNODE_HEADER_BYTES  # +16 to skip RBNode header
    trader = Pubkey.from_bytes(data[p:p + 32])
    base_bal = struct.unpack_from("<Q", data, p + 32)[0]
    quote_bal = struct.unpack_from("<Q", data, p + 40)[0]
    return ClaimedSeat(
        data_index=idx, trader=trader,
        base_balance_atoms=base_bal, quote_balance_atoms=quote_bal,
    )


def walk_seats_inorder(data: bytes, root: int):
    """Yield ClaimedSeat for each node reachable from `root`."""
    if root == NIL:
        return
    stack: list[int] = []
    cur = root
    while stack or cur != NIL:
        while cur != NIL:
            stack.append(cur)
            left, _ = _node_children(data, cur)
            cur = left
        cur = stack.pop()
        seat = parse_claimed_seat_at(data, cur)
        if seat is not None:
            yield seat
        _, right = _node_children(data, cur)
        cur = right


def find_seat_for_trader(data: bytes, trader: Pubkey) -> Optional[ClaimedSeat]:
    """Walk the seats tree, return the seat owned by `trader` or None."""
    h = parse_market_header(data)
    for seat in walk_seats_inorder(data, h.claimed_seats_root):
        if seat.trader == trader:
            return seat
    return None


# ───────────────────────────── return data ─────────────────────────────

def parse_batch_update_return(data: bytes) -> List[tuple]:
    """Parse a BatchUpdate program-return payload → list of (seq, hint) per
    NEWLY-PLACED order. Cancels do NOT appear in this payload.

    Layout (verified live 2026-04-27 against tx 5DF85i...tt and 2DjFZ8...jV):
      offset 0: u32 LE count of new orders
      then per order (12 bytes):
        u64 LE order_sequence_number
        u32 LE order_index_hint  (DataIndex of the resting node)
    """
    if len(data) < 4:
        return []
    count = struct.unpack_from("<I", data, 0)[0]
    out = []
    for i in range(count):
        off = 4 + i * 12
        if off + 12 > len(data):
            break
        seq = struct.unpack_from("<Q", data, off)[0]
        hint = struct.unpack_from("<I", data, off + 8)[0]
        out.append((seq, hint))
    return out
