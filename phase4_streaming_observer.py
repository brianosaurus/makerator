"""Phase 4 gRPC stream observer.

Subscribes to the 5 LST/SOL Manifest market accounts and prints every
update for ~30 seconds. No decision logic, no submissions. Used to:
  - Validate the gRPC connection + auth
  - Measure actual update frequency per market
  - Compare against polling latency

Run on frankfurt: python3 phase4_streaming_observer.py [--seconds 30]
"""
import argparse
import asyncio
import logging
import sys
import time

from dotenv import load_dotenv
load_dotenv()

import manifest_stream
import quoter

logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("observer")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()

    feed = manifest_stream.ManifestStreamFeed()
    if not feed.endpoint:
        logger.error("GRPC_ENDPOINT not set in env")
        return 2
    logger.info(f"endpoint: {feed.endpoint}")
    logger.info(f"observing for {args.seconds}s")

    start = time.monotonic()
    counts: dict[str, int] = {}
    last_seen: dict[str, float] = {}
    first_seen: dict[str, float] = {}

    sym_for_mint = quoter.LST_SYMBOLS

    async def consume():
        async for upd in feed.stream():
            sym = sym_for_mint.get(upd.base_mint, upd.base_mint[:6])
            now = time.monotonic()
            counts[sym] = counts.get(sym, 0) + 1
            last_seen[sym] = now
            first_seen.setdefault(sym, now)

            spread_bps = None
            if upd.best_bid and upd.best_ask:
                spread_bps = (upd.best_ask / upd.best_bid - 1) * 10_000
            spread_str = f"{spread_bps:+.2f}bp" if spread_bps is not None else "—"
            logger.info(
                f"[{sym:>8s}] slot={upd.slot}  "
                f"bid={upd.best_bid or 0:.9f} ({upd.best_bid_size:.4f})  "
                f"ask={upd.best_ask or 0:.9f} ({upd.best_ask_size:.4f})  "
                f"spread={spread_str}"
            )

    try:
        await asyncio.wait_for(consume(), timeout=args.seconds)
    except asyncio.TimeoutError:
        pass  # expected — exits on the configured deadline regardless of event volume
    elapsed = time.monotonic() - start
    print(f"\n=== summary over {elapsed:.1f}s ===")
    total = sum(counts.values())
    print(f"total updates: {total}  rate={total/elapsed:.2f}/s")
    for sym, n in sorted(counts.items(), key=lambda x: -x[1]):
        first = first_seen[sym]
        last = last_seen[sym]
        rate = n / max(elapsed, 0.001)
        print(f"  {sym:>10s}: {n:4d} updates  rate={rate:.2f}/s  span={last-first:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
