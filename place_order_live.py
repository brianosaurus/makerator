"""On-chain PlaceOrder test for makerator.

Places a single PostOnly LIMIT order via Manifest BatchUpdate (disc 6).
Defaults: SELL 0.01 jupSOL @ 1.5 SOL — far above the ~1.183 touch, so it
won't fill. After landing, the order should be visible in the asks tree.

Pre-reqs: ClaimSeat done; jupSOL deposited into the seat (run deposit_live.py).
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

JUPSOL_SOL_MARKET = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("place")


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 15) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_account(rpc_url: str, pubkey: Pubkey) -> Optional[bytes]:
    resp = rpc_call(rpc_url, "getAccountInfo", [str(pubkey), {"encoding": "base64"}])
    info = resp.get("result", {}).get("value")
    if not info:
        return None
    return base64.b64decode(info["data"][0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=str(JUPSOL_SOL_MARKET))
    ap.add_argument("--side", choices=("bid", "ask"), default="ask",
                    help="ask = sell base; bid = buy base")
    ap.add_argument("--price", type=float, default=1.5,
                    help="Human price (quote per base). 1.5 is far above jupSOL/SOL touch (~1.18)")
    ap.add_argument("--size", type=float, default=0.01, help="Size in base units (default 0.01)")
    ap.add_argument("--keypair", default=os.getenv("KEYPAIR_FILE", "/home/ubuntu/leeroy-mainnet.json"))
    ap.add_argument("--rpc", default=os.getenv("SOLANA_RPC_URL"))
    ap.add_argument("--swqos", default=os.getenv("SWQOS_ENDPOINT"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-confirm", action="store_true")
    args = ap.parse_args()

    market = Pubkey.from_string(args.market)
    logger.info(f"market: {market}")
    logger.info(f"side:   {args.side}  price={args.price}  size={args.size}")
    logger.info(f"submit: SwQOS")

    # Read market header for decimals
    market_data = fetch_account(args.rpc, market)
    if not market_data:
        logger.error("market not found")
        return 2
    h = manifest.parse_market_header(market_data)
    logger.info(f"base/quote decimals: {h.base_decimals}/{h.quote_decimals}")

    # Encode price + size
    mantissa, exponent = manifest.encode_price(args.price, h.base_decimals, h.quote_decimals)
    base_atoms = int(args.size * (10 ** h.base_decimals))
    logger.info(f"price encoded: mantissa={mantissa} exponent={exponent}  "
                f"(stored inner = {mantissa * 10**(18+exponent):.4e})")
    logger.info(f"base_atoms:    {base_atoms}")

    # Load wallet
    with open(args.keypair) as f:
        kp = Keypair.from_bytes(bytes(json.load(f)))
    payer = kp.pubkey()
    logger.info(f"payer:  {payer}")

    # Build BatchUpdate with one PostOnly order
    order = manifest.PlaceOrderParams(
        base_atoms=base_atoms,
        price_mantissa=mantissa,
        price_exponent=exponent,
        is_bid=(args.side == "bid"),
        last_valid_slot=manifest.NO_EXPIRATION,
        order_type=manifest.OrderType.POST_ONLY,
    )
    ix = manifest.build_batch_update_ix(payer, market, orders=[order])
    logger.info(f"ix data: {ix.data.hex()} ({len(ix.data)}B)")
    logger.info(f"ix accts: {len(ix.accounts)} [payer/sw, market/w, system/r]")

    # Get blockhash, compile, sign
    bh = rpc_call(args.rpc, "getLatestBlockhash", [{"commitment": "processed"}])
    blockhash_str = bh["result"]["value"]["blockhash"]
    msg = MessageV0.try_compile(
        payer=payer, instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(blockhash_str),
    )
    tx = VersionedTransaction(msg, [kp])
    tx_bytes = bytes(tx)
    tx_b64 = base64.b64encode(tx_bytes).decode()
    sig = str(tx.signatures[0])
    logger.info(f"tx size: {len(tx_bytes)}B  sig={sig}")

    # Simulate
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
                logger.info(f"OK — placed {args.side} {args.size} @ {args.price}")
                logger.info(f"explorer: https://solscan.io/tx/{sig}")
                return 0
        time.sleep(2)

    logger.error("timeout waiting for confirmation")
    return 6


if __name__ == "__main__":
    sys.exit(main())
