"""Phase-1 smoke test.

Wires up Config + JupiterPriceFeed + SignalGenerator and runs a few price
poll cycles. Verifies the lifted statalyzer modules load and execute end-to-end.

- If run locally (empty scanner DB), reports price polling + 0 baskets.
- If run on frankfurt (populated scanner DB), reports baskets + any z-score
  signals emitted from real LST prices.

Usage: python3 phase1_smoke.py [--ticks N] [--scanner-db PATH]
"""
import argparse
import asyncio
import logging
import time

from config import Config
from constants import (
    SOL_MINT, BSOL_MINT, MSOL_MINT, JITOSOL_MINT, JUPSOL_MINT,
    INF_MINT, VSOL_MINT, DSOL_MINT, EDGESOL_MINT, BONKSOL_MINT,
)
from price_feed import JupiterPriceFeed
from signals import SignalGenerator

LST_MINTS = {
    SOL_MINT, BSOL_MINT, MSOL_MINT, JITOSOL_MINT, JUPSOL_MINT,
    INF_MINT, VSOL_MINT, DSOL_MINT, EDGESOL_MINT, BONKSOL_MINT,
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ticks', type=int, default=3)
    parser.add_argument('--scanner-db', default='../arbitrage_tracker/arb_tracker.db')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    config = Config()
    config.price_poll_interval = 6.0  # tick every 6s

    feed = JupiterPriceFeed(config, LST_MINTS)
    sigs = SignalGenerator(config, scanner_db_path=args.scanner_db, db=None)

    n_baskets = sigs.load_baskets()
    print(f"loaded {n_baskets} cointegrated baskets from {args.scanner_db}")
    print(f"monitored mints: {len(sigs.monitored_mints)}")

    if sigs.monitored_mints:
        feed.update_mints(sigs.monitored_mints)

    tick = 0
    async for prices in feed.poll():
        tick += 1
        sigs.token_prices.update(prices)
        sigs.sol_usd_price = feed.sol_usd_price

        emitted = sigs.process_prices(prices, time.time())
        print(
            f"tick {tick}: prices={len(prices)}/{len(feed.mints)} "
            f"SOL=${feed.sol_usd_price:.2f} signals={len(emitted)}"
        )
        for s in emitted:
            print(
                f"  {s.signal_type.value:12s} {'/'.join(s.symbols):20s} "
                f"z={s.zscore:+.2f} spread={s.spread:+.5f}"
            )
        if tick >= args.ticks:
            break

    print(f"smoke test ok: {tick} ticks completed")


if __name__ == "__main__":
    asyncio.run(main())
