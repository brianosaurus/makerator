"""Read a Manifest market account from chain and dump its order book.

Validates manifest.parse_market_header + walk_orders_inorder + top_of_book
against the real on-chain layout. No writes.

Usage: python3 read_book_live.py [--market <pubkey>] [--depth N]
"""
import argparse
import base64
import json
import logging
import os
import sys
import urllib.request

from dotenv import load_dotenv
load_dotenv()

from solders.pubkey import Pubkey

import manifest

JUPSOL_SOL_MARKET = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("book")


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 15) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_account(rpc_url: str, pubkey: Pubkey) -> bytes:
    resp = rpc_call(rpc_url, "getAccountInfo", [str(pubkey), {"encoding": "base64"}])
    info = resp.get("result", {}).get("value")
    if not info:
        raise RuntimeError(f"account {pubkey} not found")
    data_b64 = info["data"][0]
    return base64.b64decode(data_b64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=str(JUPSOL_SOL_MARKET))
    ap.add_argument("--rpc", default=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"))
    ap.add_argument("--depth", type=int, default=10, help="max levels per side to print")
    args = ap.parse_args()

    market = Pubkey.from_string(args.market)
    logger.info(f"market: {market}")
    logger.info(f"rpc:    {args.rpc[:60]}{'...' if len(args.rpc) > 60 else ''}")

    data = fetch_account(args.rpc, market)
    logger.info(f"account size: {len(data)} bytes")

    h = manifest.parse_market_header(data)
    logger.info(f"base mint:   {h.base_mint} ({h.base_decimals} dec)")
    logger.info(f"quote mint:  {h.quote_mint} ({h.quote_decimals} dec)")
    logger.info(f"base vault:  {h.base_vault}")
    logger.info(f"quote vault: {h.quote_vault}")
    logger.info(f"seq number:  {h.order_sequence_number}")
    logger.info(f"bids root:   {h.bids_root:#010x}  best={h.bids_best:#010x}")
    logger.info(f"asks root:   {h.asks_root:#010x}  best={h.asks_best:#010x}")
    logger.info(f"quote vol:   {h.quote_volume}")

    best_bid, best_ask = manifest.top_of_book(data)
    if best_bid:
        bid_p = best_bid.price_human(h.base_decimals, h.quote_decimals)
        bid_size = best_bid.num_base_atoms / (10 ** h.base_decimals)
        logger.info(f"best bid:    {bid_p:.9f}  size={bid_size:.6f}  seq={best_bid.sequence_number}")
    else:
        logger.info("best bid:    (none — bids tree empty)")
    if best_ask:
        ask_p = best_ask.price_human(h.base_decimals, h.quote_decimals)
        ask_size = best_ask.num_base_atoms / (10 ** h.base_decimals)
        logger.info(f"best ask:    {ask_p:.9f}  size={ask_size:.6f}  seq={best_ask.sequence_number}")
    else:
        logger.info("best ask:    (none — asks tree empty)")
    if best_bid and best_ask:
        spread_bps = ((best_ask.price_human(h.base_decimals, h.quote_decimals) /
                       best_bid.price_human(h.base_decimals, h.quote_decimals)) - 1) * 10_000
        logger.info(f"spread:      {spread_bps:+.1f} bps")

    print()
    print(f"{'BIDS (base x quote)':<40s}    {'ASKS (base x quote)':<40s}")
    bids = list(manifest.walk_orders_inorder(data, h.bids_root))
    asks = list(manifest.walk_orders_inorder(data, h.asks_root))
    # Bids in-order ascends by price; reverse for descending (best at top)
    bids.sort(key=lambda o: o.price_inner, reverse=True)
    asks.sort(key=lambda o: o.price_inner)
    bids = bids[:args.depth]
    asks = asks[:args.depth]
    rows = max(len(bids), len(asks))
    for i in range(rows):
        bcell = ""
        acell = ""
        if i < len(bids):
            o = bids[i]
            p = o.price_human(h.base_decimals, h.quote_decimals)
            sz = o.num_base_atoms / (10 ** h.base_decimals)
            bcell = f"{p:.9f}  x  {sz:.6f}"
        if i < len(asks):
            o = asks[i]
            p = o.price_human(h.base_decimals, h.quote_decimals)
            sz = o.num_base_atoms / (10 ** h.base_decimals)
            acell = f"{p:.9f}  x  {sz:.6f}"
        print(f"{bcell:<40s}    {acell:<40s}")
    print()
    logger.info(f"total: {len(list(manifest.walk_orders_inorder(data, h.bids_root)))} bids, "
                f"{len(list(manifest.walk_orders_inorder(data, h.asks_root)))} asks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
