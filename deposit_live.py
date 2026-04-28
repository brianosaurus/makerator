"""On-chain Deposit test for makerator.

Deposits a small amount of base or quote token from the wallet's ATA into
the market's vault, crediting the trader's ClaimedSeat. Validates the
Deposit ix encoder + the SwQOS tipless submission path.

Pre-reqs (from prior phases):
- ClaimSeat already done on this market (run claim_seat_live.py first)
- Wallet has the chosen token in its ATA

Defaults to depositing jupSOL (base) on jupSOL/SOL market.
"""
import argparse
import base64
import json
import logging
import os
import sys
import time
import urllib.request
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import manifest

# Manifest jupSOL/SOL market
JUPSOL_SOL_MARKET = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("deposit")


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 15) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def find_ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Compute the associated token account address for owner+mint (SPL Token)."""
    seeds = [bytes(owner), bytes(manifest.TOKEN_PROGRAM_ID), bytes(mint)]
    ata, _bump = Pubkey.find_program_address(seeds, ATA_PROGRAM)
    return ata


def fetch_account(rpc_url: str, pubkey: Pubkey) -> Optional[bytes]:
    resp = rpc_call(rpc_url, "getAccountInfo", [str(pubkey), {"encoding": "base64"}])
    info = resp.get("result", {}).get("value")
    if not info:
        return None
    return base64.b64decode(info["data"][0])


def fetch_token_balance(rpc_url: str, ata: Pubkey) -> int:
    """Return raw atom balance of an SPL token ATA, or 0 if missing."""
    resp = rpc_call(rpc_url, "getTokenAccountBalance", [str(ata)])
    val = resp.get("result", {}).get("value")
    if not val:
        return 0
    return int(val.get("amount", "0"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=str(JUPSOL_SOL_MARKET))
    ap.add_argument("--side", choices=("base", "quote"), default="base",
                    help="base = deposit jupSOL into base_vault, quote = wSOL → quote_vault")
    ap.add_argument("--amount-sol", type=float, default=0.01,
                    help="Amount in human units (default 0.01 = 1% of a SOL/jupSOL)")
    ap.add_argument("--keypair", default=os.getenv("KEYPAIR_FILE", "/home/ubuntu/leeroy-mainnet.json"))
    ap.add_argument("--rpc", default=os.getenv("SOLANA_RPC_URL"))
    ap.add_argument("--swqos", default=os.getenv("SWQOS_ENDPOINT"),
                    help="SwQOS endpoint (tipless submission)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-confirm", action="store_true")
    args = ap.parse_args()

    if not args.swqos:
        logger.error("SWQOS_ENDPOINT not set in env")
        return 2

    market = Pubkey.from_string(args.market)
    logger.info(f"market:   {market}")
    logger.info(f"side:     {args.side}")
    logger.info(f"submit:   SwQOS ({args.swqos[:60]}{'...' if len(args.swqos) > 60 else ''})")

    # Load keypair
    with open(args.keypair) as f:
        kp = Keypair.from_bytes(bytes(json.load(f)))
    payer = kp.pubkey()
    logger.info(f"payer:    {payer}")

    # Read market header → vault + mint addresses + decimals
    market_data = fetch_account(args.rpc, market)
    if not market_data:
        logger.error("market account not found")
        return 2
    h = manifest.parse_market_header(market_data)
    if args.side == "base":
        mint = h.base_mint
        vault = h.base_vault
        decimals = h.base_decimals
        label = "base (jupSOL)"
    else:
        mint = h.quote_mint
        vault = h.quote_vault
        decimals = h.quote_decimals
        label = "quote (wSOL)"
    logger.info(f"depositing: {label} mint={mint}")
    logger.info(f"  vault:    {vault}")

    amount_atoms = int(args.amount_sol * (10 ** decimals))
    logger.info(f"amount:   {args.amount_sol} ({amount_atoms} atoms)")

    # Find user's ATA + check balance
    ata = find_ata(payer, mint)
    bal = fetch_token_balance(args.rpc, ata)
    bal_human = bal / (10 ** decimals)
    logger.info(f"user ATA: {ata}")
    logger.info(f"balance:  {bal_human} ({bal} atoms)")
    if bal < amount_atoms:
        logger.error(f"insufficient balance: have {bal}, need {amount_atoms}")
        return 2

    # Build Deposit ix
    deposit_ix = manifest.build_deposit_ix(
        payer=payer,
        market=market,
        trader_token_account=ata,
        market_vault=vault,
        mint=mint,
        amount_atoms=amount_atoms,
        trader_index_hint=None,  # let program look up our seat by traverse
    )
    logger.info(f"ix data:  {deposit_ix.data.hex()} ({len(deposit_ix.data)}B)")
    logger.info(f"ix accts: {len(deposit_ix.accounts)} "
                f"[payer/sw, market/w, ata/w, vault/w, token_program/r, mint/r]")

    # Recent blockhash
    bh = rpc_call(args.rpc, "getLatestBlockhash", [{"commitment": "processed"}])
    blockhash_str = bh["result"]["value"]["blockhash"]
    logger.info(f"blockhash:{blockhash_str}")

    # Compile + sign
    msg = MessageV0.try_compile(
        payer=payer,
        instructions=[deposit_ix],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(blockhash_str),
    )
    tx = VersionedTransaction(msg, [kp])
    tx_bytes = bytes(tx)
    tx_b64 = base64.b64encode(tx_bytes).decode()
    sig = str(tx.signatures[0])
    logger.info(f"tx size:  {len(tx_bytes)}B  sig={sig}")

    # Simulate
    sim = rpc_call(args.rpc, "simulateTransaction", [
        tx_b64, {"encoding": "base64", "commitment": "processed", "sigVerify": False}
    ])
    sim_val = sim.get("result", {}).get("value", {})
    sim_err = sim_val.get("err")
    logger.info(f"simulate: err={sim_err}  units={sim_val.get('unitsConsumed')}")
    for line in (sim_val.get("logs") or [])[-12:]:
        logger.info(f"  log| {line}")
    if sim_err is not None:
        logger.error(f"sim failed, aborting: {sim_err}")
        return 3

    if args.dry_run:
        logger.info("dry-run, not submitting")
        return 0

    if not args.no_confirm and sys.stdin.isatty():
        if input("submit live via SwQOS? [y/N] ").strip().lower() != "y":
            return 0

    # Submit via SwQOS — plain sendTransaction RPC, no tip
    submit = rpc_call(args.swqos, "sendTransaction", [
        tx_b64, {"encoding": "base64", "skipPreflight": True}
    ])
    if submit.get("error"):
        logger.error(f"SwQOS sendTransaction error: {submit['error']}")
        return 4
    relay_sig = submit.get("result")
    logger.info(f"SwQOS accepted: {relay_sig}")

    # Confirm
    deadline = time.time() + 60
    while time.time() < deadline:
        resp = rpc_call(args.rpc, "getSignatureStatuses", [[sig]])
        status = (resp.get("result", {}).get("value") or [None])[0]
        if status:
            err = status.get("err")
            cstatus = status.get("confirmationStatus")
            slot = status.get("slot")
            logger.info(f"status:   slot={slot} status={cstatus} err={err}")
            if err:
                logger.error(f"on-chain error: {err}")
                return 5
            if cstatus in ("confirmed", "finalized"):
                logger.info(f"OK — deposited {args.amount_sol} {label} into market vault")
                logger.info(f"explorer: https://solscan.io/tx/{sig}")
                return 0
        time.sleep(2)

    logger.error("timeout waiting for confirmation")
    return 6


if __name__ == "__main__":
    sys.exit(main())
