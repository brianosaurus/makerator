"""
gRPC pool-account price feed — multi-DEX.

Subscribes to pool accounts via Yellowstone Geyser and decodes the current
price from each account update. Sub-slot latency, no transaction parsing.

Each pool in direct_swap.POOL_REGISTRY is matched to a decoder by DEX type:
  - DEX_WHIRLPOOL      → sqrt_price at offset 65
  - DEX_RAYDIUM_CLMM   → sqrt_price_x64 at offset 253
  - DEX_METEORA (DLMM) → active_id + bin_step at offsets 76 / 80

LST/SOL pool ticks are converted to USD via the SOL/USD anchor that the
JupiterPriceFeed already maintains. LST/LST pool ticks derive USD from
whichever side already has a known USD price (cached across all decoders).

Falls back to Jupiter for: Manifest, AlphaQ, Meteora DAMM, PancakeSwap.

The MergedPriceFeed wrapper exposes the same poll() async-generator
interface as JupiterPriceFeed, so the main loop is unchanged.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable, Dict, List, Optional, Set

import base58
import grpc

import geyser_pb2
import geyser_pb2_grpc
from constants import SOL_MINT, WELL_KNOWN_TOKENS
from direct_swap import (
    POOL_REGISTRY, PoolInfo,
    DEX_WHIRLPOOL, DEX_RAYDIUM_CLMM, DEX_METEORA, DEX_MANIFEST, DEX_DAMM,
)
# Note: makerator's gRPC architecture doesn't use Jupiter. PoolPriceFeed.feed_external
# can still be called manually if a SOL/USD anchor is needed; otherwise prices are
# anchor-free SOL ratios which is what the cointegration signal needs anyway.

logger = logging.getLogger(__name__)


CHANNEL_OPTIONS = [
    ('grpc.keepalive_time_ms', 10000),
    ('grpc.keepalive_timeout_ms', 3000),
    ('grpc.keepalive_permit_without_calls', True),
    ('grpc.http2.max_pings_without_data', 0),
    ('grpc.http2.min_time_between_pings_ms', 10000),
    ('grpc.http2.min_ping_interval_without_data_ms', 300000),
    ('grpc.max_receive_message_length', 64 * 1024 * 1024),
    ('grpc.max_send_message_length', 64 * 1024 * 1024),
]


TWO_POW_128 = 1 << 128


def _decimals(mint: str) -> int:
    info = WELL_KNOWN_TOKENS.get(mint)
    if info and 'decimals' in info:
        return int(info['decimals'])
    return 9  # SPL default; LSTs and SOL are all 9


def _b58(b: bytes) -> str:
    return base58.b58encode(b).decode()


# ---------------------------------------------------------------------------
# Shared price context — every decoder reads/writes this so cross-pool USD
# derivation works (e.g. an mSOL/jitoSOL pool tick can produce a jitoSOL USD
# price using the latest mSOL USD price from another pool or from Jupiter).
# ---------------------------------------------------------------------------

@dataclass
class PriceContext:
    sol_usd_price: float = 0.0
    # latest_usd is mutated by both Jupiter (feed_external) and decoders
    # (_emit_from_b_per_a) — used as the cross-LST USD anchor.
    latest_usd: Dict[str, float] = field(default_factory=dict)
    # jupiter_usd is mutated ONLY by feed_external — used as the sanity
    # baseline so a misbehaving decoder can't poison its own check.
    jupiter_usd: Dict[str, float] = field(default_factory=dict)

    def feed_external(self, prices: Dict[str, float]):
        """Merge in prices from Jupiter (or any external source)."""
        for m, p in prices.items():
            if p and p > 0:
                self.latest_usd[m] = p
                self.jupiter_usd[m] = p
        sol = prices.get(SOL_MINT)
        if sol and sol > 0:
            self.sol_usd_price = sol


# Reject any decoder emission that disagrees with the latest Jupiter price by
# more than this. Real venue spreads on Manifest LST books run up to ~25 bps;
# 50 bps gives headroom for that without letting a layout/scale bug through.
DECODER_SANITY_BPS = 50.0


def _emit_from_b_per_a(meta_mint_a: str, meta_mint_b: str,
                       b_per_a: float, ctx: PriceContext
                       ) -> Optional[Dict[str, float]]:
    """Given a UI ratio (b_per_a = 1 token_a in token_b), return USD prices for
    whichever side(s) we can derive. Anchors via SOL/USD when one side is SOL,
    otherwise via the cached latest USD of the other side.

    Each emission is sanity-checked against the latest Jupiter snapshot — any
    mint that disagrees by more than DECODER_SANITY_BPS is dropped with a loud
    log line. This catches layout drifts and scale bugs without poisoning the
    PriceContext cache."""
    if b_per_a <= 0:
        return None
    a_per_b = 1.0 / b_per_a

    out: Dict[str, float] = {}
    if meta_mint_b == SOL_MINT and ctx.sol_usd_price > 0:
        out[meta_mint_a] = b_per_a * ctx.sol_usd_price
    elif meta_mint_a == SOL_MINT and ctx.sol_usd_price > 0:
        out[meta_mint_b] = a_per_b * ctx.sol_usd_price
    else:
        usd_a = ctx.latest_usd.get(meta_mint_a)
        usd_b = ctx.latest_usd.get(meta_mint_b)
        if usd_a and usd_a > 0:
            out[meta_mint_b] = a_per_b * usd_a
        if usd_b and usd_b > 0:
            out[meta_mint_a] = b_per_a * usd_b

    if not out:
        return None

    # Sanity check vs Jupiter snapshot. Skip if no reference yet (cold start).
    for mint in list(out.keys()):
        ref = ctx.jupiter_usd.get(mint)
        if not ref or ref <= 0:
            continue
        diff_bps = abs(out[mint] - ref) / ref * 10000.0
        if diff_bps > DECODER_SANITY_BPS:
            logger.warning(
                f"sanity check tripped: {mint[:6]}.. decoded ${out[mint]:.6f} "
                f"vs Jupiter ${ref:.6f} ({diff_bps:.0f} bps > "
                f"{DECODER_SANITY_BPS:.0f} bps threshold) — dropping emission"
            )
            del out[mint]

    if not out:
        return None
    for m, p in out.items():
        ctx.latest_usd[m] = p
    return out


# ---------------------------------------------------------------------------
# Decoders — one class per DEX. Each instance is tied to a specific pool.
# Convention: accounts() returns the Geyser pubkey subscriptions, decode()
# returns USD prices or None for one account update.
# ---------------------------------------------------------------------------

class WhirlpoolDecoder:
    """Orca Whirlpool (Anchor-encoded). sqrt_price as Q64.64 at offset 65."""
    DISCRIMINATOR = bytes([63, 149, 209, 12, 225, 128, 99, 9])
    SQRT_PRICE_OFFSET = 65
    SQRT_PRICE_LEN = 16
    MINT_A_OFFSET = 101
    MINT_B_OFFSET = 181
    MINT_LEN = 32
    MIN_LEN = 213

    def __init__(self, pool: PoolInfo):
        self.pool_address = pool.pool_address
        # Registry's token_a/token_b is just labeling — the on-chain pool may
        # have these reversed. We read the actual orientation from the account.
        self.expected_mints = frozenset({pool.token_a_mint, pool.token_b_mint})

    def accounts(self) -> List[str]:
        return [self.pool_address]

    def label(self) -> str:
        a, b = sorted(self.expected_mints)
        return f"Whirlpool({self.pool_address[:6]}.. {a[:4]}/{b[:4]})"

    def decode(self, pubkey: str, data: bytes, ctx: PriceContext
               ) -> Optional[Dict[str, float]]:
        if pubkey != self.pool_address or len(data) < self.MIN_LEN:
            return None
        if data[:8] != self.DISCRIMINATOR:
            return None

        on_chain_a = _b58(data[self.MINT_A_OFFSET:self.MINT_A_OFFSET + self.MINT_LEN])
        on_chain_b = _b58(data[self.MINT_B_OFFSET:self.MINT_B_OFFSET + self.MINT_LEN])
        if frozenset({on_chain_a, on_chain_b}) != self.expected_mints:
            logger.warning(f"{self.label()} mint set mismatch: account says "
                           f"{on_chain_a[:6]}/{on_chain_b[:6]}")
            return None

        sqrt_price = int.from_bytes(
            data[self.SQRT_PRICE_OFFSET:self.SQRT_PRICE_OFFSET + self.SQRT_PRICE_LEN],
            'little',
        )
        if sqrt_price <= 0:
            return None

        dec_a = _decimals(on_chain_a)
        dec_b = _decimals(on_chain_b)
        atomic = (sqrt_price * sqrt_price) / TWO_POW_128
        b_per_a = atomic * (10 ** (dec_a - dec_b))
        return _emit_from_b_per_a(on_chain_a, on_chain_b, b_per_a, ctx)


class RaydiumClmmDecoder:
    """Raydium CLMM PoolState. sqrt_price_x64 (Q64.64) at offset 253.
    Decimals are stored in the account itself (offsets 233/234)."""
    DISCRIMINATOR = bytes([247, 237, 227, 245, 215, 195, 222, 70])
    SQRT_PRICE_OFFSET = 253
    SQRT_PRICE_LEN = 16
    MINT_A_OFFSET = 73
    MINT_B_OFFSET = 105
    DEC_A_OFFSET = 233
    DEC_B_OFFSET = 234
    MINT_LEN = 32
    MIN_LEN = 269  # through end of sqrt_price_x64

    def __init__(self, pool: PoolInfo):
        self.pool_address = pool.pool_address
        self.expected_mints = frozenset({pool.token_a_mint, pool.token_b_mint})

    def accounts(self) -> List[str]:
        return [self.pool_address]

    def label(self) -> str:
        a, b = sorted(self.expected_mints)
        return f"RaydiumCLMM({self.pool_address[:6]}.. {a[:4]}/{b[:4]})"

    def decode(self, pubkey: str, data: bytes, ctx: PriceContext
               ) -> Optional[Dict[str, float]]:
        if pubkey != self.pool_address or len(data) < self.MIN_LEN:
            return None
        if data[:8] != self.DISCRIMINATOR:
            return None

        on_chain_a = _b58(data[self.MINT_A_OFFSET:self.MINT_A_OFFSET + self.MINT_LEN])
        on_chain_b = _b58(data[self.MINT_B_OFFSET:self.MINT_B_OFFSET + self.MINT_LEN])
        if frozenset({on_chain_a, on_chain_b}) != self.expected_mints:
            logger.warning(f"{self.label()} mint set mismatch: account says "
                           f"{on_chain_a[:6]}/{on_chain_b[:6]}")
            return None

        dec_a = data[self.DEC_A_OFFSET]
        dec_b = data[self.DEC_B_OFFSET]
        sqrt_price = int.from_bytes(
            data[self.SQRT_PRICE_OFFSET:self.SQRT_PRICE_OFFSET + self.SQRT_PRICE_LEN],
            'little',
        )
        if sqrt_price <= 0:
            return None

        atomic = (sqrt_price * sqrt_price) / TWO_POW_128
        b_per_a = atomic * (10 ** (dec_a - dec_b))
        return _emit_from_b_per_a(on_chain_a, on_chain_b, b_per_a, ctx)


class MeteoraDlmmDecoder:
    """Meteora DLMM LbPair. Bin-based pricing:
        b_per_a (atomic) = (1 + bin_step / 10000) ** active_id
    Decimals adjustment converts to UI units."""
    DISCRIMINATOR = bytes([33, 11, 49, 98, 181, 101, 177, 13])
    ACTIVE_ID_OFFSET = 76         # i32 LE  (raw byte offset, includes 8-byte disc)
    BIN_STEP_OFFSET = 80          # u16 LE
    MINT_X_OFFSET = 88            # token X (== token A in our registry)
    MINT_Y_OFFSET = 120           # token Y (== token B)
    MINT_LEN = 32
    MIN_LEN = 152                 # through end of mint_y

    def __init__(self, pool: PoolInfo):
        self.pool_address = pool.pool_address
        self.expected_mints = frozenset({pool.token_a_mint, pool.token_b_mint})

    def accounts(self) -> List[str]:
        return [self.pool_address]

    def label(self) -> str:
        a, b = sorted(self.expected_mints)
        return f"MeteoraDLMM({self.pool_address[:6]}.. {a[:4]}/{b[:4]})"

    def decode(self, pubkey: str, data: bytes, ctx: PriceContext
               ) -> Optional[Dict[str, float]]:
        if pubkey != self.pool_address or len(data) < self.MIN_LEN:
            return None
        if data[:8] != self.DISCRIMINATOR:
            return None

        on_chain_x = _b58(data[self.MINT_X_OFFSET:self.MINT_X_OFFSET + self.MINT_LEN])
        on_chain_y = _b58(data[self.MINT_Y_OFFSET:self.MINT_Y_OFFSET + self.MINT_LEN])
        if frozenset({on_chain_x, on_chain_y}) != self.expected_mints:
            logger.warning(f"{self.label()} mint set mismatch: account says "
                           f"{on_chain_x[:6]}/{on_chain_y[:6]}")
            return None

        active_id = int.from_bytes(
            data[self.ACTIVE_ID_OFFSET:self.ACTIVE_ID_OFFSET + 4],
            'little', signed=True,
        )
        bin_step = int.from_bytes(
            data[self.BIN_STEP_OFFSET:self.BIN_STEP_OFFSET + 2],
            'little',
        )
        if bin_step <= 0:
            return None

        dec_x = _decimals(on_chain_x)
        dec_y = _decimals(on_chain_y)
        # b_per_a (atomic) = (1 + bin_step / 10000) ** active_id
        atomic = (1.0 + bin_step / 10000.0) ** active_id
        b_per_a = atomic * (10 ** (dec_x - dec_y))
        return _emit_from_b_per_a(on_chain_x, on_chain_y, b_per_a, ctx)


class ManifestDecoder:
    """Manifest CLOB market account.

    MarketFixed (256 bytes) caches the best bid and best ask DataIndex
    pointers — we don't need to walk the red-black tree, just dereference
    them and read each top-of-book RestingOrder's price. Mid = (bid + ask)/2.

    Layout reference (CKS Systems / manifest):
      MARKET_FIXED_SIZE   = 256
      MARKET_BLOCK_SIZE   = 80
      RBTREE_OVERHEAD     = 16  (4 + 4 + 4 + 1 + 1 + 2 = left/right/parent/
                                 color/payload_type/_padding)
      RestingOrder.price  = QuoteAtomsPerBaseAtom (u128 inner)
                          stored as quote_atoms_per_base_atom * 10^18
      DataIndex NIL       = 0xFFFFFFFF (empty side)

    MarketFixed offsets:
        9   base_mint_decimals u8
        10  quote_mint_decimals u8
        16  base_mint Pubkey
        48  quote_mint Pubkey
        160 bids_best_index u32
        168 asks_best_index u32

    Within each RBNode (80 bytes):
        0..16   RBTree header
        16..32  RestingOrder.price (u128 LE)
    """
    DISCRIMINANT = 4859840929024028656  # MARKET_FIXED_DISCRIMINANT
    BASE_DEC_OFFSET = 9
    QUOTE_DEC_OFFSET = 10
    BASE_MINT_OFFSET = 16
    QUOTE_MINT_OFFSET = 48
    BIDS_BEST_OFFSET = 160
    ASKS_BEST_OFFSET = 168
    MARKET_FIXED_SIZE = 256
    RBNODE_HEADER_SIZE = 16
    PRICE_LEN = 16
    NIL = 0xFFFFFFFF
    PRICE_DIVISOR = 10 ** 18

    def __init__(self, pool: PoolInfo):
        self.pool_address = pool.pool_address
        self.expected_mints = frozenset({pool.token_a_mint, pool.token_b_mint})

    def accounts(self) -> List[str]:
        return [self.pool_address]

    def label(self) -> str:
        a, b = sorted(self.expected_mints)
        return f"Manifest({self.pool_address[:6]}.. {a[:4]}/{b[:4]})"

    def _read_order_price(self, data: bytes, idx: int,
                          base_dec: int, quote_dec: int) -> Optional[float]:
        if idx == self.NIL:
            return None
        node_off = self.MARKET_FIXED_SIZE + idx
        price_off = node_off + self.RBNODE_HEADER_SIZE
        if price_off + self.PRICE_LEN > len(data):
            return None
        u128_inner = int.from_bytes(data[price_off:price_off + self.PRICE_LEN], 'little')
        if u128_inner <= 0:
            return None
        atomic_ratio = u128_inner / self.PRICE_DIVISOR
        return atomic_ratio * (10 ** (base_dec - quote_dec))

    def decode(self, pubkey: str, data: bytes, ctx: PriceContext
               ) -> Optional[Dict[str, float]]:
        if pubkey != self.pool_address or len(data) < self.MARKET_FIXED_SIZE:
            return None
        if int.from_bytes(data[0:8], 'little') != self.DISCRIMINANT:
            return None

        on_chain_base = _b58(data[self.BASE_MINT_OFFSET:self.BASE_MINT_OFFSET + 32])
        on_chain_quote = _b58(data[self.QUOTE_MINT_OFFSET:self.QUOTE_MINT_OFFSET + 32])
        if frozenset({on_chain_base, on_chain_quote}) != self.expected_mints:
            logger.warning(f"{self.label()} mint set mismatch: account says "
                           f"{on_chain_base[:6]}/{on_chain_quote[:6]}")
            return None

        base_dec = data[self.BASE_DEC_OFFSET]
        quote_dec = data[self.QUOTE_DEC_OFFSET]

        bids_best = int.from_bytes(
            data[self.BIDS_BEST_OFFSET:self.BIDS_BEST_OFFSET + 4], 'little')
        asks_best = int.from_bytes(
            data[self.ASKS_BEST_OFFSET:self.ASKS_BEST_OFFSET + 4], 'little')

        bid = self._read_order_price(data, bids_best, base_dec, quote_dec)
        ask = self._read_order_price(data, asks_best, base_dec, quote_dec)

        if bid is None and ask is None:
            return None
        if bid is None:
            mid = ask
        elif ask is None:
            mid = bid
        else:
            mid = (bid + ask) / 2.0

        # mid is (UI quote per UI base) — emit using the on-chain orientation
        # (base_mint = first arg, quote_mint = second).
        return _emit_from_b_per_a(on_chain_base, on_chain_quote, mid, ctx)


class MeteoraDammDecoder:
    """Meteora DAMM v1 (program Eo7WjKq67...) — Stable curve with Depeg.

    All Meteora DAMM v1 LST/SOL pools are CurveType::Stable with a Depeg
    component. The constant-product `b_atoms / a_atoms` formula does NOT
    apply: with high amp (e.g. 1000), a stable pool can hold massively
    imbalanced reserves while still trading near the depeg ratio. The actual
    marginal price is encoded directly in `depeg.base_virtual_price` (u64
    inside the CurveType::Stable variant), which the program updates from the
    underlying stake-pool's exchange rate on every swap.

    For edgeSOL/SOL (depeg_type=3, SPL stake-pool depeg) the value is scaled
    by 10^6: `bvp = 1_276_092` ⇒ 1.276092 SOL per edgeSOL — verified against
    Jupiter ($107.38 / $84.44 = 1.272, error ~30 bps which is the venue
    spread).

    Pool layout (Anchor-encoded, 944 bytes):
        0..8      discriminator [241,154,109,4,17,177,109,188]
        40..72    token_a_mint (32)
        72..104   token_b_mint (32)
        ... (pubkeys, fees, padding) ...
        874       curve_type tag (0=ConstantProduct, 1=Stable)
        875..883  amp (u64)
        883..891  token_a_multiplier (u64)
        891..899  token_b_multiplier (u64)
        899       precision_factor (u8)
        900..908  depeg.base_virtual_price (u64 LE)   ← what we read
        908..916  depeg.base_cache_updated (u64)
        916       depeg.depeg_type (u8 enum)
        917..925  last_amp_updated_timestamp (u64)

    Bootstrap is done off the same pool account (validates discriminator,
    curve_type=Stable, mint set) and just records the mint orientation.
    No vault-side accounts needed — single subscription per pool.
    """
    DISCRIMINATOR = bytes([241, 154, 109, 4, 17, 177, 109, 188])
    POOL_TOKEN_A_MINT_OFFSET = 40
    POOL_TOKEN_B_MINT_OFFSET = 72
    POOL_MIN_LEN = 944
    CURVE_TYPE_OFFSET = 874
    BVP_OFFSET = 900
    BVP_LEN = 8
    BVP_SCALE = 10 ** 6              # SPL-stake-pool depeg scaling

    def __init__(self, pool: PoolInfo):
        self.pool_address = pool.pool_address
        self.expected_mints = frozenset({pool.token_a_mint, pool.token_b_mint})
        # Set by bootstrap():
        self._token_a_mint: Optional[str] = None
        self._token_b_mint: Optional[str] = None
        self._bootstrapped = False

    def label(self) -> str:
        a, b = sorted(self.expected_mints)
        return f"MeteoraDAMM({self.pool_address[:6]}.. {a[:4]}/{b[:4]})"

    def accounts(self) -> List[str]:
        if not self._bootstrapped:
            return []
        return [self.pool_address]

    def _rpc_get_one(self, rpc_url: str, addr: str) -> Optional[bytes]:
        import base64
        import json as _json
        import urllib.request as _ur
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [addr, {"encoding": "base64", "commitment": "processed"}],
        }
        req = _ur.Request(
            rpc_url, data=_json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=20) as resp:
            d = _json.loads(resp.read())
        v = d.get("result", {}).get("value")
        if not v:
            return None
        return base64.b64decode(v["data"][0])

    def bootstrap(self, rpc_url: str):
        """Read pool once to validate layout (discriminator + curve_type=Stable
        + matching mints) and capture on-chain mint orientation. Raises on
        failure — caller skips this pool."""
        pool_data = self._rpc_get_one(rpc_url, self.pool_address)
        if pool_data is None or len(pool_data) < self.POOL_MIN_LEN:
            raise RuntimeError("pool account missing or too short")
        if pool_data[:8] != self.DISCRIMINATOR:
            raise RuntimeError("DAMM v1 discriminator mismatch")
        ta = _b58(pool_data[self.POOL_TOKEN_A_MINT_OFFSET:
                            self.POOL_TOKEN_A_MINT_OFFSET + 32])
        tb = _b58(pool_data[self.POOL_TOKEN_B_MINT_OFFSET:
                            self.POOL_TOKEN_B_MINT_OFFSET + 32])
        if frozenset({ta, tb}) != self.expected_mints:
            raise RuntimeError(
                f"pool mints {ta[:6]}/{tb[:6]} do not match registry")
        curve_tag = pool_data[self.CURVE_TYPE_OFFSET]
        if curve_tag != 1:
            raise RuntimeError(
                f"unsupported curve_type tag {curve_tag} (expected 1=Stable)")
        self._token_a_mint = ta
        self._token_b_mint = tb
        self._bootstrapped = True
        logger.info(f"{self.label()} bootstrapped: Stable + Depeg, "
                    f"orientation a={ta[:6]}.. b={tb[:6]}..")

    def _decode_price(self, data: bytes, ctx: PriceContext
                      ) -> Optional[Dict[str, float]]:
        bvp = int.from_bytes(
            data[self.BVP_OFFSET:self.BVP_OFFSET + self.BVP_LEN], 'little')
        if bvp <= 0:
            return None
        # bvp = (UI quote per UI base) × 10^6, where the staking-yield side
        # is the "depeg" token. By convention base_virtual_price tracks
        # "1 LST = X SOL", so it's a_per_b when token_a=SOL OR b_per_a when
        # token_a=LST. Apply the right interpretation.
        sol_per_lst = bvp / self.BVP_SCALE
        if self._token_a_mint == SOL_MINT:
            # SOL is A, LST is B → bvp tells us SOL_per_LST = a_per_b
            # → b_per_a = LST_per_SOL = 1 / sol_per_lst
            b_per_a = 1.0 / sol_per_lst
        elif self._token_b_mint == SOL_MINT:
            # LST is A, SOL is B → bvp tells us SOL_per_LST = b_per_a
            b_per_a = sol_per_lst
        else:
            # Non-SOL pair — fall back to b_per_a = sol_per_lst (best guess)
            b_per_a = sol_per_lst
        return _emit_from_b_per_a(self._token_a_mint, self._token_b_mint,
                                  b_per_a, ctx)

    def decode(self, pubkey: str, data: bytes, ctx: PriceContext
               ) -> Optional[Dict[str, float]]:
        if not self._bootstrapped or pubkey != self.pool_address:
            return None
        if len(data) < self.POOL_MIN_LEN:
            return None
        if data[:8] != self.DISCRIMINATOR:
            return None
        return self._decode_price(data, ctx)

    # Helper for one-shot validators / bootstrap-time price computation.
    def _compute(self, ctx: PriceContext, *, rpc_url: Optional[str] = None,
                 data: Optional[bytes] = None) -> Optional[Dict[str, float]]:
        if data is None:
            if rpc_url is None:
                return None
            data = self._rpc_get_one(rpc_url, self.pool_address)
            if data is None or len(data) < self.POOL_MIN_LEN:
                return None
        return self._decode_price(data, ctx)


# ---------------------------------------------------------------------------
# Decoder factory — maps DEX strings → decoder classes.
# Adding a new DEX is one entry here plus a new decoder class above.
# ---------------------------------------------------------------------------

DECODER_FACTORIES: Dict[str, Callable[[PoolInfo], object]] = {
    DEX_WHIRLPOOL: WhirlpoolDecoder,
    DEX_RAYDIUM_CLMM: RaydiumClmmDecoder,
    DEX_METEORA: MeteoraDlmmDecoder,
    DEX_MANIFEST: ManifestDecoder,
    DEX_DAMM: MeteoraDammDecoder,
}


# ---------------------------------------------------------------------------
# PoolPriceFeed — owns the gRPC stream and dispatches account updates to the
# right decoder. Yields {mint: usd_price} dicts on every successful decode.
# ---------------------------------------------------------------------------

class PoolPriceFeed:
    def __init__(self, config, mints: Set[str], exclude_dexes: Optional[Set[str]] = None):
        self.config = config
        self.endpoint = getattr(config, 'grpc_endpoint', '') or ''
        self.token = getattr(config, 'grpc_token', '') or ''
        self.context = PriceContext()
        exclude_dexes = exclude_dexes or set()

        wanted = set(mints) | {SOL_MINT}
        rpc_url = getattr(config, 'rpc_url', '') or ''
        # One decoder per pool; pubkey → decoder for fast dispatch.
        self.decoders: List[object] = []
        self.by_account: Dict[str, object] = {}
        for info in POOL_REGISTRY.values():
            if info.dex in exclude_dexes:
                continue
            factory = DECODER_FACTORIES.get(info.dex)
            if factory is None or not info.pool_address:
                continue
            if info.token_a_mint not in wanted or info.token_b_mint not in wanted:
                continue
            decoder = factory(info)
            # Some decoders need an RPC bootstrap (e.g. MeteoraDammDecoder must
            # discover vault PDAs and seed initial state). Skip the pool if
            # bootstrap fails — Jupiter polling still covers it.
            if hasattr(decoder, 'bootstrap'):
                if not rpc_url:
                    logger.warning(
                        f"{decoder.label()} requires RPC bootstrap but rpc_url "
                        f"is empty — skipping this pool"
                    )
                    continue
                try:
                    decoder.bootstrap(rpc_url)
                except Exception as e:
                    logger.warning(f"{decoder.label()} bootstrap failed: {e} "
                                   f"— skipping this pool")
                    continue
            self.decoders.append(decoder)
            for acct in decoder.accounts():
                self.by_account[acct] = decoder

        if self.decoders:
            counts: Dict[str, int] = {}
            for d in self.decoders:
                counts[type(d).__name__] = counts.get(type(d).__name__, 0) + 1
            summary = ", ".join(f"{n}={c}" for n, c in sorted(counts.items()))
            logger.info(f"PoolPriceFeed: {len(self.decoders)} pool(s) [{summary}]")

    def feed_external(self, prices: Dict[str, float]):
        """Merge external (Jupiter) prices into the shared context so cross-LST
        pools can derive USD via the side that has a known price."""
        self.context.feed_external(prices)

    @property
    def sol_usd_price(self) -> float:
        return self.context.sol_usd_price

    def _create_channel(self):
        if 'localhost' in self.endpoint or ':443' not in self.endpoint:
            return grpc.aio.insecure_channel(self.endpoint, options=CHANNEL_OPTIONS)
        return grpc.aio.secure_channel(
            self.endpoint, grpc.ssl_channel_credentials(), options=CHANNEL_OPTIONS,
        )

    def _metadata(self):
        return [('x-token', self.token)] if self.token else []

    def _build_request(self) -> geyser_pb2.SubscribeRequest:
        request = geyser_pb2.SubscribeRequest(
            commitment=geyser_pb2.CommitmentLevel.PROCESSED,
        )
        accounts_filter = request.accounts["pool_accounts"]
        accounts_filter.account.extend(self.by_account.keys())
        return request

    async def stream(self) -> AsyncGenerator[Dict[str, float], None]:
        if not self.decoders:
            logger.warning("PoolPriceFeed: no matching pools — feed disabled")
            return
        if not self.endpoint:
            logger.warning("PoolPriceFeed: GRPC_ENDPOINT not set — feed disabled")
            return

        retry = 0
        while True:
            channel = self._create_channel()
            try:
                stub = geyser_pb2_grpc.GeyserStub(channel)
                metadata = self._metadata()
                version = await stub.GetVersion(
                    geyser_pb2.GetVersionRequest(), metadata=metadata)
                logger.info(
                    f"PoolPriceFeed: connected to Geyser v{version.version}, "
                    f"subscribing to {len(self.by_account)} pool account(s)"
                )
                retry = 0

                request = self._build_request()
                stream = stub.Subscribe(iter([request]), metadata=metadata)
                async for update in stream:
                    if not update.HasField('account'):
                        continue
                    info = update.account.account
                    pubkey = _b58(info.pubkey)
                    decoder = self.by_account.get(pubkey)
                    if decoder is None:
                        continue
                    try:
                        out = decoder.decode(pubkey, info.data, self.context)
                    except Exception as e:
                        logger.warning(f"{decoder.label()} decode error: {e}")
                        continue
                    if out:
                        yield out
            except grpc.aio.AioRpcError as e:
                retry += 1
                wait = min(retry * 2, 30)
                logger.error(
                    f"PoolPriceFeed gRPC error: {e.code()} {e.details()}, "
                    f"retry in {wait}s ({retry})"
                )
                await asyncio.sleep(wait)
            except Exception as e:
                retry += 1
                logger.error(f"PoolPriceFeed error: {e}, retry in 5s ({retry})")
                await asyncio.sleep(5)
            finally:
                try:
                    await channel.close()
                except Exception:
                    pass


# Backward-compat alias — older callers import WhirlpoolPriceFeed.
WhirlpoolPriceFeed = PoolPriceFeed


# MergedPriceFeed (Jupiter + Pool multiplex) removed — makerator does not
# use Jupiter polling. Use PoolPriceFeed directly via its `stream()` method.
