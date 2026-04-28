"""
Direct DEX pool swaps — bypass Jupiter aggregator.

Builds and signs swap transactions directly against on-chain pool programs
for known token/SOL pairs. Falls back to Jupiter (via executor) when no
direct pool is registered.

Supported DEXes: Whirlpool, Meteora DLMM, Meteora DAMM, Manifest, PancakeSwap CLMM, AlphaQ, Raydium CLMM.

Usage:
    from direct_swap import has_direct_pool, build_direct_swap_tx

    if has_direct_pool(input_mint, output_mint):
        signed_tx_b64, sig_str = build_direct_swap_tx(
            input_mint, output_mint, amount_raw,
            signer_keypair, rpc_endpoint, priority_fee_lamports
        )
"""

import struct
import json
import math
import base64
import logging
import urllib.request
from hashlib import sha256
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.message import Message
from solders.transaction import Transaction
from solders.hash import Hash

from constants import (
    SOL_MINT, MSOL_MINT, STSOL_MINT, INF_MINT, BSOL_MINT,
    JITOSOL_MINT, JUPSOL_MINT, JUP_MINT, ETH_MINT,
    VSOL_MINT, DSOL_MINT, EDGESOL_MINT, BONKSOL_MINT,
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID, SYSTEM_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    WHIRLPOOL_PROGRAM, METEORA_DLMM_PROGRAM,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Program IDs
# ---------------------------------------------------------------------------
MANIFEST_PROGRAM = "MNFSTqtC93rEfYHB6hF82sKdZpUDFWkViLByLd1k1Ms"
PANCAKESWAP_CLMM_PROGRAM = "HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq"
ALPHAQ_PROGRAM = "ALPHAQmeA7bjrVuccPsYPiCvsi428SNwte66Srvs4pHA"
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
METEORA_DAMM_PROGRAM = "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB"
METEORA_DAMM_VAULT_PROGRAM = "24Uqj9JCLxUeoC3hGfh5W3s9FM9uCHDS2SG3LYwBpyTi"

# ---------------------------------------------------------------------------
# Pool Registry
# ---------------------------------------------------------------------------
# DEX types for dispatch
DEX_WHIRLPOOL = "whirlpool"
DEX_METEORA = "meteora_dlmm"
DEX_MANIFEST = "manifest"
DEX_PANCAKESWAP = "pancakeswap_clmm"
DEX_ALPHAQ = "alphaq"
DEX_RAYDIUM_CLMM = "raydium_clmm"
DEX_DAMM = "meteora_damm"


@dataclass
class PoolInfo:
    pool_address: str
    dex: str
    token_a_mint: str  # pool's canonical token A
    token_b_mint: str  # pool's canonical token B
    enabled: bool = True
    notes: str = ""
    cost_bps: float = 6.0  # measured price impact in bps (default conservative)


# Canonical key: frozenset({mint_a, mint_b}) -> PoolInfo
# Pool addresses from user specification. IMPORTANT: these need on-chain
# verification via getAccountInfo before live use.
POOL_REGISTRY: Dict[frozenset, PoolInfo] = {
    # === VERIFIED FROM JUPITER ROUTING (2026-04-19) ===
    # bSOL/SOL — Whirlpool
    frozenset({BSOL_MINT, SOL_MINT}): PoolInfo(
        pool_address="8phK65jxmTPEN158xLgSr4oZvssw9SyTErpNZj3g7px4",
        dex=DEX_WHIRLPOOL, token_a_mint=BSOL_MINT, token_b_mint=SOL_MINT,
        cost_bps=2.0,
    ),
    # INF/SOL — Whirlpool
    frozenset({INF_MINT, SOL_MINT}): PoolInfo(
        pool_address="DxD41srN8Xk9QfYjdNXF9tTnP6qQxeF2bZF8s1eN62Pe",
        dex=DEX_WHIRLPOOL, token_a_mint=INF_MINT, token_b_mint=SOL_MINT,
        cost_bps=2.0,
    ),
    # jupSOL/SOL — Manifest (Jupiter routes here, NOT AlphaQ)
    frozenset({JUPSOL_MINT, SOL_MINT}): PoolInfo(
        pool_address="8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv",
        dex=DEX_MANIFEST, token_a_mint=JUPSOL_MINT, token_b_mint=SOL_MINT,
        cost_bps=2.0,
    ),
    # jitoSOL/SOL — Manifest (simulation PASSED)
    frozenset({JITOSOL_MINT, SOL_MINT}): PoolInfo(
        pool_address="7ecvmhGKVcK4SgxeGQJG6yVwVAhbQxLrBuaMoUmpRZ6i",
        dex=DEX_MANIFEST, token_a_mint=JITOSOL_MINT, token_b_mint=SOL_MINT,
        cost_bps=2.0,
    ),
    # ETH/SOL — PancakeSwap CLMM (tick_spacing=1)
    frozenset({ETH_MINT, SOL_MINT}): PoolInfo(
        pool_address="3nLR2KnqC4ZHmPbjJgZCmfKazPjyxZEeqQv77sctcs4P",
        dex=DEX_PANCAKESWAP, token_a_mint=SOL_MINT, token_b_mint=ETH_MINT,
        cost_bps=10.0,
    ),
    # mSOL/SOL — AlphaQ
    frozenset({MSOL_MINT, SOL_MINT}): PoolInfo(
        pool_address="DbTYuFpdELAgyZBhX7TaTVDGYq1dJSQqdnAhtWHjxPjP",
        dex=DEX_ALPHAQ, token_a_mint=MSOL_MINT, token_b_mint=SOL_MINT,
        cost_bps=3.0,
    ),
    # === LST/LST DIRECT POOLS (token-to-token, no SOL intermediary) ===
    # bSOL/mSOL — Whirlpool (1.5 bps impact)
    frozenset({BSOL_MINT, MSOL_MINT}): PoolInfo(
        pool_address="CwZbEdMZdxjnPLcRGRz8PwuvA4tK4iBmS9YZrMvnrNJr",
        dex=DEX_WHIRLPOOL, token_a_mint=BSOL_MINT, token_b_mint=MSOL_MINT,
        cost_bps=1.5,
    ),
    # jitoSOL/bSOL — Whirlpool (1.6 bps impact)
    frozenset({JITOSOL_MINT, BSOL_MINT}): PoolInfo(
        pool_address="5snaYowgJDfuM1LPbTNUYHbgkKHtVVnzHiiLDWUV2hh8",
        dex=DEX_WHIRLPOOL, token_a_mint=JITOSOL_MINT, token_b_mint=BSOL_MINT,
        cost_bps=1.6,
    ),
    # mSOL/jitoSOL — Whirlpool (0.0 bps impact)
    frozenset({MSOL_MINT, JITOSOL_MINT}): PoolInfo(
        pool_address="HZsTF6VHdQy2W6cfEEqqpoTFKocx7Ch5c4TnWucXkAYv",
        dex=DEX_WHIRLPOOL, token_a_mint=MSOL_MINT, token_b_mint=JITOSOL_MINT,
        cost_bps=0.5,
    ),
    # jupSOL/jitoSOL — Whirlpool (1.2 bps impact)
    frozenset({JUPSOL_MINT, JITOSOL_MINT}): PoolInfo(
        pool_address="D2G7dVp1tSsxKx9hs4PLvG5nECszWLg5A1YzahmFx8Pp",
        dex=DEX_WHIRLPOOL, token_a_mint=JUPSOL_MINT, token_b_mint=JITOSOL_MINT,
        cost_bps=1.2,
    ),
    # mSOL/jupSOL — Raydium CLMM (0.9 bps impact)
    frozenset({MSOL_MINT, JUPSOL_MINT}): PoolInfo(
        pool_address="FJTS7LvcyDLCbFkSUB4P3MumRJrZNPWA3gvts9W9MEWh",
        dex=DEX_RAYDIUM_CLMM, token_a_mint=MSOL_MINT, token_b_mint=JUPSOL_MINT,
        cost_bps=0.9,
    ),
    # INF/jupSOL — Meteora DLMM (2.6 bps impact)
    frozenset({INF_MINT, JUPSOL_MINT}): PoolInfo(
        pool_address="H1tk6dsTZniLopJPnHnNQ4L2PDSp7he1vS7sTLeNLZMo",
        dex=DEX_METEORA, token_a_mint=INF_MINT, token_b_mint=JUPSOL_MINT,
        cost_bps=2.6,
    ),
    # === NEW SANCTUM LSTs (2026-04-26) ===
    # vSOL/SOL — Manifest (0.2 bps — may error 8 when pool SOL is low, falls back to Jupiter)
    frozenset({VSOL_MINT, SOL_MINT}): PoolInfo(
        pool_address="2jieFDtgChGY76j62EfpNCRGKLGwSicRJndWWe4UobXa",
        dex=DEX_MANIFEST, token_a_mint=VSOL_MINT, token_b_mint=SOL_MINT,
        cost_bps=0.2,
    ),
    # dSOL/SOL — Manifest (1.0 bps impact)
    frozenset({DSOL_MINT, SOL_MINT}): PoolInfo(
        pool_address="2eu49YAcLy6BNyi9zoiCnuPotaeufu2HrDuHNWEqeGuT",
        dex=DEX_MANIFEST, token_a_mint=DSOL_MINT, token_b_mint=SOL_MINT,
        cost_bps=1.0,
    ),
    # edgeSOL/SOL — Meteora DAMM (NOT DLMM — different program Eo7WjKq67...)
    frozenset({EDGESOL_MINT, SOL_MINT}): PoolInfo(
        pool_address="7AtUeAW4TKPEXkR41bawBnwyemKXL4pCPrWP5tXcPMSA",
        dex=DEX_DAMM, token_a_mint=EDGESOL_MINT, token_b_mint=SOL_MINT,
        cost_bps=2.1,
    ),
    # bonkSOL/SOL — Manifest (3.5 bps impact)
    frozenset({BONKSOL_MINT, SOL_MINT}): PoolInfo(
        pool_address="E6ErbWTKJG5GghniWakNogYCNmw8SkMCmrop94iJT7tg",
        dex=DEX_MANIFEST, token_a_mint=BONKSOL_MINT, token_b_mint=SOL_MINT,
        cost_bps=3.5,
    ),
    # INF/bonkSOL — Whirlpool (1.9 bps impact)
    frozenset({INF_MINT, BONKSOL_MINT}): PoolInfo(
        pool_address="8fTQVevKVqT5k7wWirsg6dvgxY4ffTdXQbMvZ8cQMBzb",
        dex=DEX_WHIRLPOOL, token_a_mint=INF_MINT, token_b_mint=BONKSOL_MINT,
        cost_bps=1.9,
    ),
    # === JUPITER MULTI-HOP PAIRS (no direct pool, ~10 bps estimated real cost) ===
    # Jupiter routes through 2-3 cheap hops. Quotes show 0-2 bps but real cost ~10 bps.
    frozenset({BSOL_MINT, JUPSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=BSOL_MINT, token_b_mint=JUPSOL_MINT, cost_bps=10.0),
    frozenset({BSOL_MINT, INF_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=BSOL_MINT, token_b_mint=INF_MINT, cost_bps=10.0),
    frozenset({BSOL_MINT, VSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=BSOL_MINT, token_b_mint=VSOL_MINT, cost_bps=10.0),
    frozenset({BSOL_MINT, DSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=BSOL_MINT, token_b_mint=DSOL_MINT, cost_bps=10.0),
    frozenset({BSOL_MINT, EDGESOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=BSOL_MINT, token_b_mint=EDGESOL_MINT, cost_bps=10.0),
    frozenset({BSOL_MINT, BONKSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=BSOL_MINT, token_b_mint=BONKSOL_MINT, cost_bps=10.0),
    frozenset({MSOL_MINT, INF_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=MSOL_MINT, token_b_mint=INF_MINT, cost_bps=10.0),
    frozenset({MSOL_MINT, VSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=MSOL_MINT, token_b_mint=VSOL_MINT, cost_bps=10.0),
    frozenset({MSOL_MINT, DSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=MSOL_MINT, token_b_mint=DSOL_MINT, cost_bps=10.0),
    frozenset({MSOL_MINT, EDGESOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=MSOL_MINT, token_b_mint=EDGESOL_MINT, cost_bps=10.0),
    frozenset({MSOL_MINT, BONKSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=MSOL_MINT, token_b_mint=BONKSOL_MINT, cost_bps=10.0),
    frozenset({JITOSOL_MINT, INF_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JITOSOL_MINT, token_b_mint=INF_MINT, cost_bps=10.0),
    frozenset({JITOSOL_MINT, VSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JITOSOL_MINT, token_b_mint=VSOL_MINT, cost_bps=10.0),
    frozenset({JITOSOL_MINT, DSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JITOSOL_MINT, token_b_mint=DSOL_MINT, cost_bps=10.0),
    frozenset({JITOSOL_MINT, EDGESOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JITOSOL_MINT, token_b_mint=EDGESOL_MINT, cost_bps=10.0),
    frozenset({JITOSOL_MINT, BONKSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JITOSOL_MINT, token_b_mint=BONKSOL_MINT, cost_bps=10.0),
    frozenset({JUPSOL_MINT, VSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JUPSOL_MINT, token_b_mint=VSOL_MINT, cost_bps=10.0),
    frozenset({JUPSOL_MINT, DSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JUPSOL_MINT, token_b_mint=DSOL_MINT, cost_bps=10.0),
    frozenset({JUPSOL_MINT, EDGESOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JUPSOL_MINT, token_b_mint=EDGESOL_MINT, cost_bps=10.0),
    frozenset({JUPSOL_MINT, BONKSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=JUPSOL_MINT, token_b_mint=BONKSOL_MINT, cost_bps=10.0),
    frozenset({INF_MINT, VSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=INF_MINT, token_b_mint=VSOL_MINT, cost_bps=10.0),
    frozenset({INF_MINT, DSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=INF_MINT, token_b_mint=DSOL_MINT, cost_bps=10.0),
    frozenset({INF_MINT, EDGESOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=INF_MINT, token_b_mint=EDGESOL_MINT, cost_bps=10.0),
    frozenset({VSOL_MINT, DSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=VSOL_MINT, token_b_mint=DSOL_MINT, cost_bps=10.0),
    frozenset({VSOL_MINT, EDGESOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=VSOL_MINT, token_b_mint=EDGESOL_MINT, cost_bps=10.0),
    frozenset({VSOL_MINT, BONKSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=VSOL_MINT, token_b_mint=BONKSOL_MINT, cost_bps=10.0),
    frozenset({DSOL_MINT, EDGESOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=DSOL_MINT, token_b_mint=EDGESOL_MINT, cost_bps=10.0),
    frozenset({DSOL_MINT, BONKSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=DSOL_MINT, token_b_mint=BONKSOL_MINT, cost_bps=10.0),
    frozenset({EDGESOL_MINT, BONKSOL_MINT}): PoolInfo(
        pool_address="", dex="jupiter", token_a_mint=EDGESOL_MINT, token_b_mint=BONKSOL_MINT, cost_bps=10.0),
}


# ---------------------------------------------------------------------------
# Whirlpool constants
# ---------------------------------------------------------------------------
WHIRLPOOL_MIN_SQRT_PRICE = 4295048016
WHIRLPOOL_MAX_SQRT_PRICE = 79226673515401279992447579055


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_associated_token_address(
    owner: Pubkey, mint: Pubkey, token_program: Pubkey = None
) -> Pubkey:
    """Derive ATA address (mirrors arbito/arbitrage_utils.py)."""
    if token_program is None:
        token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID),
    )
    return pda


def _rpc_call(rpc_endpoint: str, method: str, params: list) -> dict:
    """Synchronous JSON-RPC call."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params,
    }).encode()
    req = urllib.request.Request(
        rpc_endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_pool_state(pool_address: str, rpc_endpoint: str) -> Optional[bytes]:
    """Fetch raw account data for a pool address. Returns decoded bytes or None."""
    result = _rpc_call(rpc_endpoint, "getAccountInfo", [
        pool_address,
        {"encoding": "base64", "commitment": "confirmed"},
    ])
    value = result.get("result", {}).get("value")
    if value is None:
        return None
    data_b64 = value["data"][0]
    return base64.b64decode(data_b64)


def fetch_account_owner(pool_address: str, rpc_endpoint: str) -> Optional[str]:
    """Fetch the owner program of an account. Returns owner pubkey string or None."""
    result = _rpc_call(rpc_endpoint, "getAccountInfo", [
        pool_address,
        {"encoding": "base64", "commitment": "confirmed"},
    ])
    value = result.get("result", {}).get("value")
    if value is None:
        return None
    return value.get("owner")


def _get_recent_blockhash(rpc_endpoint: str) -> str:
    """Fetch a recent blockhash for transaction building."""
    result = _rpc_call(rpc_endpoint, "getLatestBlockhash", [
        {"commitment": "confirmed"},
    ])
    return result["result"]["value"]["blockhash"]


def _build_compute_budget_ixs(
    cu_limit: int, priority_fee_lamports: int
) -> list:
    """Build compute budget instructions (CU limit + priority fee)."""
    program = Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID)
    ixs = []
    # SetComputeUnitLimit
    ixs.append(Instruction(
        program_id=program,
        accounts=[],
        data=struct.pack('<BI', 2, cu_limit),
    ))
    # SetComputeUnitPrice (micro-lamports per CU)
    # Convert lamports to micro-lamports-per-CU: fee_lamports / cu_limit * 1e6
    micro_lamports_per_cu = max(1, (priority_fee_lamports * 1_000_000) // cu_limit)
    ixs.append(Instruction(
        program_id=program,
        accounts=[],
        data=struct.pack('<BQ', 3, micro_lamports_per_cu),
    ))
    return ixs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def has_direct_pool(input_mint: str, output_mint: str) -> bool:
    """Check if a direct (non-Jupiter) pool exists for this pair."""
    key = frozenset({input_mint, output_mint})
    pool = POOL_REGISTRY.get(key)
    return pool is not None and pool.enabled


def get_pool_info(input_mint: str, output_mint: str) -> Optional[PoolInfo]:
    """Get pool info for a pair, or None."""
    key = frozenset({input_mint, output_mint})
    pool = POOL_REGISTRY.get(key)
    if pool is not None and pool.enabled:
        return pool
    return None


def build_direct_swap_tx(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
    priority_fee_lamports: int = 10_000,
) -> Tuple[str, str]:
    """
    Build a complete signed transaction for a direct pool swap.

    Returns (signed_tx_base64, signature_string).
    Raises RuntimeError on failure.
    """
    pool = get_pool_info(input_mint, output_mint)
    if pool is None:
        raise RuntimeError(f"No direct pool for {input_mint[:8]}../{output_mint[:8]}..")

    # Determine swap direction relative to pool's canonical token ordering
    a_to_b = (input_mint == pool.token_a_mint)

    # Build the DEX-specific swap instruction
    # NOTE: each builder receives a_to_b as a hint but may recompute direction
    # from on-chain state to handle registry/on-chain ordering mismatches.
    if pool.dex == DEX_WHIRLPOOL:
        swap_ix = _build_whirlpool_swap_ix(
            pool, input_mint, amount_raw, signer_keypair, rpc_endpoint
        )
    elif pool.dex == DEX_METEORA:
        swap_ix = _build_meteora_swap_ix(
            pool, a_to_b, amount_raw, signer_keypair, rpc_endpoint
        )
    elif pool.dex == DEX_MANIFEST:
        swap_ix = _build_manifest_swap_ix(
            pool, a_to_b, amount_raw, signer_keypair, rpc_endpoint
        )
    elif pool.dex == DEX_PANCAKESWAP:
        swap_ix = _build_pancakeswap_swap_ix(
            pool, input_mint, amount_raw, signer_keypair, rpc_endpoint
        )
    elif pool.dex == DEX_ALPHAQ:
        swap_ix = _build_alphaq_swap_ix(
            pool, a_to_b, amount_raw, signer_keypair, rpc_endpoint
        )
    elif pool.dex == DEX_RAYDIUM_CLMM:
        swap_ix = _build_raydium_clmm_swap_ix(
            pool, input_mint, amount_raw, signer_keypair, rpc_endpoint
        )
    elif pool.dex == DEX_DAMM:
        swap_ix = _build_damm_swap_ix(
            pool, a_to_b, amount_raw, signer_keypair, rpc_endpoint
        )
    else:
        # Unsupported DEX — return None to fall back to Jupiter silently
        return None

    if swap_ix is None:
        raise RuntimeError(f"Failed to build swap ix for {pool.dex} pool {pool.pool_address}")

    # Assemble full transaction: compute budget + SOL wrapping + swap + SOL unwrapping
    cu_limit = 400_000  # generous for single-pool swap
    budget_ixs = _build_compute_budget_ixs(cu_limit, priority_fee_lamports)

    signer_pubkey = signer_keypair.pubkey()
    sol_mint = Pubkey.from_string(SOL_MINT)
    wsol_ata = get_associated_token_address(signer_pubkey, sol_mint)
    token_program = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    ata_program = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
    system_program = Pubkey.from_string("11111111111111111111111111111111")

    pre_ixs = []
    post_ixs = []

    # Create wSOL ATA if needed (idempotent — won't fail if already exists)
    if input_mint == SOL_MINT or output_mint == SOL_MINT:
        # CreateAssociatedTokenAccountIdempotent
        create_ata_ix = Instruction(
            ata_program,
            bytes([1]),  # CreateIdempotent discriminator
            [
                AccountMeta(signer_pubkey, is_signer=True, is_writable=True),
                AccountMeta(wsol_ata, is_signer=False, is_writable=True),
                AccountMeta(signer_pubkey, is_signer=False, is_writable=False),
                AccountMeta(sol_mint, is_signer=False, is_writable=False),
                AccountMeta(system_program, is_signer=False, is_writable=False),
                AccountMeta(token_program, is_signer=False, is_writable=False),
            ],
        )
        pre_ixs.append(create_ata_ix)

    if input_mint == SOL_MINT:
        # Transfer native SOL to wSOL ATA
        transfer_data = struct.pack('<IQ', 2, amount_raw)  # SystemProgram::Transfer
        transfer_ix = Instruction(
            system_program,
            transfer_data,
            [
                AccountMeta(signer_pubkey, is_signer=True, is_writable=True),
                AccountMeta(wsol_ata, is_signer=False, is_writable=True),
            ],
        )
        pre_ixs.append(transfer_ix)

        # SyncNative on wSOL ATA
        sync_ix = Instruction(
            token_program,
            bytes([17]),  # SyncNative instruction index
            [AccountMeta(wsol_ata, is_signer=False, is_writable=True)],
        )
        pre_ixs.append(sync_ix)

    if output_mint == SOL_MINT or input_mint == SOL_MINT:
        # Close wSOL ATA after swap to recover SOL
        close_data = bytes([9])  # CloseAccount instruction index
        close_ix = Instruction(
            token_program,
            close_data,
            [
                AccountMeta(wsol_ata, is_signer=False, is_writable=True),
                AccountMeta(signer_pubkey, is_signer=False, is_writable=True),
                AccountMeta(signer_pubkey, is_signer=True, is_writable=False),
            ],
        )
        post_ixs.append(close_ix)

    # Also ensure the non-SOL token ATA exists
    other_mint_str = output_mint if input_mint == SOL_MINT else input_mint
    if other_mint_str != SOL_MINT:
        other_mint = Pubkey.from_string(other_mint_str)
        other_ata = get_associated_token_address(signer_pubkey, other_mint)
        create_other_ix = Instruction(
            ata_program,
            bytes([1]),
            [
                AccountMeta(signer_pubkey, is_signer=True, is_writable=True),
                AccountMeta(other_ata, is_signer=False, is_writable=True),
                AccountMeta(signer_pubkey, is_signer=False, is_writable=False),
                AccountMeta(other_mint, is_signer=False, is_writable=False),
                AccountMeta(system_program, is_signer=False, is_writable=False),
                AccountMeta(token_program, is_signer=False, is_writable=False),
            ],
        )
        pre_ixs.append(create_other_ix)

    all_ixs = budget_ixs + pre_ixs + [swap_ix] + post_ixs

    # Get recent blockhash and build + sign transaction
    blockhash_str = _get_recent_blockhash(rpc_endpoint)
    blockhash = Hash.from_string(blockhash_str)

    msg = Message.new_with_blockhash(all_ixs, signer_keypair.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([signer_keypair], blockhash)

    tx_bytes = bytes(tx)
    signed_b64 = base64.b64encode(tx_bytes).decode()
    sig_str = str(tx.signatures[0])

    logger.info(f"Direct swap tx built: {pool.dex} pool={pool.pool_address[:12]}.. "
                f"{'A->B' if a_to_b else 'B->A'} amount={amount_raw} sig={sig_str[:16]}..")
    return signed_b64, sig_str


# ---------------------------------------------------------------------------
# Whirlpool swap instruction builder
# ---------------------------------------------------------------------------

def _get_tick_array_address(whirlpool_address: str, start_tick: int) -> Pubkey:
    """Derive tick array PDA for Whirlpool.

    CRITICAL: Whirlpool uses the STRING representation of start_tick_index
    as the PDA seed, not binary bytes. This matches the Orca on-chain program:
        seeds = [b"tick_array", whirlpool.key().as_ref(), start_tick_index.to_string().as_bytes()]
    """
    program_id = Pubkey.from_string(WHIRLPOOL_PROGRAM)
    pool_pubkey = Pubkey.from_string(whirlpool_address)
    seeds = [b"tick_array", bytes(pool_pubkey), str(start_tick).encode('utf-8')]
    pda, _ = Pubkey.find_program_address(seeds, program_id)
    return pda


def _parse_whirlpool_state(data: bytes) -> dict:
    """Parse essential fields from Whirlpool account data.

    Whirlpool account layout (densely packed, no alignment padding):
        0x00: discriminator (8)         [63,149,209,12,225,128,99,9]
        0x08: whirlpools_config (32)
        0x28: whirlpool_bump (u8[1])    = 1 byte
        0x29: tick_spacing (u16)        = 2 bytes
        0x2b: fee_tier_index_seed (u8[2]) = 2 bytes
        0x2d: fee_rate (u16)            = 2 bytes
        0x2f: protocol_fee_rate (u16)   = 2 bytes
        0x31: liquidity (u128)          = 16 bytes
        0x41: sqrt_price (u128)         = 16 bytes
        0x51: tick_current_index (i32)  = 4 bytes
        0x55: protocol_fee_owed_a (u64) = 8 bytes
        0x5d: protocol_fee_owed_b (u64) = 8 bytes
        0x65: token_mint_a (32)
        0x85: token_vault_a (32)
        0xa5: fee_growth_global_a (u128) = 16 bytes
        0xb5: token_mint_b (32)
        0xd5: token_vault_b (32)
        0xf5: fee_growth_global_b (u128) = 16 bytes
        ...

    Offsets verified against arbito/dex/whirlpool_parser.py sequential parse.
    """
    if len(data) < 0xf5 + 32:
        raise ValueError(f"Whirlpool account data too short: {len(data)} bytes")

    tick_spacing = struct.unpack_from('<H', data, 0x29)[0]
    sqrt_price = int.from_bytes(data[0x41:0x51], 'little')
    tick_current_index = struct.unpack_from('<i', data, 0x51)[0]
    token_mint_a = Pubkey.from_bytes(data[0x65:0x85])
    token_vault_a = Pubkey.from_bytes(data[0x85:0xa5])
    token_mint_b = Pubkey.from_bytes(data[0xb5:0xd5])
    token_vault_b = Pubkey.from_bytes(data[0xd5:0xf5])

    return {
        "tick_spacing": tick_spacing,
        "sqrt_price": sqrt_price,
        "tick_current_index": tick_current_index,
        "token_mint_a": token_mint_a,
        "token_mint_b": token_mint_b,
        "token_vault_a": token_vault_a,
        "token_vault_b": token_vault_b,
    }


def _build_whirlpool_swap_ix(
    pool: PoolInfo,
    input_mint: str,
    amount_in: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
) -> Optional[Instruction]:
    """Build a Whirlpool swap instruction.

    Uses input_mint (not a_to_b flag) to determine direction from on-chain
    state, avoiding registry/on-chain token ordering mismatches.
    """
    program_id = Pubkey.from_string(WHIRLPOOL_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool.pool_address)

    # Fetch pool state
    raw_data = fetch_pool_state(pool.pool_address, rpc_endpoint)
    if raw_data is None:
        logger.error(f"Whirlpool pool {pool.pool_address} not found on-chain")
        return None

    try:
        state = _parse_whirlpool_state(raw_data)
    except Exception as e:
        logger.error(f"Failed to parse Whirlpool state: {e}")
        return None

    tick_spacing = state["tick_spacing"]
    current_tick = state["tick_current_index"]
    token_vault_a = state["token_vault_a"]
    token_vault_b = state["token_vault_b"]
    token_mint_a = state["token_mint_a"]
    token_mint_b = state["token_mint_b"]

    # Derive a_to_b from ON-CHAIN mints (not registry) to avoid ordering mismatch
    a_to_b = (input_mint == str(token_mint_a))
    if input_mint != str(token_mint_a) and input_mint != str(token_mint_b):
        logger.error(
            f"Whirlpool: input_mint {input_mint[:12]}.. matches neither "
            f"on-chain token_a={str(token_mint_a)[:12]}.. nor token_b={str(token_mint_b)[:12]}.."
        )
        return None
    logger.info(f"Whirlpool: on-chain token_a={str(token_mint_a)[:12]}.. "
                f"token_b={str(token_mint_b)[:12]}.. a_to_b={a_to_b}")

    # Derive oracle PDA
    oracle, _ = Pubkey.find_program_address(
        [b"oracle", bytes(pool_pubkey)], program_id
    )

    # Calculate tick array sequence (3 arrays)
    # Matches Orca SDK: getStartTickIndex(tickIndex, tickSpacing, offset)
    #   realIndex = floor(tickIndex / tickSpacing / TICK_ARRAY_SIZE)
    #   startTickIndex = (realIndex + offset) * tickSpacing * TICK_ARRAY_SIZE
    TICK_ARRAY_SIZE = 88
    ticks_per_array = TICK_ARRAY_SIZE * tick_spacing

    def get_start_tick_index(tick_index: int, offset: int = 0) -> int:
        """Matches Orca SDK getStartTickIndex exactly."""
        real_index = math.floor(tick_index / tick_spacing / TICK_ARRAY_SIZE)
        return (real_index + offset) * tick_spacing * TICK_ARRAY_SIZE

    # For a_to_b (price decreasing): arrays at offset 0, -1, -2
    # For b_to_a (price increasing): arrays at offset 0, +1, +2
    direction = -1 if a_to_b else 1
    tick_starts = [
        get_start_tick_index(current_tick, 0),
        get_start_tick_index(current_tick, direction),
        get_start_tick_index(current_tick, 2 * direction),
    ]

    tick_array_0 = _get_tick_array_address(pool.pool_address, tick_starts[0])
    tick_array_1 = _get_tick_array_address(pool.pool_address, tick_starts[1])
    tick_array_2 = _get_tick_array_address(pool.pool_address, tick_starts[2])

    # User ATAs
    signer_pubkey = signer_keypair.pubkey()
    user_ata_a = get_associated_token_address(signer_pubkey, token_mint_a)
    user_ata_b = get_associated_token_address(signer_pubkey, token_mint_b)

    # Instruction data: Anchor discriminator + swap params
    discriminator = sha256(b"global:swap").digest()[:8]
    sqrt_price_limit = WHIRLPOOL_MIN_SQRT_PRICE if a_to_b else WHIRLPOOL_MAX_SQRT_PRICE
    other_amount_threshold = 0  # minimum output (0 = accept any)

    # sqrt_price_limit is u128 (16 bytes LE), split into two u64 for struct.pack
    lower = sqrt_price_limit & 0xFFFFFFFFFFFFFFFF
    upper = sqrt_price_limit >> 64

    # Booleans are Borsh bool = u8 (not signed byte)
    instruction_data = discriminator + struct.pack(
        '<QQQQBB',
        int(amount_in),                 # amount: u64
        int(other_amount_threshold),    # other_amount_threshold: u64
        int(lower),                     # sqrt_price_limit low 64 bits
        int(upper),                     # sqrt_price_limit high 64 bits
        1,                              # amount_specified_is_input: bool (u8)
        1 if a_to_b else 0,            # a_to_b: bool (u8)
    )

    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)

    accounts = [
        AccountMeta(token_program, is_signer=False, is_writable=False),
        AccountMeta(signer_pubkey, is_signer=True, is_writable=False),
        AccountMeta(pool_pubkey, is_signer=False, is_writable=True),
        AccountMeta(user_ata_a, is_signer=False, is_writable=True),
        AccountMeta(token_vault_a, is_signer=False, is_writable=True),
        AccountMeta(user_ata_b, is_signer=False, is_writable=True),
        AccountMeta(token_vault_b, is_signer=False, is_writable=True),
        AccountMeta(tick_array_0, is_signer=False, is_writable=True),
        AccountMeta(tick_array_1, is_signer=False, is_writable=True),
        AccountMeta(tick_array_2, is_signer=False, is_writable=True),
        AccountMeta(oracle, is_signer=False, is_writable=False),
    ]

    return Instruction(program_id=program_id, accounts=accounts, data=instruction_data)


# ---------------------------------------------------------------------------
# Meteora DLMM swap instruction builder
# ---------------------------------------------------------------------------

METEORA_BINS_PER_ARRAY = 70


def _derive_meteora_bin_array_pda(pool_address: str, index: int) -> Pubkey:
    """Derive bin array PDA for Meteora DLMM."""
    program_id = Pubkey.from_string(METEORA_DLMM_PROGRAM)
    pool_pk = Pubkey.from_string(pool_address)
    index_bytes = index.to_bytes(8, byteorder='little', signed=True)
    seeds = [b'bin_array', bytes(pool_pk), index_bytes]
    pda, _ = Pubkey.find_program_address(seeds, program_id)
    return pda


METEORA_DLMM_V1_DISC = bytes([33, 11, 49, 98, 181, 101, 177, 13])
METEORA_DLMM_V2_DISC = bytes([241, 154, 109, 4, 17, 177, 109, 188])


def _parse_meteora_state(data: bytes) -> dict:
    """Parse essential fields from Meteora DLMM lb_pair account.
    Supports both V1 (904 bytes) and V2 (944 bytes) layouts.
    """
    if len(data) < 200:
        raise ValueError(f"Meteora DLMM account data too short: {len(data)} bytes")

    disc = data[:8]
    if disc == METEORA_DLMM_V2_DISC:
        # V2 layout (944 bytes):
        # disc(8) + pubkey(32)@8 + tokenX(32)@40 + tokenY(32)@72 +
        # reserveX(32)@104 + reserveY(32)@136 + pubkey(32)@168 + pubkey(32)@200
        # ... parameters ...
        # bin_step(u16)@330 + ... + active_id(i32)@340
        token_x_mint = Pubkey.from_bytes(data[40:72])
        token_y_mint = Pubkey.from_bytes(data[72:104])
        reserve_x = Pubkey.from_bytes(data[104:136])
        reserve_y = Pubkey.from_bytes(data[136:168])
        active_id = struct.unpack_from('<i', data, 340)[0]
        return {
            "active_id": active_id,
            "token_x_mint": token_x_mint,
            "token_y_mint": token_y_mint,
            "reserve_x": reserve_x,
            "reserve_y": reserve_y,
            "version": 2,
        }
    else:
        # V1 layout (904 bytes):
        # disc(8) + StaticParams(32) + VarParams(32) + bump/binStep/pairType(4) +
        # activeId(i32)@0x4C + binStep(u16)@0x50 + ...
        # tokenXMint(32)@0x58 + tokenYMint(32)@0x78 +
        # reserveX(32)@0x98 + reserveY(32)@0xB8
        active_id = struct.unpack_from('<i', data, 0x4C)[0]
        token_x_mint = Pubkey.from_bytes(data[0x58:0x78])
        token_y_mint = Pubkey.from_bytes(data[0x78:0x98])
        reserve_x = Pubkey.from_bytes(data[0x98:0xB8])
        reserve_y = Pubkey.from_bytes(data[0xB8:0xD8])
        return {
            "active_id": active_id,
            "token_x_mint": token_x_mint,
            "token_y_mint": token_y_mint,
            "reserve_x": reserve_x,
            "reserve_y": reserve_y,
            "version": 1,
        }


def _build_meteora_swap_ix(
    pool: PoolInfo,
    a_to_b: bool,
    amount_in: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
) -> Optional[Instruction]:
    """Build a Meteora DLMM swap instruction.

    a_to_b here means: pool.token_a -> pool.token_b.
    In Meteora terms: swap_for_y = True means X->Y.
    We map: if input == token_x_mint, swap_for_y = True.
    """
    program_id = Pubkey.from_string(METEORA_DLMM_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool.pool_address)

    # Fetch pool state
    raw_data = fetch_pool_state(pool.pool_address, rpc_endpoint)
    if raw_data is None:
        logger.error(f"Meteora pool {pool.pool_address} not found on-chain")
        return None

    try:
        state = _parse_meteora_state(raw_data)
    except Exception as e:
        logger.error(f"Failed to parse Meteora state: {e}")
        return None

    token_x_mint = state["token_x_mint"]
    token_y_mint = state["token_y_mint"]
    reserve_x = state["reserve_x"]
    reserve_y = state["reserve_y"]
    active_id = state["active_id"]

    # Determine Meteora swap direction based on actual pool mints
    input_mint_pk = Pubkey.from_string(pool.token_a_mint if a_to_b else pool.token_b_mint)
    swap_for_y = (input_mint_pk == token_x_mint)

    signer_pubkey = signer_keypair.pubkey()

    # Derive PDAs
    oracle, _ = Pubkey.find_program_address(
        [b"oracle", bytes(pool_pubkey)], program_id
    )
    event_authority, _ = Pubkey.find_program_address(
        [b"__event_authority"], program_id
    )

    # Bitmap extension PDA — check if it exists on-chain
    bitmap_pda, _ = Pubkey.find_program_address(
        [b"bitmap", bytes(pool_pubkey)], program_id
    )
    # Probe bitmap existence
    bitmap_owner = fetch_account_owner(str(bitmap_pda), rpc_endpoint)
    if bitmap_owner == METEORA_DLMM_PROGRAM:
        bin_array_bitmap_extension = bitmap_pda
    else:
        # Use program_id as placeholder when bitmap extension doesn't exist
        bin_array_bitmap_extension = program_id

    # User ATAs
    user_token_in = get_associated_token_address(
        signer_pubkey,
        token_x_mint if swap_for_y else token_y_mint,
    )
    user_token_out = get_associated_token_address(
        signer_pubkey,
        token_y_mint if swap_for_y else token_x_mint,
    )

    # Instruction data: discriminator + amount_in + min_amount_out
    discriminator = bytes([0xf8, 0xc6, 0x9e, 0x91, 0xe1, 0x75, 0x87, 0xc8])
    min_amount_out = 1
    instruction_data = discriminator + struct.pack('<QQ', int(amount_in), min_amount_out)

    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)

    # Account list matching on-chain Meteora DLMM swap
    accounts = [
        AccountMeta(pool_pubkey, is_signer=False, is_writable=True),           # lb_pair
        AccountMeta(bin_array_bitmap_extension, is_signer=False, is_writable=True),  # bitmap ext
        AccountMeta(reserve_x, is_signer=False, is_writable=True),             # reserve_x
        AccountMeta(reserve_y, is_signer=False, is_writable=True),             # reserve_y
        AccountMeta(user_token_in, is_signer=False, is_writable=True),         # user_token_in
        AccountMeta(user_token_out, is_signer=False, is_writable=True),        # user_token_out
        AccountMeta(token_x_mint, is_signer=False, is_writable=False),         # token_x_mint
        AccountMeta(token_y_mint, is_signer=False, is_writable=False),         # token_y_mint
        AccountMeta(oracle, is_signer=False, is_writable=True),                # oracle
        AccountMeta(user_token_in, is_signer=False, is_writable=True),         # host_fee_account
        AccountMeta(signer_pubkey, is_signer=True, is_writable=True),          # user/signer
        AccountMeta(token_program, is_signer=False, is_writable=False),        # token_program
        AccountMeta(token_program, is_signer=False, is_writable=False),        # token_program (2)
        AccountMeta(event_authority, is_signer=False, is_writable=False),      # event_authority
        AccountMeta(program_id, is_signer=False, is_writable=False),           # program
    ]

    # Append bin array PDAs (base-1, base, base+1)
    base_idx = active_id // METEORA_BINS_PER_ARRAY
    for idx in [base_idx - 1, base_idx, base_idx + 1]:
        pda = _derive_meteora_bin_array_pda(pool.pool_address, idx)
        accounts.append(AccountMeta(pda, is_signer=False, is_writable=True))

    return Instruction(program_id=program_id, accounts=accounts, data=instruction_data)


# ---------------------------------------------------------------------------
# Manifest swap instruction builder
# ---------------------------------------------------------------------------

def _parse_manifest_market(data: bytes) -> dict:
    """Parse Manifest MarketFixed account to extract mints and vaults.

    MarketFixed layout (256 bytes, #[repr(C)]):
        0x00: discriminant (u64, 8 bytes)
        0x08: version (u8)
        0x09: base_mint_decimals (u8)
        0x0A: quote_mint_decimals (u8)
        0x0B: base_vault_bump (u8)
        0x0C: quote_vault_bump (u8)
        0x0D: _padding1 (3 bytes)
        0x10: base_mint (32 bytes)
        0x30: quote_mint (32 bytes)
        0x50: base_vault (32 bytes)
        0x70: quote_vault (32 bytes)
        ... (remaining fields)
    """
    if len(data) < 0x90:
        raise ValueError(f"Manifest market data too short: {len(data)} bytes")

    base_mint = Pubkey.from_bytes(data[0x10:0x30])
    quote_mint = Pubkey.from_bytes(data[0x30:0x50])
    base_vault = Pubkey.from_bytes(data[0x50:0x70])
    quote_vault = Pubkey.from_bytes(data[0x70:0x90])

    return {
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "base_vault": base_vault,
        "quote_vault": quote_vault,
    }


def _build_manifest_swap_ix(
    pool: PoolInfo,
    a_to_b: bool,
    amount_in: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
) -> Optional[Instruction]:
    """Build a Manifest Swap instruction.

    Manifest Swap (discriminator = 4, single byte):
        Data (Borsh): in_atoms(u64) + out_atoms(u64) +
              is_base_in(u8) + is_exact_in(u8) = 18 bytes + 1 byte disc = 19

    Account layout from instruction.rs:
        0: payer (signer, writable)
        1: market (writable)
        2: system_program
        3: trader_base (writable)
        4: trader_quote (writable)
        5: base_vault (writable)
        6: quote_vault (writable)
        7: token_program_base
        8: base_mint (optional — needed for Token-2022)
        9: token_program_quote (optional — needed if different from base)
        10: quote_mint (optional — needed for Token-2022)

    For standard SPL Token mints (not Token-2022), accounts 8-10 are optional
    and can be omitted. We include them for safety since the program handles
    them gracefully.
    """
    program_id = Pubkey.from_string(MANIFEST_PROGRAM)
    market_pubkey = Pubkey.from_string(pool.pool_address)
    signer_pubkey = signer_keypair.pubkey()

    # Fetch market account data to find mints and vaults
    raw_data = fetch_pool_state(pool.pool_address, rpc_endpoint)
    if raw_data is None:
        logger.error(f"Manifest market {pool.pool_address} not found on-chain")
        return None

    try:
        market = _parse_manifest_market(raw_data)
    except Exception as e:
        logger.error(f"Failed to parse Manifest market: {e}")
        return None

    base_mint = market["base_mint"]
    quote_mint = market["quote_mint"]
    base_vault = market["base_vault"]
    quote_vault = market["quote_vault"]

    # Validate parsed mints match expected pool mints
    parsed_mints = {str(base_mint), str(quote_mint)}
    expected_mints = {pool.token_a_mint, pool.token_b_mint}
    if parsed_mints != expected_mints:
        logger.error(
            f"Manifest market {pool.pool_address}: parsed mints {parsed_mints} "
            f"don't match expected {expected_mints}. Layout may have changed."
        )
        return None

    # Determine if input is base or quote
    input_mint_str = pool.token_a_mint if a_to_b else pool.token_b_mint
    is_base_in = (input_mint_str == str(base_mint))

    # User ATAs
    trader_base_ata = get_associated_token_address(signer_pubkey, base_mint)
    trader_quote_ata = get_associated_token_address(signer_pubkey, quote_mint)

    # Token programs — all our LST/SOL pairs use standard Token program
    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    system_program = Pubkey.from_string(SYSTEM_PROGRAM_ID)

    # Build instruction data
    # Swap discriminator: 4 (single byte, not Anchor 8-byte hash)
    # Borsh-serialized SwapParams: in_atoms(u64) + out_atoms(u64) +
    #   is_base_in(bool=u8) + is_exact_in(bool=u8)
    instruction_data = struct.pack(
        '<BQQBB',
        4,                              # discriminator (Swap = instruction 4)
        int(amount_in),                 # in_atoms
        1,                              # out_atoms (min output)
        1 if is_base_in else 0,         # is_base_in
        1,                              # is_exact_in
    )

    accounts = [
        AccountMeta(signer_pubkey, is_signer=True, is_writable=True),      # payer
        AccountMeta(market_pubkey, is_signer=False, is_writable=True),     # market
        AccountMeta(system_program, is_signer=False, is_writable=False),   # system_program
        AccountMeta(trader_base_ata, is_signer=False, is_writable=True),   # trader_base
        AccountMeta(trader_quote_ata, is_signer=False, is_writable=True),  # trader_quote
        AccountMeta(base_vault, is_signer=False, is_writable=True),        # base_vault
        AccountMeta(quote_vault, is_signer=False, is_writable=True),       # quote_vault
        AccountMeta(token_program, is_signer=False, is_writable=False),    # token_program_base
        AccountMeta(base_mint, is_signer=False, is_writable=False),        # base_mint (optional)
        AccountMeta(token_program, is_signer=False, is_writable=False),    # token_program_quote (optional)
        AccountMeta(quote_mint, is_signer=False, is_writable=False),       # quote_mint (optional)
    ]

    return Instruction(program_id=program_id, accounts=accounts, data=instruction_data)


# ---------------------------------------------------------------------------
# PancakeSwap CLMM swap instruction builder
# ---------------------------------------------------------------------------

def _parse_pancakeswap_pool_state(
    data: bytes,
    expected_mint_a: str = "",
    expected_mint_b: str = "",
    pool_address: str = "",
    rpc_endpoint: str = "",
) -> dict:
    """Parse PancakeSwap CLMM pool state.

    PancakeSwap CLMM is a fork of Raydium CLMM. The pool state layout
    (densely packed, Anchor serialization, no alignment padding):

        0x00: discriminator (8)
        0x08: bump (u8[1])              = 1 byte
        0x09: amm_config (32)
        0x29: owner (32)
        0x49: token_mint_0 (32)
        0x69: token_mint_1 (32)
        0x89: token_vault_0 (32)
        0xa9: token_vault_1 (32)
        0xc9: observation_key (32)
        0xe9: mint_decimals_0 (u8) + mint_decimals_1 (u8) = 2 bytes
        0xeb: tick_spacing (i16)        = 2 bytes
        0xed: liquidity (u128)          = 16 bytes
        0xfd: sqrt_price_x64 (u128)     = 16 bytes
        0x10d: tick_current (i32)       = 4 bytes
        ...

    If fixed offsets produce wrong mints, falls back to scanning account data
    for known mint pubkeys and deriving vaults from adjacent 32-byte slots.
    """
    if len(data) < 0x111:
        raise ValueError(f"PancakeSwap pool data too short: {len(data)} bytes")

    amm_config = Pubkey.from_bytes(data[0x09:0x29])
    token_mint_0 = Pubkey.from_bytes(data[0x49:0x69])
    token_mint_1 = Pubkey.from_bytes(data[0x69:0x89])
    token_vault_0 = Pubkey.from_bytes(data[0x89:0xa9])
    token_vault_1 = Pubkey.from_bytes(data[0xa9:0xc9])
    observation_key = Pubkey.from_bytes(data[0xc9:0xe9])
    tick_spacing = struct.unpack_from('<h', data, 0xeb)[0]
    sqrt_price_x64 = int.from_bytes(data[0xed:0xfd], 'little')
    tick_current = struct.unpack_from('<i', data, 0x10d)[0]

    # Validate fixed-offset mints against expected values
    parsed_set = {str(token_mint_0), str(token_mint_1)}
    expected_set = {expected_mint_a, expected_mint_b} if expected_mint_a else set()

    if expected_set and parsed_set != expected_set:
        logger.warning(
            f"PancakeSwap fixed-offset mints {parsed_set} don't match expected "
            f"{expected_set}. Scanning account data for correct offsets..."
        )
        # Scan for the expected mints in the account data
        mint_offsets = {}
        for offset in range(0, len(data) - 32, 1):
            try:
                pk = Pubkey.from_bytes(data[offset:offset + 32])
                pk_str = str(pk)
                if pk_str in expected_set and pk_str not in mint_offsets:
                    mint_offsets[pk_str] = offset
                    logger.info(f"  Found mint {pk_str[:12]}.. at offset 0x{offset:x}")
                    if len(mint_offsets) == 2:
                        break
            except Exception:
                continue

        if len(mint_offsets) == 2:
            sorted_mints = sorted(mint_offsets.items(), key=lambda x: x[1])
            m0_str, m0_off = sorted_mints[0]
            m1_str, m1_off = sorted_mints[1]
            token_mint_0 = Pubkey.from_string(m0_str)
            token_mint_1 = Pubkey.from_string(m1_str)

            # In Raydium CLMM layout: mint_0, mint_1, vault_0, vault_1 (contiguous)
            # vault_0 is at mint_0 + 64 (skip both mints), vault_1 is at mint_0 + 96
            vault_0_off = m0_off + 64
            vault_1_off = m0_off + 96
            if vault_0_off + 32 <= len(data) and vault_1_off + 32 <= len(data):
                token_vault_0 = Pubkey.from_bytes(data[vault_0_off:vault_0_off + 32])
                token_vault_1 = Pubkey.from_bytes(data[vault_1_off:vault_1_off + 32])
                logger.info(f"  vault_0 at 0x{vault_0_off:x}: {str(token_vault_0)[:12]}..")
                logger.info(f"  vault_1 at 0x{vault_1_off:x}: {str(token_vault_1)[:12]}..")

                # Verify vaults are token accounts (owned by Token program)
                if rpc_endpoint:
                    for vault_name, vault_pk in [("vault_0", token_vault_0), ("vault_1", token_vault_1)]:
                        owner = fetch_account_owner(str(vault_pk), rpc_endpoint)
                        if owner != TOKEN_PROGRAM_ID:
                            logger.warning(f"  {vault_name} {str(vault_pk)[:12]}.. owner={owner}, expected Token program")

            # amm_config is typically at a fixed offset relative to mints
            # For Raydium CLMM: amm_config at 0x09, mint_0 at 0x49, so config = mint_0 - 0x40
            config_off = m0_off - 0x40
            if config_off >= 8 and config_off + 32 <= len(data):
                amm_config = Pubkey.from_bytes(data[config_off:config_off + 32])
                logger.info(f"  amm_config at 0x{config_off:x}: {str(amm_config)[:12]}..")

            # observation_key follows vault_1 in Raydium layout (mint_0 + 128)
            obs_off = m0_off + 128
            if obs_off + 32 <= len(data):
                observation_key = Pubkey.from_bytes(data[obs_off:obs_off + 32])
                logger.info(f"  observation_key at 0x{obs_off:x}: {str(observation_key)[:12]}..")

            # After observation_key: mint_decimals(2) + tick_spacing(2) + liquidity(16) + sqrt_price(16) + tick_current(4)
            ts_off = obs_off + 32 + 2  # skip observation_key(32) + mint_decimals(2)
            if ts_off + 2 <= len(data):
                tick_spacing = struct.unpack_from('<h', data, ts_off)[0]
                logger.info(f"  tick_spacing at 0x{ts_off:x}: {tick_spacing}")

            # sqrt_price_x64 at ts_off + 2 + 16 (skip tick_spacing + liquidity)
            sp_off = ts_off + 2 + 16
            if sp_off + 16 <= len(data):
                sqrt_price_x64 = int.from_bytes(data[sp_off:sp_off + 16], 'little')

            tc_off = sp_off + 16  # after sqrt_price_x64
            if tc_off + 4 <= len(data):
                tick_current = struct.unpack_from('<i', data, tc_off)[0]
                logger.info(f"  tick_current at 0x{tc_off:x}: {tick_current}")
        else:
            raise ValueError(
                f"Could not find expected mints in PancakeSwap pool data. "
                f"Expected: {expected_set}, found offsets: {mint_offsets}"
            )

    return {
        "amm_config": amm_config,
        "token_mint_0": token_mint_0,
        "token_mint_1": token_mint_1,
        "token_vault_0": token_vault_0,
        "token_vault_1": token_vault_1,
        "observation_key": observation_key,
        "tick_spacing": tick_spacing,
        "sqrt_price_x64": sqrt_price_x64,
        "tick_current": tick_current,
    }


def _get_pancakeswap_tick_array_address(
    pool_address: str, start_tick: int
) -> Pubkey:
    """Derive tick array PDA for PancakeSwap CLMM.

    PancakeSwap uses i32 BIG-ENDIAN encoding for start_tick in PDA seeds
    (unlike Whirlpool which uses string representation).
    Seeds: ["tick_array", pool_state.key(), start_tick_index(i32 BE)]
    """
    program_id = Pubkey.from_string(PANCAKESWAP_CLMM_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool_address)
    tick_bytes = struct.pack('>i', start_tick)
    seeds = [b"tick_array", bytes(pool_pubkey), tick_bytes]
    pda, _ = Pubkey.find_program_address(seeds, program_id)
    return pda


def _get_pancakeswap_bitmap_extension(pool_address: str) -> Pubkey:
    """Derive tick array bitmap extension PDA for PancakeSwap CLMM.

    PDA seeds from IDL: ["pool_tick_array_bitmap_extension", pool_state.key()]
    """
    program_id = Pubkey.from_string(PANCAKESWAP_CLMM_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool_address)
    seeds = [b"pool_tick_array_bitmap_extension", bytes(pool_pubkey)]
    pda, _ = Pubkey.find_program_address(seeds, program_id)
    return pda


# PancakeSwap CLMM sqrt_price limits (Raydium CLMM fork)
# The on-chain program uses STRICT inequality (> MIN, < MAX), so we use MIN+1 / MAX-1
# to avoid SqrtPriceLimitOverflow error.
PANCAKE_MIN_SQRT_PRICE_X64 = 4295048016 + 1
PANCAKE_MAX_SQRT_PRICE_X64 = 79226673515401279992447579055 - 1


def _build_pancakeswap_swap_ix(
    pool: PoolInfo,
    input_mint: str,
    amount_in: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
) -> Optional[Instruction]:
    """Build a PancakeSwap CLMM swap instruction.

    Uses input_mint (not a_to_b flag) to determine direction from on-chain state.

    Discriminator: f8c69e91e17587c8 (8 bytes)
    Data: discriminator(8) + amount(u64) + other_amount_threshold(u64) +
          sqrt_price_limit_x64(u128) + a_to_b(u8) = 41 bytes

    Accounts:
        0: signer (signer, writable)
        1: amm_config
        2: pool_state (writable)
        3: input_token_account (writable)
        4: output_token_account (writable)
        5: input_vault (writable)
        6: output_vault (writable)
        7: observation_state (writable)
        8: token_program
        9: tick_array_0 (writable)
        10: tick_array_1 (writable)
        11: tick_array_2 (writable)
        12: tick_array_bitmap_extension
    """
    program_id = Pubkey.from_string(PANCAKESWAP_CLMM_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool.pool_address)
    signer_pubkey = signer_keypair.pubkey()

    # Fetch pool state
    raw_data = fetch_pool_state(pool.pool_address, rpc_endpoint)
    if raw_data is None:
        logger.error(f"PancakeSwap pool {pool.pool_address} not found on-chain")
        return None

    try:
        state = _parse_pancakeswap_pool_state(
            raw_data,
            expected_mint_a=pool.token_a_mint,
            expected_mint_b=pool.token_b_mint,
            pool_address=pool.pool_address,
            rpc_endpoint=rpc_endpoint,
        )
    except Exception as e:
        logger.error(f"Failed to parse PancakeSwap pool state: {e}")
        return None

    token_mint_0 = state["token_mint_0"]
    token_mint_1 = state["token_mint_1"]
    token_vault_0 = state["token_vault_0"]
    token_vault_1 = state["token_vault_1"]
    amm_config = state["amm_config"]
    observation_key = state["observation_key"]
    tick_spacing = state["tick_spacing"]
    tick_current = state["tick_current"]

    # Validate parsed mints match expected pool mints
    parsed_mints = {str(token_mint_0), str(token_mint_1)}
    expected_mints = {pool.token_a_mint, pool.token_b_mint}
    if parsed_mints != expected_mints:
        logger.error(
            f"PancakeSwap pool {pool.pool_address}: parsed mints {parsed_mints} "
            f"don't match expected {expected_mints}. Layout offsets may be wrong."
        )
        return None

    # Determine swap direction relative to on-chain mint_0/mint_1
    is_zero_to_one = (input_mint == str(token_mint_0))
    if input_mint != str(token_mint_0) and input_mint != str(token_mint_1):
        logger.error(
            f"PancakeSwap: input_mint {input_mint[:12]}.. matches neither "
            f"on-chain mint_0={str(token_mint_0)[:12]}.. nor mint_1={str(token_mint_1)[:12]}.."
        )
        return None

    # User ATAs
    if is_zero_to_one:
        input_ata = get_associated_token_address(signer_pubkey, token_mint_0)
        output_ata = get_associated_token_address(signer_pubkey, token_mint_1)
        input_vault = token_vault_0
        output_vault = token_vault_1
    else:
        input_ata = get_associated_token_address(signer_pubkey, token_mint_1)
        output_ata = get_associated_token_address(signer_pubkey, token_mint_0)
        input_vault = token_vault_1
        output_vault = token_vault_0

    # Calculate tick array sequence
    # PancakeSwap CLMM (Raydium CLMM fork) uses 60 ticks per array
    PANCAKE_TICK_ARRAY_SIZE = 60
    ticks_per_array = PANCAKE_TICK_ARRAY_SIZE * abs(tick_spacing)

    def get_start_tick_index(tick_index: int, offset: int = 0) -> int:
        real_index = math.floor(tick_index / abs(tick_spacing) / PANCAKE_TICK_ARRAY_SIZE)
        return (real_index + offset) * abs(tick_spacing) * PANCAKE_TICK_ARRAY_SIZE

    # For zero_to_one (price decreasing): arrays at offset 0, -1, -2
    # For one_to_zero (price increasing): arrays at offset 0, +1, +2
    direction = -1 if is_zero_to_one else 1
    tick_starts = [
        get_start_tick_index(tick_current, 0),
        get_start_tick_index(tick_current, direction),
        get_start_tick_index(tick_current, 2 * direction),
    ]

    tick_array_0 = _get_pancakeswap_tick_array_address(pool.pool_address, tick_starts[0])
    tick_array_1 = _get_pancakeswap_tick_array_address(pool.pool_address, tick_starts[1])
    tick_array_2 = _get_pancakeswap_tick_array_address(pool.pool_address, tick_starts[2])
    bitmap_ext = _get_pancakeswap_bitmap_extension(pool.pool_address)

    # Instruction data: Anchor discriminator + swap params
    # Raydium CLMM fork: swap(amount, other_amount_threshold, sqrt_price_limit_x64, is_base_input)
    discriminator = sha256(b"global:swap").digest()[:8]
    other_amount_threshold = 0  # min output (0 = accept any)

    sqrt_price_limit = PANCAKE_MIN_SQRT_PRICE_X64 if is_zero_to_one else PANCAKE_MAX_SQRT_PRICE_X64
    sqrt_lower = sqrt_price_limit & 0xFFFFFFFFFFFFFFFF
    sqrt_upper = sqrt_price_limit >> 64

    # is_base_input: true means amount is the input amount (exact-in swap)
    instruction_data = discriminator + struct.pack(
        '<QQQQB',
        int(amount_in),                 # amount: u64
        int(other_amount_threshold),    # other_amount_threshold: u64
        int(sqrt_lower),                # sqrt_price_limit_x64 low 64 bits
        int(sqrt_upper),                # sqrt_price_limit_x64 high 64 bits
        1,                              # is_base_input: bool (u8) = true (exact input)
    )

    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)

    # Account layout from IDL: 10 fixed accounts (payer through tick_array),
    # then remaining accounts: bitmap_extension, tick_array_1, tick_array_2.
    # Jupiter's known-good ordering:
    #   0:payer, 1:amm_config, 2:pool_state, 3:input_token, 4:output_token,
    #   5:input_vault, 6:output_vault, 7:observation_state, 8:token_program,
    #   9:tick_array_0, 10:bitmap_extension, 11:tick_array_1, 12:tick_array_2
    accounts = [
        AccountMeta(signer_pubkey, is_signer=True, is_writable=False),     # payer (IDL: isMut=false)
        AccountMeta(amm_config, is_signer=False, is_writable=False),       # amm_config
        AccountMeta(pool_pubkey, is_signer=False, is_writable=True),       # pool_state
        AccountMeta(input_ata, is_signer=False, is_writable=True),         # input_token_account
        AccountMeta(output_ata, is_signer=False, is_writable=True),        # output_token_account
        AccountMeta(input_vault, is_signer=False, is_writable=True),       # input_vault
        AccountMeta(output_vault, is_signer=False, is_writable=True),      # output_vault
        AccountMeta(observation_key, is_signer=False, is_writable=True),   # observation_state
        AccountMeta(token_program, is_signer=False, is_writable=False),    # token_program
        AccountMeta(tick_array_0, is_signer=False, is_writable=True),      # tick_array_0 (fixed)
        AccountMeta(bitmap_ext, is_signer=False, is_writable=True),        # bitmap_extension (remaining)
        AccountMeta(tick_array_1, is_signer=False, is_writable=True),      # tick_array_1 (remaining)
        AccountMeta(tick_array_2, is_signer=False, is_writable=True),      # tick_array_2 (remaining)
    ]

    return Instruction(program_id=program_id, accounts=accounts, data=instruction_data)


# ---------------------------------------------------------------------------
# Raydium CLMM swap instruction builder
# ---------------------------------------------------------------------------

# Raydium CLMM sqrt_price limits (same constants as PancakeSwap fork)
# On-chain uses strict inequality, so MIN+1 / MAX-1
RAYDIUM_CLMM_MIN_SQRT_PRICE_X64 = 4295048016 + 1
RAYDIUM_CLMM_MAX_SQRT_PRICE_X64 = 79226673515401279992447579055 - 1

# SPL Memo program (required by swap_v2)
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

# Tick array size for Raydium CLMM
RAYDIUM_TICK_ARRAY_SIZE = 60


def _parse_raydium_clmm_pool_state(
    data: bytes,
    expected_mint_a: str = "",
    expected_mint_b: str = "",
    pool_address: str = "",
    rpc_endpoint: str = "",
) -> dict:
    """Parse Raydium CLMM pool state.

    Layout (identical to PancakeSwap CLMM fork):
        0x00: discriminator (8)
        0x08: bump (u8)                 = 1 byte
        0x09: amm_config (32)
        0x29: owner (32)
        0x49: token_mint_0 (32)
        0x69: token_mint_1 (32)
        0x89: token_vault_0 (32)
        0xa9: token_vault_1 (32)
        0xc9: observation_key (32)
        0xe9: mint_decimals_0 (u8) + mint_decimals_1 (u8) = 2 bytes
        0xeb: tick_spacing (u16)        = 2 bytes
        0xed: liquidity (u128)          = 16 bytes
        0xfd: sqrt_price_x64 (u128)     = 16 bytes
        0x10d: tick_current (i32)       = 4 bytes
    """
    if len(data) < 0x111:
        raise ValueError(f"Raydium CLMM pool data too short: {len(data)} bytes")

    amm_config = Pubkey.from_bytes(data[0x09:0x29])
    token_mint_0 = Pubkey.from_bytes(data[0x49:0x69])
    token_mint_1 = Pubkey.from_bytes(data[0x69:0x89])
    token_vault_0 = Pubkey.from_bytes(data[0x89:0xa9])
    token_vault_1 = Pubkey.from_bytes(data[0xa9:0xc9])
    observation_key = Pubkey.from_bytes(data[0xc9:0xe9])
    tick_spacing = struct.unpack_from('<H', data, 0xeb)[0]  # u16
    sqrt_price_x64 = int.from_bytes(data[0xed:0xfd], 'little')
    tick_current = struct.unpack_from('<i', data, 0x10d)[0]

    # Validate parsed mints against expected values
    parsed_set = {str(token_mint_0), str(token_mint_1)}
    expected_set = {expected_mint_a, expected_mint_b} if expected_mint_a else set()

    if expected_set and parsed_set != expected_set:
        logger.warning(
            f"Raydium CLMM fixed-offset mints {parsed_set} don't match expected "
            f"{expected_set}. Scanning account data for correct offsets..."
        )
        # Scan for the expected mints in the account data
        mint_offsets = {}
        for offset in range(0, len(data) - 32, 1):
            try:
                pk = Pubkey.from_bytes(data[offset:offset + 32])
                pk_str = str(pk)
                if pk_str in expected_set and pk_str not in mint_offsets:
                    mint_offsets[pk_str] = offset
                    logger.info(f"  Found mint {pk_str[:12]}.. at offset 0x{offset:x}")
                    if len(mint_offsets) == 2:
                        break
            except Exception:
                continue

        if len(mint_offsets) == 2:
            sorted_mints = sorted(mint_offsets.items(), key=lambda x: x[1])
            m0_str, m0_off = sorted_mints[0]
            m1_str, m1_off = sorted_mints[1]
            token_mint_0 = Pubkey.from_string(m0_str)
            token_mint_1 = Pubkey.from_string(m1_str)

            # In Raydium CLMM layout: mint_0, mint_1, vault_0, vault_1 (contiguous)
            vault_0_off = m0_off + 64
            vault_1_off = m0_off + 96
            if vault_0_off + 32 <= len(data) and vault_1_off + 32 <= len(data):
                token_vault_0 = Pubkey.from_bytes(data[vault_0_off:vault_0_off + 32])
                token_vault_1 = Pubkey.from_bytes(data[vault_1_off:vault_1_off + 32])
                logger.info(f"  vault_0 at 0x{vault_0_off:x}: {str(token_vault_0)[:12]}..")
                logger.info(f"  vault_1 at 0x{vault_1_off:x}: {str(token_vault_1)[:12]}..")

                if rpc_endpoint:
                    for vault_name, vault_pk in [("vault_0", token_vault_0), ("vault_1", token_vault_1)]:
                        owner = fetch_account_owner(str(vault_pk), rpc_endpoint)
                        if owner not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
                            logger.warning(f"  {vault_name} {str(vault_pk)[:12]}.. owner={owner}, expected Token program")

            config_off = m0_off - 0x40
            if config_off >= 8 and config_off + 32 <= len(data):
                amm_config = Pubkey.from_bytes(data[config_off:config_off + 32])
                logger.info(f"  amm_config at 0x{config_off:x}: {str(amm_config)[:12]}..")

            obs_off = m0_off + 128
            if obs_off + 32 <= len(data):
                observation_key = Pubkey.from_bytes(data[obs_off:obs_off + 32])
                logger.info(f"  observation_key at 0x{obs_off:x}: {str(observation_key)[:12]}..")

            ts_off = obs_off + 32 + 2  # skip observation_key(32) + mint_decimals(2)
            if ts_off + 2 <= len(data):
                tick_spacing = struct.unpack_from('<H', data, ts_off)[0]
                logger.info(f"  tick_spacing at 0x{ts_off:x}: {tick_spacing}")

            sp_off = ts_off + 2 + 16  # skip tick_spacing + liquidity
            if sp_off + 16 <= len(data):
                sqrt_price_x64 = int.from_bytes(data[sp_off:sp_off + 16], 'little')

            tc_off = sp_off + 16
            if tc_off + 4 <= len(data):
                tick_current = struct.unpack_from('<i', data, tc_off)[0]
                logger.info(f"  tick_current at 0x{tc_off:x}: {tick_current}")
        else:
            raise ValueError(
                f"Could not find expected mints in Raydium CLMM pool data. "
                f"Expected: {expected_set}, found offsets: {mint_offsets}"
            )

    return {
        "amm_config": amm_config,
        "token_mint_0": token_mint_0,
        "token_mint_1": token_mint_1,
        "token_vault_0": token_vault_0,
        "token_vault_1": token_vault_1,
        "observation_key": observation_key,
        "tick_spacing": tick_spacing,
        "sqrt_price_x64": sqrt_price_x64,
        "tick_current": tick_current,
    }


def _get_raydium_clmm_tick_array_address(
    pool_address: str, start_tick: int
) -> Pubkey:
    """Derive tick array PDA for Raydium CLMM.

    Seeds: ["tick_array", pool_state.key(), start_tick_index(i32 BE)]
    Uses big-endian i32 encoding (same as PancakeSwap fork).
    """
    program_id = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool_address)
    tick_bytes = struct.pack('>i', start_tick)
    seeds = [b"tick_array", bytes(pool_pubkey), tick_bytes]
    pda, _ = Pubkey.find_program_address(seeds, program_id)
    return pda


def _get_token_program_for_mint(mint_str: str, rpc_endpoint: str) -> Pubkey:
    """Determine whether a mint uses Token or Token-2022 by checking account owner."""
    owner = fetch_account_owner(mint_str, rpc_endpoint)
    if owner == TOKEN_2022_PROGRAM_ID:
        return Pubkey.from_string(TOKEN_2022_PROGRAM_ID)
    return Pubkey.from_string(TOKEN_PROGRAM_ID)


def _build_raydium_clmm_swap_ix(
    pool: PoolInfo,
    input_mint: str,
    amount_in: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
) -> Optional[Instruction]:
    """Build a Raydium CLMM swap_v2 instruction.

    Uses input_mint (not a_to_b flag) to determine direction from on-chain state.

    swap_v2 discriminator: [43, 4, 237, 11, 26, 201, 30, 98]
    Data: discriminator(8) + amount(u64) + other_amount_threshold(u64) +
          sqrt_price_limit_x64(u128) + is_base_input(bool) = 41 bytes

    13 fixed accounts:
        0: payer (signer)
        1: amm_config
        2: pool_state (writable)
        3: input_token_account (writable)
        4: output_token_account (writable)
        5: input_vault (writable)
        6: output_vault (writable)
        7: observation_state (writable)
        8: token_program
        9: token_program_2022
        10: memo_program
        11: input_vault_mint
        12: output_vault_mint

    Remaining accounts: tick arrays (3x, writable)
    """
    program_id = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool.pool_address)
    signer_pubkey = signer_keypair.pubkey()

    # Fetch pool state
    raw_data = fetch_pool_state(pool.pool_address, rpc_endpoint)
    if raw_data is None:
        logger.error(f"Raydium CLMM pool {pool.pool_address} not found on-chain")
        return None

    try:
        state = _parse_raydium_clmm_pool_state(
            raw_data,
            expected_mint_a=pool.token_a_mint,
            expected_mint_b=pool.token_b_mint,
            pool_address=pool.pool_address,
            rpc_endpoint=rpc_endpoint,
        )
    except Exception as e:
        logger.error(f"Failed to parse Raydium CLMM pool state: {e}")
        return None

    token_mint_0 = state["token_mint_0"]
    token_mint_1 = state["token_mint_1"]
    token_vault_0 = state["token_vault_0"]
    token_vault_1 = state["token_vault_1"]
    amm_config = state["amm_config"]
    observation_key = state["observation_key"]
    tick_spacing = state["tick_spacing"]
    tick_current = state["tick_current"]

    # Validate parsed mints match expected pool mints
    parsed_mints = {str(token_mint_0), str(token_mint_1)}
    expected_mints = {pool.token_a_mint, pool.token_b_mint}
    if parsed_mints != expected_mints:
        logger.error(
            f"Raydium CLMM pool {pool.pool_address}: parsed mints {parsed_mints} "
            f"don't match expected {expected_mints}. Layout offsets may be wrong."
        )
        return None

    # Determine swap direction relative to on-chain mint_0/mint_1
    is_zero_to_one = (input_mint == str(token_mint_0))
    if input_mint != str(token_mint_0) and input_mint != str(token_mint_1):
        logger.error(
            f"Raydium CLMM: input_mint {input_mint[:12]}.. matches neither "
            f"on-chain mint_0={str(token_mint_0)[:12]}.. nor mint_1={str(token_mint_1)[:12]}.."
        )
        return None

    # Determine token programs for each mint (Token vs Token-2022)
    mint_0_token_program = _get_token_program_for_mint(str(token_mint_0), rpc_endpoint)
    mint_1_token_program = _get_token_program_for_mint(str(token_mint_1), rpc_endpoint)

    # User ATAs (derived with the correct token program for each mint)
    if is_zero_to_one:
        input_ata = get_associated_token_address(signer_pubkey, token_mint_0, mint_0_token_program)
        output_ata = get_associated_token_address(signer_pubkey, token_mint_1, mint_1_token_program)
        input_vault = token_vault_0
        output_vault = token_vault_1
        input_vault_mint = token_mint_0
        output_vault_mint = token_mint_1
    else:
        input_ata = get_associated_token_address(signer_pubkey, token_mint_1, mint_1_token_program)
        output_ata = get_associated_token_address(signer_pubkey, token_mint_0, mint_0_token_program)
        input_vault = token_vault_1
        output_vault = token_vault_0
        input_vault_mint = token_mint_1
        output_vault_mint = token_mint_0

    # Calculate tick array sequence
    def get_start_tick_index(tick_index: int, offset: int = 0) -> int:
        real_index = math.floor(tick_index / abs(tick_spacing) / RAYDIUM_TICK_ARRAY_SIZE)
        return (real_index + offset) * abs(tick_spacing) * RAYDIUM_TICK_ARRAY_SIZE

    # For zero_to_one (price decreasing): arrays at offset 0, -1, -2
    # For one_to_zero (price increasing): arrays at offset 0, +1, +2
    direction = -1 if is_zero_to_one else 1
    tick_starts = [
        get_start_tick_index(tick_current, 0),
        get_start_tick_index(tick_current, direction),
        get_start_tick_index(tick_current, 2 * direction),
    ]

    tick_array_0 = _get_raydium_clmm_tick_array_address(pool.pool_address, tick_starts[0])
    tick_array_1 = _get_raydium_clmm_tick_array_address(pool.pool_address, tick_starts[1])
    tick_array_2 = _get_raydium_clmm_tick_array_address(pool.pool_address, tick_starts[2])

    # Instruction data: swap_v2 discriminator + params
    discriminator = bytes([43, 4, 237, 11, 26, 201, 30, 98])
    other_amount_threshold = 0  # min output (0 = accept any)

    sqrt_price_limit = RAYDIUM_CLMM_MIN_SQRT_PRICE_X64 if is_zero_to_one else RAYDIUM_CLMM_MAX_SQRT_PRICE_X64
    sqrt_lower = sqrt_price_limit & 0xFFFFFFFFFFFFFFFF
    sqrt_upper = sqrt_price_limit >> 64

    instruction_data = discriminator + struct.pack(
        '<QQQQB',
        int(amount_in),                 # amount: u64
        int(other_amount_threshold),    # other_amount_threshold: u64
        int(sqrt_lower),                # sqrt_price_limit_x64 low 64 bits
        int(sqrt_upper),                # sqrt_price_limit_x64 high 64 bits
        1,                              # is_base_input: bool (u8) = true (exact input)
    )

    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    token_program_2022 = Pubkey.from_string(TOKEN_2022_PROGRAM_ID)
    memo_program = Pubkey.from_string(MEMO_PROGRAM)

    # 13 fixed accounts + 3 tick arrays as remaining accounts
    accounts = [
        AccountMeta(signer_pubkey, is_signer=True, is_writable=False),       # 0: payer
        AccountMeta(amm_config, is_signer=False, is_writable=False),         # 1: amm_config
        AccountMeta(pool_pubkey, is_signer=False, is_writable=True),         # 2: pool_state
        AccountMeta(input_ata, is_signer=False, is_writable=True),           # 3: input_token_account
        AccountMeta(output_ata, is_signer=False, is_writable=True),          # 4: output_token_account
        AccountMeta(input_vault, is_signer=False, is_writable=True),         # 5: input_vault
        AccountMeta(output_vault, is_signer=False, is_writable=True),        # 6: output_vault
        AccountMeta(observation_key, is_signer=False, is_writable=True),     # 7: observation_state
        AccountMeta(token_program, is_signer=False, is_writable=False),      # 8: token_program
        AccountMeta(token_program_2022, is_signer=False, is_writable=False), # 9: token_program_2022
        AccountMeta(memo_program, is_signer=False, is_writable=False),       # 10: memo_program
        AccountMeta(input_vault_mint, is_signer=False, is_writable=False),   # 11: input_vault_mint
        AccountMeta(output_vault_mint, is_signer=False, is_writable=False),  # 12: output_vault_mint
        # Remaining accounts: tick arrays
        AccountMeta(tick_array_0, is_signer=False, is_writable=True),        # 13: tick_array_0
        AccountMeta(tick_array_1, is_signer=False, is_writable=True),        # 14: tick_array_1
        AccountMeta(tick_array_2, is_signer=False, is_writable=True),        # 15: tick_array_2
    ]

    logger.info(
        f"Raydium CLMM swap_v2: pool={pool.pool_address[:12]}.. "
        f"{'0->1' if is_zero_to_one else '1->0'} amount={amount_in} "
        f"tick={tick_current} spacing={tick_spacing}"
    )

    return Instruction(program_id=program_id, accounts=accounts, data=instruction_data)


# ---------------------------------------------------------------------------
# AlphaQ swap instruction builder
# ---------------------------------------------------------------------------

# Sysvar Instructions program ID
SYSVAR_INSTRUCTIONS = "Sysvar1nstructions1111111111111111111111111"


def _fetch_multiple_accounts(rpc_endpoint: str, addresses: list) -> list:
    """Fetch multiple accounts in a single RPC call using getMultipleAccounts."""
    result = _rpc_call(rpc_endpoint, "getMultipleAccounts", [
        addresses,
        {"encoding": "base64", "commitment": "confirmed"},
    ])
    values = result.get("result", {}).get("value", [])
    decoded = []
    for v in values:
        if v is None:
            decoded.append(None)
        else:
            decoded.append({
                "data": base64.b64decode(v["data"][0]),
                "owner": v.get("owner"),
            })
    return decoded


def _find_alphaq_market_state(pool_address: str, pool_data: bytes, rpc_endpoint: str) -> Pubkey:
    """Find the AlphaQ market_state account for a given market (pool).

    AlphaQ uses two accounts per pool:
      - market (larger, ~672 bytes): mints, vaults, config (passed as read-only)
      - market_state (smaller, 336 bytes): mutable price/state data

    Both share the same 16-byte name prefix. We find market_state via
    getProgramAccounts filtered by name + dataSize=336.
    """
    # Extract name from first 16 bytes of pool data
    name_bytes = pool_data[:16]

    # Use getProgramAccounts to find the market_state
    name_b64 = base64.b64encode(name_bytes).decode()
    result = _rpc_call(rpc_endpoint, "getProgramAccounts", [
        ALPHAQ_PROGRAM,
        {
            "encoding": "base64",
            "commitment": "confirmed",
            "filters": [
                {"memcmp": {"offset": 0, "bytes": name_b64, "encoding": "base64"}},
                {"dataSize": 336},
            ],
            "dataSlice": {"offset": 0, "length": 0},
        }
    ])
    accounts = result.get("result", [])
    for acct in accounts:
        pk_str = acct["pubkey"]
        if pk_str != pool_address:
            logger.info(f"AlphaQ market_state found: {pk_str[:20]}..")
            return Pubkey.from_string(pk_str)

    raise RuntimeError(
        f"Could not find AlphaQ market_state for pool {pool_address}. "
        f"Name: {name_bytes[:8].decode('ascii', errors='replace')}"
    )


def _parse_alphaq_market(
    data: bytes, expected_mint_a: str, expected_mint_b: str,
    pool_address: str, rpc_endpoint: str,
) -> dict:
    """Parse AlphaQ market (pool) account to extract vaults and mints.

    AlphaQ market layout (672 bytes):
        0x000: name (16 bytes, e.g. "MSOL-SOL\\0...")
        0x010: flags/config (8 bytes)
        0x018: authority PDA (32 bytes) — ALPHAQuu7...
        0x038: more config
        0x070: vault_a pubkey (32 bytes) — token account for mint_a
        0x090: vault_b pubkey (32 bytes) — token account for mint_b
        0x0B0: (duplicate vault area, 64 bytes)
        0x0F0: mint_a pubkey (32 bytes)
        0x110: mint_b pubkey (32 bytes)
        0x130+: price/state data

    Per real transaction analysis, token_a_authority = vault_a and
    token_b_authority = vault_b (vaults are their own PDAs).
    vendor_key = vault_b (SOL vault used as fee destination).
    """
    if len(data) < 0x130:
        raise ValueError(f"AlphaQ market data too short: {len(data)} bytes")

    # Extract vaults from fixed offsets
    vault_a = Pubkey.from_bytes(data[0x70:0x90])
    vault_b = Pubkey.from_bytes(data[0x90:0xB0])

    # Extract mints
    mint_a = Pubkey.from_bytes(data[0xF0:0x110])
    mint_b = Pubkey.from_bytes(data[0x110:0x130])

    # Validate mints match expected
    parsed_set = {str(mint_a), str(mint_b)}
    expected_set = {expected_mint_a, expected_mint_b}
    if parsed_set != expected_set:
        # Fallback: scan for mints at 8-byte offsets
        found_mints = {}
        for offset in range(0, len(data) - 32, 8):
            pk = Pubkey.from_bytes(data[offset:offset + 32])
            pk_str = str(pk)
            if pk_str in expected_set and pk_str not in found_mints:
                found_mints[pk_str] = (offset, pk)
                if len(found_mints) == 2:
                    break
        if len(found_mints) != 2:
            raise ValueError(
                f"Could not find expected mints in AlphaQ market. "
                f"Expected: {expected_set}, parsed: {parsed_set}"
            )
        sorted_mints = sorted(found_mints.values(), key=lambda x: x[0])
        mint_a = sorted_mints[0][1]
        mint_b = sorted_mints[1][1]
        logger.warning(f"AlphaQ: mints at non-standard offsets, "
                       f"A at 0x{sorted_mints[0][0]:x}, B at 0x{sorted_mints[1][0]:x}")

    logger.info(f"AlphaQ market parsed: mint_a={str(mint_a)[:12]}.. "
                f"mint_b={str(mint_b)[:12]}.. vault_a={str(vault_a)[:12]}.. "
                f"vault_b={str(vault_b)[:12]}..")

    # Validate vaults are real token accounts (single batch RPC call)
    vault_addrs = [str(vault_a), str(vault_b)]
    vault_accts = _fetch_multiple_accounts(rpc_endpoint, vault_addrs)
    for i, (vname, expected_mint) in enumerate([("vault_a", mint_a), ("vault_b", mint_b)]):
        acct = vault_accts[i]
        if acct is None:
            raise ValueError(f"AlphaQ {vname} {vault_addrs[i][:16]}.. not found on-chain")
        if acct["owner"] != TOKEN_PROGRAM_ID:
            raise ValueError(f"AlphaQ {vname} not a token account (owner={acct['owner'][:16]}..)")
        if len(acct["data"]) >= 32:
            actual_mint = Pubkey.from_bytes(acct["data"][0:32])
            if actual_mint != expected_mint:
                logger.warning(f"AlphaQ {vname} mint mismatch: "
                               f"expected {str(expected_mint)[:12]}.. got {str(actual_mint)[:12]}..")

    # Find market_state
    market_state = _find_alphaq_market_state(pool_address, data, rpc_endpoint)

    return {
        "market_state": market_state,
        "mint_a": mint_a,
        "mint_b": mint_b,
        "vault_a": vault_a,
        "vault_b": vault_b,
        # Per real tx analysis: authorities = vaults themselves, vendor = vault_b
        "token_a_authority": vault_a,
        "token_b_authority": vault_b,
        "vendor_key": vault_b,
    }


def _build_alphaq_swap_ix(
    pool: PoolInfo,
    a_to_b: bool,
    amount_in: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
) -> Optional[Instruction]:
    """Build an AlphaQ swap instruction per official IDL.

    IDL instruction "Swap" (discriminant u8 = 12):
        Args: aToB(u8) + amountIn(u64) + minAmountOut(u64)

    Account list (12 accounts, from IDL):
        0:  user (signer, writable)
        1:  market (readonly)
        2:  market_state (writable)
        3:  user_token_account_a (writable)
        4:  user_token_account_b (writable)
        5:  vault_token_account_a (writable)
        6:  vault_token_account_b (writable)
        7:  token_a_authority (writable)
        8:  token_b_authority (writable)
        9:  vendor_key (writable)
        10: token_program (readonly)
        11: instructions_sysvar (readonly)
    """
    program_id = Pubkey.from_string(ALPHAQ_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool.pool_address)
    signer_pubkey = signer_keypair.pubkey()

    # Fetch market (pool) account data
    raw_data = fetch_pool_state(pool.pool_address, rpc_endpoint)
    if raw_data is None:
        logger.error(f"AlphaQ pool {pool.pool_address} not found on-chain")
        return None

    try:
        state = _parse_alphaq_market(
            raw_data, pool.token_a_mint, pool.token_b_mint,
            pool.pool_address, rpc_endpoint,
        )
    except Exception as e:
        logger.error(f"Failed to parse AlphaQ market: {e}")
        return None

    market_state = state["market_state"]
    mint_a = state["mint_a"]
    mint_b = state["mint_b"]
    vault_a = state["vault_a"]
    vault_b = state["vault_b"]
    token_a_authority = state["token_a_authority"]
    token_b_authority = state["token_b_authority"]
    vendor_key = state["vendor_key"]

    # User ATAs
    user_ata_a = get_associated_token_address(signer_pubkey, mint_a)
    user_ata_b = get_associated_token_address(signer_pubkey, mint_b)

    # Build instruction data per IDL:
    # discriminant(u8=12) + aToB(u8) + amountIn(u64) + minAmountOut(u64)
    a_to_b_flag = 1 if a_to_b else 0
    min_amount_out = 1  # minimal slippage protection
    instruction_data = struct.pack(
        '<BBQQ',
        12,                     # discriminant
        a_to_b_flag,            # aToB
        int(amount_in),         # amountIn
        int(min_amount_out),    # minAmountOut
    )

    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    sysvar_ix = Pubkey.from_string(SYSVAR_INSTRUCTIONS)

    # Account layout verified against real AlphaQ swap transactions:
    #   [0]: user (signer)
    #   [1]: market = pool_address (read-only config)
    #   [2]: market_state (writable state, separate account)
    #   [3]: user_token_account_a
    #   [4]: user_token_account_b
    #   [5]: vault_token_account_a
    #   [6]: vault_token_account_b
    #   [7]: token_a_authority = vault_a (vaults are self-authorizing PDAs)
    #   [8]: token_b_authority = vault_b
    #   [9]: vendor_key = vault_b (SOL vault as fee destination)
    #  [10]: token_program
    #  [11]: instructions_sysvar
    accounts = [
        AccountMeta(signer_pubkey, is_signer=True, is_writable=True),           # user
        AccountMeta(pool_pubkey, is_signer=False, is_writable=False),           # market (pool = read-only)
        AccountMeta(market_state, is_signer=False, is_writable=True),           # market_state
        AccountMeta(user_ata_a, is_signer=False, is_writable=True),             # user_token_account_a
        AccountMeta(user_ata_b, is_signer=False, is_writable=True),             # user_token_account_b
        AccountMeta(vault_a, is_signer=False, is_writable=True),                # vault_token_account_a
        AccountMeta(vault_b, is_signer=False, is_writable=True),                # vault_token_account_b
        AccountMeta(token_a_authority, is_signer=False, is_writable=True),      # token_a_authority
        AccountMeta(token_b_authority, is_signer=False, is_writable=True),      # token_b_authority
        AccountMeta(vendor_key, is_signer=False, is_writable=True),             # vendor_key
        AccountMeta(token_program, is_signer=False, is_writable=False),         # token_program
        AccountMeta(sysvar_ix, is_signer=False, is_writable=False),             # instructions_sysvar
    ]

    return Instruction(program_id=program_id, accounts=accounts, data=instruction_data)


# ---------------------------------------------------------------------------
# Meteora DAMM (Dynamic AMM) swap instruction builder
# ---------------------------------------------------------------------------

# Cache for DAMM pool auxiliary accounts fetched via getAccountInfo.
# Key: pool_address -> dict of parsed accounts.
_damm_pool_cache: Dict[str, dict] = {}

DAMM_V2_DISCRIMINATOR = bytes([241, 154, 109, 4, 17, 177, 109, 188])
DAMM_SWAP_DISCRIMINATOR = bytes([248, 198, 158, 145, 225, 117, 135, 200])


def _parse_damm_pool_state(data: bytes) -> dict:
    """Parse Meteora DAMM v2 pool account data.

    Pool layout (944 bytes, v2 discriminator [241,154,109,4,17,177,109,188]):
        offset  0: discriminator (8 bytes)
        offset  8: pubkey_0 — LP mint (32 bytes)
        offset 40: token_a_mint (32 bytes)
        offset 72: token_b_mint (32 bytes)
        offset 104: a_vault (reserve/vault for token A) (32 bytes)
        offset 136: b_vault (reserve/vault for token B) (32 bytes)
        offset 168: a_vault_lp (vault A LP token account) (32 bytes)
        offset 200: b_vault_lp (vault B LP token account) (32 bytes)
        offset 232: a_vault_lp_bump (u8)
        offset 233: enabled (u8)
        ... more fields follow
    """
    if len(data) < 264:
        raise ValueError(f"DAMM pool data too short: {len(data)} bytes (need >= 264)")

    disc = data[0:8]
    if disc != DAMM_V2_DISCRIMINATOR:
        raise ValueError(f"DAMM discriminator mismatch: got {list(disc)}, "
                         f"expected {list(DAMM_V2_DISCRIMINATOR)}")

    def pk(offset):
        return Pubkey.from_bytes(data[offset:offset + 32])

    return {
        "lp_mint": pk(8),
        "token_a_mint": pk(40),
        "token_b_mint": pk(72),
        "a_vault": pk(104),
        "b_vault": pk(136),
        "a_vault_lp": pk(168),
        "b_vault_lp": pk(200),
    }


def _fetch_damm_vault_token(vault_address: Pubkey, rpc_endpoint: str) -> Optional[Pubkey]:
    """Fetch the token account (SPL token vault) stored inside a Mercurial/Meteora vault.

    Mercurial vault layout:
        offset 0: discriminator (8 bytes)
        offset 8: ... various fields
        offset 168: token_vault pubkey (32 bytes) — the actual SPL token account holding funds

    If the vault layout doesn't match, try common offsets.
    """
    raw = fetch_pool_state(str(vault_address), rpc_endpoint)
    if raw is None:
        return None
    # Mercurial vault stores the token_vault at offset 168
    if len(raw) >= 200:
        return Pubkey.from_bytes(raw[168:200])
    return None


def _derive_damm_protocol_fee_token(
    token_mint: Pubkey,
    pool_pubkey: Pubkey,
) -> Pubkey:
    """Derive the protocol fee token account PDA for DAMM.

    Uses seeds: [pool_address, token_mint, "fee"] under the DAMM program.
    """
    program_id = Pubkey.from_string(METEORA_DAMM_PROGRAM)
    pda, _ = Pubkey.find_program_address(
        [bytes(pool_pubkey), bytes(token_mint), b"fee"],
        program_id,
    )
    return pda


def _build_damm_swap_ix(
    pool: PoolInfo,
    a_to_b: bool,
    amount_in: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
) -> Optional[Instruction]:
    """Build a Meteora DAMM (Dynamic AMM) swap instruction.

    Account layout (16 accounts):
        0: pool — writable
        1: pool LP token mint or fee account — writable
        2: user_source_token — writable
        3: a_vault — writable
        4: b_vault — writable
        5: user_dest_token — writable
        6: token_a_mint — read-only
        7: token_b_mint — read-only
        8: vault_a_lp_token — writable
        9: a_vault_lp (from pool offset 168) — writable
        10: b_vault_lp (from pool offset 200) — writable
        11: protocol_fee_token — writable
        12: user/signer — signer
        13: vault_program — read-only
        14: token_program — read-only
        15: lp_mint — read-only
    """
    program_id = Pubkey.from_string(METEORA_DAMM_PROGRAM)
    pool_pubkey = Pubkey.from_string(pool.pool_address)

    # Check cache first
    cache_key = pool.pool_address
    if cache_key in _damm_pool_cache:
        state = _damm_pool_cache[cache_key]
    else:
        raw_data = fetch_pool_state(pool.pool_address, rpc_endpoint)
        if raw_data is None:
            logger.error(f"DAMM pool {pool.pool_address} not found on-chain")
            return None

        try:
            state = _parse_damm_pool_state(raw_data)
        except Exception as e:
            logger.error(f"Failed to parse DAMM pool state: {e}")
            return None

        # Fetch the vault token accounts (the actual SPL token vaults inside
        # the Mercurial vaults). These are accounts #9 and #10 in the instruction.
        a_vault_token = _fetch_damm_vault_token(state["a_vault"], rpc_endpoint)
        b_vault_token = _fetch_damm_vault_token(state["b_vault"], rpc_endpoint)

        if a_vault_token is None or b_vault_token is None:
            logger.error(f"DAMM: failed to fetch vault token accounts for pool {pool.pool_address}")
            return None

        state["a_vault_token"] = a_vault_token
        state["b_vault_token"] = b_vault_token

        _damm_pool_cache[cache_key] = state

    token_a_mint = state["token_a_mint"]
    token_b_mint = state["token_b_mint"]
    a_vault = state["a_vault"]
    b_vault = state["b_vault"]
    a_vault_lp = state["a_vault_lp"]
    b_vault_lp = state["b_vault_lp"]
    a_vault_token = state["a_vault_token"]
    b_vault_token = state["b_vault_token"]
    lp_mint = state["lp_mint"]

    # Determine actual swap direction from on-chain mints
    input_mint_pk = Pubkey.from_string(pool.token_a_mint if a_to_b else pool.token_b_mint)
    is_a_to_b = (input_mint_pk == token_a_mint)

    signer_pubkey = signer_keypair.pubkey()

    # Hardcoded pool accounts extracted from real Jupiter transactions.
    # The DAMM account structure is complex (Mercurial vaults, LP tokens, protocol fees)
    # and PDA derivations don't match standard patterns. These are verified on-chain.
    DAMM_POOL_ACCOUNTS = {
        # edgeSOL/SOL pool (from tx 4AX6vzHb...)
        "7AtUeAW4TKPEXkR41bawBnwyemKXL4pCPrWP5tXcPMSA": {
            "lp_account": "9boNsHGdzNJSfyuZFgPeQC7bGQfDEvokTFLKhzkx8mVD",
            "a_vault": "FERjPVNEa7Udq8CEv68h6tPL46Tq7ieE49HrE2wea3XT",
            "b_vault": "6y93C5iNFAqqpamYWxJM4cS5kj9pVSBmT16Kq2Lx54fm",
            "token_a_acct": "2DukbzRSWxE6NweX4vGYSTz3QVXGCpbxGjHQZerNWHvo",
            "token_b_acct": "FZN7QZ8ZUUAxMPfxYEYkH3cXUASzH8EqA6B4tyCL8f1j",
            "vault_a_lp": "7gdDXCTHQA6LGbPsiUsKgSmaYRL198i9fVPuHnvcqGfs",
            "vault_a_token": "3N1EAP7FgRVBapCTx2oTSaRtghmDGzpgPcVjRxi9WSuq",
            "vault_b_token": "4bbEqq2D1yxxsfT81pA1Kb3AqfrnncTfdzj6JEhSKYhz",
            "protocol_fee": "Dy1NwpzBU39iinHYakkGQ7kx7k1jMDjE8xiQf2z1osZn",
            "pool_mint": "edgejNWAqkePLpi5sHRxT9vHi7u3kSHP9cocABPKiWZ",
            "token_a_mint": SOL_MINT,
            "token_b_mint": EDGESOL_MINT,
        },
        # vSOL/SOL pool (from tx 5sVL6h8p...)
        "7TY9HFLwy8BpS1sGNzYt281YY9WP4V1TzKcYbYft5SD1": {
            "lp_account": "FJFdwj6pUFWLye51e6q5d9tm6A9SGm9E8Z5ncCXovuom",
            "a_vault": "FERjPVNEa7Udq8CEv68h6tPL46Tq7ieE49HrE2wea3XT",
            "b_vault": "21Mxxv6dzUNG9Q6MWiP1irww2Vck2UBJYkKMyM5Sbg2a",
            "token_a_acct": "F1hpWXgg8HyxxjSWH92DctGRc4N4Di7J1u635JxKcrGA",
            "token_b_acct": "FZN7QZ8ZUUAxMPfxYEYkH3cXUASzH8EqA6B4tyCL8f1j",
            "vault_a_lp": "AyWh3aajhZHFhdjks1MUkpgWtrgxftZZkuUKbaRgFp8k",
            "vault_a_token": "GKVfP6rtiA2hXEq27Az98SrNNfBRwbyscVRUK4KYxG2n",
            "vault_b_token": "2CtXAUsnsHNndqbjGthSr36o5nCXABqZ62ctbFMsfMnD",
            "vault_b_lp": "Bf85GTLhH6EUqgZvUcXE9PDy7kvGh1dggRuR4yMSTNJ6",
            "protocol_fee": "Bf85GTLhH6EUqgZvUcXE9PDy7kvGh1dggRuR4yMSTNJ6",
            "pool_mint": "Fu9BYC6tWBo1KMKaP3CFoKfRhqv9akmy3DuYwnCyWiyC",
            "token_a_mint": SOL_MINT,
            "token_b_mint": VSOL_MINT,
        },
    }

    pool_accts = DAMM_POOL_ACCOUNTS.get(pool.pool_address)
    if pool_accts is None:
        logger.warning(f"DAMM pool {pool.pool_address[:16]} not in hardcoded accounts")
        return None

    signer_pubkey = signer_keypair.pubkey()
    token_a_mint_pk = Pubkey.from_string(pool_accts["token_a_mint"])
    token_b_mint_pk = Pubkey.from_string(pool_accts["token_b_mint"])

    # Determine swap direction
    input_mint_pk = Pubkey.from_string(pool.token_a_mint if a_to_b else pool.token_b_mint)
    is_a_to_b = (input_mint_pk == token_a_mint_pk)

    if is_a_to_b:
        user_source_token = get_associated_token_address(signer_pubkey, token_a_mint_pk)
        user_dest_token = get_associated_token_address(signer_pubkey, token_b_mint_pk)
        # A→B: accounts in natural order (matching the reference tx)
        vault_in = pool_accts["a_vault"]
        vault_out = pool_accts["b_vault"]
        token_in_acct = pool_accts["token_a_acct"]
        token_out_acct = pool_accts["token_b_acct"]
        vault_in_lp = pool_accts["vault_a_lp"]
        vault_in_token = pool_accts["vault_a_token"]
        vault_out_token = pool_accts["vault_b_token"]
    else:
        user_source_token = get_associated_token_address(signer_pubkey, token_b_mint_pk)
        user_dest_token = get_associated_token_address(signer_pubkey, token_a_mint_pk)
        # B→A: swap vault ordering — input is B, output is A
        vault_in = pool_accts["b_vault"]
        vault_out = pool_accts["a_vault"]
        token_in_acct = pool_accts["token_b_acct"]
        token_out_acct = pool_accts["token_a_acct"]
        vault_in_lp = pool_accts.get("vault_b_lp", pool_accts["vault_a_lp"])
        vault_in_token = pool_accts["vault_b_token"]
        vault_out_token = pool_accts["vault_a_token"]

    vault_program = Pubkey.from_string(METEORA_DAMM_VAULT_PROGRAM)
    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)

    min_amount_out = 1
    instruction_data = DAMM_SWAP_DISCRIMINATOR + struct.pack('<QQ', int(amount_in), min_amount_out)

    accounts = [
        AccountMeta(pool_pubkey, is_signer=False, is_writable=True),                                # 0: pool
        AccountMeta(Pubkey.from_string(pool_accts["lp_account"]), is_signer=False, is_writable=True), # 1: lp
        AccountMeta(user_source_token, is_signer=False, is_writable=True),                           # 2: user src
        AccountMeta(Pubkey.from_string(vault_in), is_signer=False, is_writable=True),               # 3: input vault
        AccountMeta(Pubkey.from_string(vault_out), is_signer=False, is_writable=True),              # 4: output vault
        AccountMeta(user_dest_token, is_signer=False, is_writable=True),                             # 5: user dst
        AccountMeta(Pubkey.from_string(token_in_acct), is_signer=False, is_writable=True),          # 6: input token acct
        AccountMeta(Pubkey.from_string(token_out_acct), is_signer=False, is_writable=True),         # 7: output token acct
        AccountMeta(Pubkey.from_string(vault_in_lp), is_signer=False, is_writable=True),            # 8: input vault LP
        AccountMeta(Pubkey.from_string(vault_in_token), is_signer=False, is_writable=True),         # 9: input vault token
        AccountMeta(Pubkey.from_string(vault_out_token), is_signer=False, is_writable=True),        # 10: output vault token
        AccountMeta(Pubkey.from_string(pool_accts["protocol_fee"]), is_signer=False, is_writable=True), # 11: proto fee
        AccountMeta(signer_pubkey, is_signer=True, is_writable=True),                                # 12: user
        AccountMeta(vault_program, is_signer=False, is_writable=False),                              # 13: vault prog
        AccountMeta(token_program, is_signer=False, is_writable=False),                              # 14: token prog
        AccountMeta(Pubkey.from_string(pool_accts["pool_mint"]), is_signer=False, is_writable=False), # 15: pool mint
    ]

    return Instruction(program_id=program_id, accounts=accounts, data=instruction_data)


# ---------------------------------------------------------------------------
# Pool verification utility
# ---------------------------------------------------------------------------

def verify_pools(rpc_endpoint: str) -> dict:
    """Verify all registered pools exist on-chain and are owned by the expected program.

    Returns dict of pool_address -> {exists, owner, expected_owner, ok}.
    """
    expected_owners = {
        DEX_WHIRLPOOL: WHIRLPOOL_PROGRAM,
        DEX_METEORA: METEORA_DLMM_PROGRAM,
        DEX_MANIFEST: MANIFEST_PROGRAM,
        DEX_PANCAKESWAP: PANCAKESWAP_CLMM_PROGRAM,
        DEX_ALPHAQ: ALPHAQ_PROGRAM,
        DEX_DAMM: METEORA_DAMM_PROGRAM,
    }

    results = {}
    for key, pool in POOL_REGISTRY.items():
        owner = fetch_account_owner(pool.pool_address, rpc_endpoint)
        expected = expected_owners.get(pool.dex, "unknown")
        ok = (owner == expected)
        mints = sorted(key)
        results[pool.pool_address] = {
            "dex": pool.dex,
            "mints": [m[:8] + ".." for m in mints],
            "exists": owner is not None,
            "owner": owner,
            "expected_owner": expected,
            "ok": ok,
            "enabled": pool.enabled,
        }
        status = "OK" if ok else ("NOT FOUND" if owner is None else f"WRONG OWNER: {owner}")
        enabled_str = "" if pool.enabled else " [DISABLED]"
        logger.info(f"  {pool.pool_address[:12]}.. ({pool.dex}): {status}{enabled_str}")

    return results


# ---------------------------------------------------------------------------
# CLI entry point for verification
# ---------------------------------------------------------------------------

def simulate_swap(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    signer_keypair: Keypair,
    rpc_endpoint: str,
    priority_fee_lamports: int = 10_000,
) -> dict:
    """Build a swap transaction and simulate it via RPC (free, no SOL spent).

    Returns the simulateTransaction result dict.
    """
    signed_b64, sig_str = build_direct_swap_tx(
        input_mint, output_mint, amount_raw,
        signer_keypair, rpc_endpoint, priority_fee_lamports,
    )
    result = _rpc_call(rpc_endpoint, "simulateTransaction", [
        signed_b64,
        {"encoding": "base64", "commitment": "processed"},
    ])
    sim = result.get("result", {})
    return {
        "signature": sig_str,
        "err": sim.get("value", {}).get("err"),
        "logs": sim.get("value", {}).get("logs", []),
        "units_consumed": sim.get("value", {}).get("unitsConsumed"),
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rpc = sys.argv[1] if len(sys.argv) > 1 else "https://api.mainnet-beta.solana.com"

    # If --simulate flag, run a test simulation
    if "--simulate" in sys.argv:
        # Usage: python direct_swap.py <rpc> --simulate <input_mint> <output_mint> <amount_raw> <keypair_path>
        args = [a for a in sys.argv[1:] if a != "--simulate"]
        if len(args) < 4:
            print("Usage: python direct_swap.py <rpc> --simulate <input_mint> <output_mint> <amount_raw> <keypair_path>")
            print("  Example: python direct_swap.py https://api.mainnet-beta.solana.com --simulate "
                  f"{SOL_MINT} {BSOL_MINT} 10000000 /home/ubuntu/leeroy-mainnet.json")
            sys.exit(1)

        rpc_url = args[0]
        in_mint = args[1]
        out_mint = args[2]
        amount = int(args[3])
        kp_path = args[4] if len(args) > 4 else "/home/ubuntu/leeroy-mainnet.json"

        kp = Keypair.from_bytes(bytes(json.load(open(kp_path))))
        pool = get_pool_info(in_mint, out_mint)
        if pool is None:
            print(f"No pool found for {in_mint[:12]}.. -> {out_mint[:12]}..")
            sys.exit(1)

        print(f"Simulating swap on {pool.dex} pool {pool.pool_address[:16]}..")
        print(f"  Input:  {in_mint[:12]}.. amount={amount}")
        print(f"  Output: {out_mint[:12]}..")
        print(f"  Signer: {kp.pubkey()}")
        print()

        try:
            result = simulate_swap(in_mint, out_mint, amount, kp, rpc_url)
        except Exception as e:
            print(f"ERROR building tx: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

        if result["err"] is None:
            print(f"  SIMULATION PASSED (no error)")
        else:
            print(f"  SIMULATION FAILED: {result['err']}")

        print(f"  CU consumed: {result.get('units_consumed', 'N/A')}")
        print(f"  Signature:   {result['signature']}")

        if result.get("logs"):
            print(f"\n  Logs ({len(result['logs'])} lines):")
            for line in result["logs"]:
                print(f"    {line}")

        sys.exit(0 if result["err"] is None else 1)

    print(f"Verifying pool registry against {rpc}...\n")
    results = verify_pools(rpc)
    print()

    ok_count = sum(1 for r in results.values() if r["ok"])
    total = len(results)
    enabled = sum(1 for r in results.values() if r["enabled"])
    print(f"Results: {ok_count}/{total} verified, {enabled}/{total} enabled")

    for addr, r in results.items():
        mark = "PASS" if r["ok"] else "FAIL"
        en = "" if r["enabled"] else " [disabled]"
        print(f"  [{mark}] {addr[:16]}.. {r['dex']:20s} mints={r['mints']}{en}")
        if not r["ok"]:
            print(f"         expected owner: {r['expected_owner']}")
            print(f"         actual owner:   {r['owner']}")
