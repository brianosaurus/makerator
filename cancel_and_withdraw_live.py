"""On-chain CancelOrder + Withdraw test for makerator.

Cancels a specific resting order by sequence_number, then withdraws all
base-side balance from the seat back to the trader's ATA.

Both operations in ONE tx (BatchUpdate cancels[] + Withdraw ix), so we
verify a multi-ix tx submission via SwQOS too.

Defaults are tuned for the order placed by place_order_live.py:
  sequence_number=377748, side=base.
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

ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
JUPSOL_SOL_MARKET = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cancel+wd")


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 15) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def find_ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    seeds = [bytes(owner), bytes(manifest.TOKEN_PROGRAM_ID), bytes(mint)]
    return Pubkey.find_program_address(seeds, ATA_PROGRAM)[0]


def fetch_account(rpc_url: str, pubkey: Pubkey) -> Optional[bytes]:
    resp = rpc_call(rpc_url, "getAccountInfo", [str(pubkey), {"encoding": "base64"}])
    info = resp.get("result", {}).get("value")
    if not info:
        return None
    return base64.b64decode(info["data"][0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=str(JUPSOL_SOL_MARKET))
    ap.add_argument("--seq", type=int, default=377748,
                    help="order_sequence_number to cancel")
    ap.add_argument("--hint", type=int, default=4160,
                    help="order_index_hint (for fast cancel)")
    ap.add_argument("--side", choices=("base", "quote"), default="base")
    ap.add_argument("--withdraw-amount", type=float, default=0.01,
                    help="Amount to withdraw (default 0.01)")
    ap.add_argument("--keypair", default=os.getenv("KEYPAIR_FILE", "/home/ubuntu/leeroy-mainnet.json"))
    ap.add_argument("--rpc", default=os.getenv("SOLANA_RPC_URL"))
    ap.add_argument("--swqos", default=os.getenv("SWQOS_ENDPOINT"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-confirm", action="store_true")
    args = ap.parse_args()

    market = Pubkey.from_string(args.market)
    logger.info(f"market: {market}")
    logger.info(f"cancel: seq={args.seq} hint={args.hint}")
    logger.info(f"withdraw: {args.withdraw_amount} ({args.side})")

    # Load keypair
    with open(args.keypair) as f:
        kp = Keypair.from_bytes(bytes(json.load(f)))
    payer = kp.pubkey()
    logger.info(f"payer:  {payer}")

    # Read market header
    market_data = fetch_account(args.rpc, market)
    if not market_data:
        logger.error("market not found")
        return 2
    h = manifest.parse_market_header(market_data)
    if args.side == "base":
        mint, vault, decimals = h.base_mint, h.base_vault, h.base_decimals
    else:
        mint, vault, decimals = h.quote_mint, h.quote_vault, h.quote_decimals
    ata = find_ata(payer, mint)
    amount_atoms = int(args.withdraw_amount * (10 ** decimals))
    logger.info(f"mint:    {mint}")
    logger.info(f"vault:   {vault}")
    logger.info(f"ata:     {ata}")

    # Build two ixs: BatchUpdate(cancel) + Withdraw
    cancel = manifest.CancelOrderParams(
        order_sequence_number=args.seq,
        order_index_hint=args.hint,
    )
    cancel_ix = manifest.build_batch_update_ix(payer, market, cancels=[cancel])
    withdraw_ix = manifest.build_withdraw_ix(
        payer=payer, market=market,
        trader_token_account=ata, market_vault=vault,
        mint=mint, amount_atoms=amount_atoms,
    )
    logger.info(f"cancel ix:   {cancel_ix.data.hex()} ({len(cancel_ix.data)}B)")
    logger.info(f"withdraw ix: {withdraw_ix.data.hex()} ({len(withdraw_ix.data)}B)")

    # Compile + sign
    bh = rpc_call(args.rpc, "getLatestBlockhash", [{"commitment": "processed"}])
    blockhash_str = bh["result"]["value"]["blockhash"]
    msg = MessageV0.try_compile(
        payer=payer,
        instructions=[cancel_ix, withdraw_ix],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(blockhash_str),
    )
    tx = VersionedTransaction(msg, [kp])
    tx_bytes = bytes(tx)
    tx_b64 = base64.b64encode(tx_bytes).decode()
    sig = str(tx.signatures[0])
    logger.info(f"tx size: {len(tx_bytes)}B  sig={sig}")

    sim = rpc_call(args.rpc, "simulateTransaction", [
        tx_b64, {"encoding": "base64", "commitment": "processed", "sigVerify": False}
    ])
    sim_val = sim.get("result", {}).get("value", {})
    sim_err = sim_val.get("err")
    logger.info(f"simulate: err={sim_err} units={sim_val.get('unitsConsumed')}")
    for line in (sim_val.get("logs") or [])[-15:]:
        logger.info(f"  log| {line}")
    if sim_err is not None:
        logger.error(f"sim failed: {sim_err}")
        return 3

    if args.dry_run:
        logger.info("dry-run, not submitting")
        return 0

    if not args.no_confirm and sys.stdin.isatty():
        if input("submit live? [y/N] ").strip().lower() != "y":
            return 0

    submit = rpc_call(args.swqos, "sendTransaction", [
        tx_b64, {"encoding": "base64", "skipPreflight": True}
    ])
    if submit.get("error"):
        logger.error(f"SwQOS error: {submit['error']}")
        return 4
    logger.info(f"SwQOS accepted: {submit.get('result')}")

    # Confirm
    deadline = time.time() + 60
    while time.time() < deadline:
        resp = rpc_call(args.rpc, "getSignatureStatuses", [[sig]])
        status = (resp.get("result", {}).get("value") or [None])[0]
        if status:
            err = status.get("err")
            cstatus = status.get("confirmationStatus")
            slot = status.get("slot")
            logger.info(f"status: slot={slot} status={cstatus} err={err}")
            if err:
                logger.error(f"on-chain error: {err}")
                return 5
            if cstatus in ("confirmed", "finalized"):
                logger.info(f"OK — cancelled + withdrew")
                logger.info(f"explorer: https://solscan.io/tx/{sig}")
                return 0
        time.sleep(2)

    logger.error("timeout")
    return 6


if __name__ == "__main__":
    sys.exit(main())
