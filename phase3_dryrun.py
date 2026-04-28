"""Phase 3 dry-run: signal-driven quoting, no on-chain submission.

Demonstrates the full read path:
  Jupiter prices → SignalGenerator → load 5 Manifest books → quoter.decide_quotes
                 → pretty-print intents (would-place-order)

Run on frankfurt for live signals; safe to run repeatedly (no writes).

Usage: python3 phase3_dryrun.py [--ticks N] [--entry-z 1.5]
"""
import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
import urllib.request
from typing import Dict

from dotenv import load_dotenv
load_dotenv()

from solders.pubkey import Pubkey

from config import Config
from price_feed import JupiterPriceFeed
from signals import SignalGenerator
import manifest
import quoter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dryrun")


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 15) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_books(rpc_url: str, markets: Dict[str, Pubkey]) -> Dict[str, quoter.TopOfBook]:
    """Fetch all 5 markets in one getMultipleAccounts call, parse top of book."""
    addrs = [str(m) for m in markets.values()]
    resp = rpc_call(rpc_url, "getMultipleAccounts", [addrs, {"encoding": "base64"}])
    values = resp.get("result", {}).get("value") or []
    out: Dict[str, quoter.TopOfBook] = {}
    for mint, market in markets.items():
        idx = addrs.index(str(market))
        info = values[idx] if idx < len(values) else None
        if not info:
            out[str(market)] = quoter.TopOfBook(best_bid=None, best_ask=None)
            continue
        data = base64.b64decode(info["data"][0])
        try:
            h = manifest.parse_market_header(data)
            bb, ba = manifest.top_of_book(data)
            tob = quoter.TopOfBook(
                best_bid=bb.price_human(h.base_decimals, h.quote_decimals) if bb else None,
                best_ask=ba.price_human(h.base_decimals, h.quote_decimals) if ba else None,
                best_bid_size=(bb.num_base_atoms / (10 ** h.base_decimals)) if bb else 0,
                best_ask_size=(ba.num_base_atoms / (10 ** h.base_decimals)) if ba else 0,
            )
        except Exception as e:
            logger.warning(f"failed to parse {market}: {e}")
            tob = quoter.TopOfBook(best_bid=None, best_ask=None)
        out[str(market)] = tob
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=5)
    ap.add_argument("--scanner-db", default=os.getenv("SCANNER_DB", "../arbitrage_tracker/arb_tracker.db"))
    ap.add_argument("--rpc", default=os.getenv("SOLANA_RPC_URL"))
    ap.add_argument("--entry-z", type=float, default=None,
                    help="Override config.entry_zscore for the dry-run")
    args = ap.parse_args()

    config = Config()
    if args.entry_z is not None:
        config.entry_zscore = args.entry_z
    config.price_poll_interval = 6.0
    if not hasattr(config, 'quote_size_sol'):
        config.quote_size_sol = 0.5

    logger.info(f"entry_zscore={config.entry_zscore}  "
                f"quote_size_sol={getattr(config, 'quote_size_sol', 0.5)}  "
                f"tick_interval={config.price_poll_interval}s")

    sigs = SignalGenerator(config, scanner_db_path=args.scanner_db, db=None)
    n_baskets = sigs.load_baskets()
    logger.info(f"loaded {n_baskets} baskets, monitoring {len(sigs.monitored_mints)} mints")

    feed = JupiterPriceFeed(config, set(quoter.MANIFEST_LST_SOL_MARKETS.keys()))
    if sigs.monitored_mints:
        feed.update_mints(sigs.monitored_mints)

    tick = 0
    async for prices in feed.poll():
        tick += 1
        sigs.token_prices.update(prices)
        sigs.sol_usd_price = feed.sol_usd_price

        emitted = sigs.process_prices(prices, time.time())
        # Filter to baskets where BOTH tokens trade on the 5 Manifest LST/SOL
        manifest_lsts = set(quoter.MANIFEST_LST_SOL_MARKETS.keys())
        relevant = [s for s in emitted
                    if s.basket_size == 2 and all(m in manifest_lsts for m in s.mints)]

        # Snapshot all 5 books in one RPC roundtrip
        books = fetch_books(args.rpc, quoter.MANIFEST_LST_SOL_MARKETS)

        # Print book snapshot (compact)
        book_summary = []
        for mint, market in quoter.MANIFEST_LST_SOL_MARKETS.items():
            tob = books.get(str(market))
            sym = quoter.LST_SYMBOLS.get(mint, mint[:6])
            if tob and tob.best_bid and tob.best_ask:
                spread_bps = ((tob.best_ask / tob.best_bid) - 1) * 10_000
                book_summary.append(f"{sym}: {tob.best_bid:.6f}/{tob.best_ask:.6f} ({spread_bps:.2f}bp)")
            else:
                book_summary.append(f"{sym}: empty")

        intents = quoter.decide_quotes(config, relevant, books)
        logger.info(
            f"tick {tick}: prices={len(prices)}/{len(feed.mints)} "
            f"signals_total={len(emitted)} signals_actionable={len(relevant)} "
            f"intents={len(intents)}"
        )
        for line in book_summary:
            logger.info(f"  book | {line}")
        if relevant:
            for s in relevant[:5]:
                logger.info(f"  sig  | {s.signal_type.value:12s} {'/'.join(s.symbols):20s} "
                            f"z={s.zscore:+.2f} hl={s.half_life_secs:.0f}s")
        for it in intents:
            logger.info(f"  WOULD| {it.side:>3s} {it.size_base:.4f} on {it.market_label:>14s} "
                        f"@ {it.price:.9f}  ({it.pair_label})")

        if tick >= args.ticks:
            break

    logger.info(f"dry-run ok: {tick} ticks, no submissions")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
