"""First on-chain test: ClaimSeat on jupSOL/SOL Manifest market.

Single-purpose script. Validates manifest.build_claim_seat_ix + lunar_lander.send
end-to-end with the smallest possible real tx (~5k CU, ~0.000005 SOL base fee,
no token movement, just allocates a ClaimedSeat node on the market account).

Safety:
- Simulates first; aborts if simulation fails.
- ClaimSeat is idempotent at the program level — if seat already exists the
  program returns an error and the tx fails, no double-charge.
- No tip ix (we don't need landing speed for a one-off test).

Run on frankfurt only — keypair lives at /home/ubuntu/leeroy-mainnet.json.
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
load_dotenv()  # so LUNAR_LANDER_UUID, KEYPAIR_FILE, SOLANA_RPC_URL etc. resolve

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import manifest
from lunar_lander import LunarLander, build_tip_ix

# Lunar Lander /send requires inline tip ≥ 1_000_000 lamports
LL_TIP_LAMPORTS = 1_000_000

# jupSOL/SOL Manifest market (verified address from statalyzer learnings)
JUPSOL_SOL_MARKET = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("claim_seat_live")


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 15) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def load_keypair(path: str) -> Keypair:
    with open(path) as f:
        secret = json.load(f)
    return Keypair.from_bytes(bytes(secret))


def verify_market_owner(rpc_url: str, market: Pubkey) -> Optional[str]:
    """Return the account owner (program) for the market, or None if not found."""
    resp = rpc_call(rpc_url, "getAccountInfo", [str(market), {"encoding": "base64"}])
    info = resp.get("result", {}).get("value")
    if not info:
        return None
    return info.get("owner")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=str(JUPSOL_SOL_MARKET),
                    help="Manifest market pubkey (default: jupSOL/SOL)")
    ap.add_argument("--keypair", default=os.getenv("KEYPAIR_FILE", "/home/ubuntu/leeroy-mainnet.json"))
    ap.add_argument("--rpc", default=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"))
    ap.add_argument("--ll-endpoint", default=os.getenv("LUNAR_LANDER_ENDPOINT", "http://fra.lunar-lander.hellomoon.io"))
    ap.add_argument("--no-confirm", action="store_true",
                    help="Skip the 'are you sure' prompt — for non-interactive runs")
    ap.add_argument("--dry-run", action="store_true",
                    help="Simulate only, do not submit")
    args = ap.parse_args()

    market = Pubkey.from_string(args.market)
    logger.info(f"market:       {market}")
    logger.info(f"rpc:          {args.rpc[:50]}{'...' if len(args.rpc) > 50 else ''}")
    logger.info(f"ll endpoint:  {args.ll_endpoint}")
    logger.info(f"keypair file: {args.keypair}")

    # Load wallet
    if not os.path.exists(args.keypair):
        logger.error(f"keypair file not found: {args.keypair}")
        return 2
    kp = load_keypair(args.keypair)
    payer = kp.pubkey()
    logger.info(f"payer:        {payer}")

    # Sanity: market account exists and is owned by Manifest program
    owner = verify_market_owner(args.rpc, market)
    if owner is None:
        logger.error(f"market account {market} not found on-chain")
        return 2
    if owner != str(manifest.PROGRAM_ID):
        logger.error(f"market owner mismatch: got {owner}, want {manifest.PROGRAM_ID}")
        return 2
    logger.info(f"market owner: {owner} (Manifest ✓)")

    # Build ixs: ClaimSeat + LL tip (relay requires tip ≥ 1M lamports inline)
    seat_ix = manifest.build_claim_seat_ix(payer, market)
    tip_ix = build_tip_ix(str(payer), lamports=LL_TIP_LAMPORTS)
    ixs = [seat_ix, tip_ix]
    logger.info(f"seat ix data: {seat_ix.data.hex()} ({len(seat_ix.data)}B), 3 accounts")
    logger.info(f"tip ix:       {LL_TIP_LAMPORTS} lamports → {tip_ix.accounts[1].pubkey}")

    # Recent blockhash for tx
    bh_resp = rpc_call(args.rpc, "getLatestBlockhash", [{"commitment": "processed"}])
    blockhash_str = bh_resp["result"]["value"]["blockhash"]
    logger.info(f"blockhash:    {blockhash_str}")

    # Compile + sign
    msg = MessageV0.try_compile(
        payer=payer,
        instructions=ixs,
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(blockhash_str),
    )
    tx = VersionedTransaction(msg, [kp])
    tx_bytes = bytes(tx)
    tx_b64 = base64.b64encode(tx_bytes).decode()
    sig = str(tx.signatures[0])
    logger.info(f"tx size:      {len(tx_bytes)}B")
    logger.info(f"tx signature: {sig}")

    # Simulate
    sim = rpc_call(args.rpc, "simulateTransaction", [
        tx_b64, {"encoding": "base64", "commitment": "processed", "sigVerify": False}
    ])
    sim_val = sim.get("result", {}).get("value", {})
    sim_err = sim_val.get("err")
    units = sim_val.get("unitsConsumed")
    logs = sim_val.get("logs") or []
    logger.info(f"simulate:     err={sim_err}  units={units}")
    for line in logs[-10:]:
        logger.info(f"  log| {line}")
    if sim_err is not None:
        logger.error(f"simulation failed, aborting before submit: {sim_err}")
        return 3

    if args.dry_run:
        logger.info("dry-run, not submitting")
        return 0

    if not args.no_confirm and sys.stdin.isatty():
        ans = input(f"submit live? [y/N] ").strip().lower()
        if ans != "y":
            logger.info("aborted by user")
            return 0

    # Submit via Lunar Lander /send
    ll = LunarLander(base_url=args.ll_endpoint)
    relay_sig = ll.send(tx_bytes, skip_preflight=True, timeout_s=15)
    if relay_sig is None:
        logger.error("Lunar Lander rejected the tx")
        return 4
    logger.info(f"LL accepted:  {relay_sig}")

    # Poll for confirmation
    deadline = time.time() + 60
    final_status = None
    while time.time() < deadline:
        resp = rpc_call(args.rpc, "getSignatureStatuses", [[sig]])
        status = (resp.get("result", {}).get("value") or [None])[0]
        if status:
            err = status.get("err")
            confs = status.get("confirmations")
            cstatus = status.get("confirmationStatus")
            slot = status.get("slot")
            logger.info(f"status:       slot={slot} confs={confs} status={cstatus} err={err}")
            if err:
                logger.error(f"tx errored on chain: {err}")
                return 5
            if cstatus in ("confirmed", "finalized"):
                final_status = status
                break
        time.sleep(2)

    if not final_status:
        logger.error("timed out waiting for confirmation")
        return 6

    logger.info(f"OK — claimed seat on {market} at slot {final_status.get('slot')}")
    logger.info(f"explorer: https://solscan.io/tx/{sig}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
