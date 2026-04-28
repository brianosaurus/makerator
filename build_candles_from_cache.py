#!/usr/bin/env python3
"""
Build backtest-ready candle data from scanner's price_cache.

Reads raw token prices from the scanner DB, constructs log-price candles
for all cointegrated baskets, and writes them to a target DB's price_candles table.

Usage:
    python3 build_candles_from_cache.py \
        --scanner-db ../arbitrage_tracker/arb_tracker.db \
        --output-db backtest_data.db \
        --interval 30 \
        --token-whitelist SOL,bSOL,jitoSOL,mSOL,jupSOL,stSOL,JUP,FARTCOIN,ETH
"""

import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Dict, List, Set

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def load_price_cache(scanner_db: str) -> Dict[str, List[tuple]]:
    """Load all price snapshots grouped by token mint, sorted by time."""
    conn = sqlite3.connect(f"file:{scanner_db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT token_mint, timestamp, price FROM price_cache "
        "WHERE price > 0 ORDER BY timestamp"
    ).fetchall()
    conn.close()

    by_token = defaultdict(list)
    for mint, ts, price in rows:
        by_token[mint].append((ts, price))

    logger.info(f"Loaded {len(rows)} price snapshots for {len(by_token)} tokens")
    return dict(by_token)


def load_baskets(scanner_db: str, whitelist_mints: Set[str] = None, max_basket_size: int = None) -> List[dict]:
    """Load cointegrated baskets from scanner DB."""
    conn = sqlite3.connect(f"file:{scanner_db}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT basket_key, basket_size, mints_json, symbols_json,
               hedge_ratios_json, spread_mean, spread_std, half_life
        FROM cointegration_results
        WHERE eg_is_cointegrated = 1 OR johansen_is_cointegrated = 1
    """).fetchall()
    conn.close()

    baskets = []
    for row in rows:
        mints = json.loads(row[2])
        symbols = json.loads(row[3])
        hedge_ratios = json.loads(row[4])

        if max_basket_size and row[1] > max_basket_size:
            continue

        if whitelist_mints:
            if not all(m in whitelist_mints for m in mints):
                continue

        baskets.append({
            "basket_key": row[0],
            "basket_size": row[1],
            "mints": mints,
            "symbols": symbols,
            "hedge_ratios": hedge_ratios,
            "spread_mean": row[5],
            "spread_std": row[6],
            "half_life": row[7],
        })

    logger.info(f"Loaded {len(baskets)} baskets" +
                (f" ({len(rows) - len(baskets)} filtered by whitelist)" if whitelist_mints else ""))
    return baskets


def interpolate_price(snapshots: List[tuple], target_ts: float) -> float:
    """Get price at target_ts via nearest-neighbor lookup."""
    # Binary search for closest timestamp
    lo, hi = 0, len(snapshots) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if snapshots[mid][0] < target_ts:
            lo = mid + 1
        else:
            hi = mid

    # Check neighbors
    best = lo
    if best > 0:
        if abs(snapshots[best - 1][0] - target_ts) < abs(snapshots[best][0] - target_ts):
            best = best - 1

    ts, price = snapshots[best]
    # Only use if within 5 minutes of target
    if abs(ts - target_ts) > 300:
        return 0.0
    return price


def build_candles(price_cache: Dict[str, List[tuple]], baskets: List[dict],
                  interval_secs: int = 30) -> Dict[str, List[tuple]]:
    """Build candles for each basket at the specified interval."""
    # Find global time range
    all_times = []
    for snapshots in price_cache.values():
        if snapshots:
            all_times.append(snapshots[0][0])
            all_times.append(snapshots[-1][0])

    if not all_times:
        return {}

    start_ts = min(all_times)
    end_ts = max(all_times)
    logger.info(f"Price data spans {(end_ts - start_ts) / 3600:.1f} hours")

    # Generate candle timestamps
    candle_times = []
    t = start_ts
    while t <= end_ts:
        candle_times.append(t)
        t += interval_secs

    logger.info(f"Generating {len(candle_times)} candles at {interval_secs}s intervals")

    # Build candles per basket
    candles = {}
    skipped = 0

    for i, basket in enumerate(baskets):
        if (i + 1) % 100 == 0:
            logger.info(f"  Processing basket {i + 1}/{len(baskets)}...")

        mints = basket["mints"]

        # Check all mints have price data
        if not all(m in price_cache for m in mints):
            skipped += 1
            continue

        basket_candles = []
        for ts in candle_times:
            prices = []
            valid = True
            for mint in mints:
                p = interpolate_price(price_cache[mint], ts)
                if p <= 0:
                    valid = False
                    break
                prices.append(p)

            if valid:
                log_prices = [math.log(p) for p in prices]
                basket_candles.append((ts, log_prices))

        if basket_candles:
            candles[basket["basket_key"]] = basket_candles

    logger.info(f"Built candles for {len(candles)} baskets ({skipped} skipped, no price data)")
    return candles


def write_output_db(output_path: str, candles: Dict[str, List[tuple]], baskets: List[dict]):
    """Write candles and basket info to output DB."""
    conn = sqlite3.connect(output_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Create tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_key TEXT NOT NULL,
            timestamp REAL NOT NULL,
            log_prices_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_candles_pair_time ON price_candles(basket_key, timestamp);

        CREATE TABLE IF NOT EXISTS discovered_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_key TEXT NOT NULL UNIQUE,
            basket_size INTEGER NOT NULL DEFAULT 2,
            mints_json TEXT NOT NULL DEFAULT '[]',
            symbols_json TEXT NOT NULL DEFAULT '[]',
            hedge_ratios_json TEXT NOT NULL DEFAULT '[]',
            half_life REAL NOT NULL,
            eg_p_value REAL NOT NULL DEFAULT 0.01,
            eg_test_statistic REAL NOT NULL DEFAULT -4.0,
            spread_mean REAL NOT NULL,
            spread_std REAL NOT NULL,
            num_observations INTEGER NOT NULL DEFAULT 1000,
            analyzed_at REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Clear existing data
    conn.execute("DELETE FROM price_candles")
    conn.execute("DELETE FROM discovered_pairs")

    # Write candles
    total = 0
    for basket_key, basket_candles in candles.items():
        for ts, log_prices in basket_candles:
            conn.execute(
                "INSERT INTO price_candles (basket_key, timestamp, log_prices_json) VALUES (?, ?, ?)",
                (basket_key, ts, json.dumps(log_prices))
            )
            total += 1

        if total % 50000 == 0:
            conn.commit()
            logger.info(f"  Written {total} candles...")

    conn.commit()
    logger.info(f"Written {total} candles total")

    # Write basket info (as discovered_pairs for the backtester)
    now = time.time()
    basket_map = {b["basket_key"]: b for b in baskets}
    for basket_key in candles:
        b = basket_map.get(basket_key)
        if not b:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO discovered_pairs
            (pair_key, basket_size, mints_json, symbols_json, hedge_ratios_json,
             half_life, spread_mean, spread_std, num_observations, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            basket_key, b["basket_size"],
            json.dumps(b["mints"]), json.dumps(b["symbols"]),
            json.dumps(b["hedge_ratios"]),
            b["half_life"], b["spread_mean"], b["spread_std"],
            1000, now,
        ))

    conn.commit()
    conn.close()

    pairs_written = sum(1 for bk in candles if bk in basket_map)
    logger.info(f"Written {pairs_written} basket definitions")


def main():
    parser = argparse.ArgumentParser(description="Build candle data from scanner price cache")
    parser.add_argument("--scanner-db", default="../arbitrage_tracker/arb_tracker.db")
    parser.add_argument("--output-db", default="backtest_data.db")
    parser.add_argument("--interval", type=int, default=30, help="Candle interval in seconds")
    parser.add_argument("--token-whitelist", type=str, default=None,
                        help="Comma-separated token symbols to include")
    parser.add_argument("--max-basket-size", type=int, default=None,
                        help="Max basket size (e.g., 2 for pairs only)")
    args = parser.parse_args()

    # Resolve whitelist to mints
    whitelist_mints = None
    if args.token_whitelist:
        from constants import WELL_KNOWN_TOKENS
        sym_to_mint = {v["symbol"]: k for k, v in WELL_KNOWN_TOKENS.items()}
        symbols = [s.strip() for s in args.token_whitelist.split(",")]
        whitelist_mints = set()
        for sym in symbols:
            mint = sym_to_mint.get(sym)
            if mint:
                whitelist_mints.add(mint)
            else:
                logger.warning(f"Unknown symbol: {sym}")
        logger.info(f"Whitelist: {len(whitelist_mints)} mints from {len(symbols)} symbols")

    price_cache = load_price_cache(args.scanner_db)
    baskets = load_baskets(args.scanner_db, whitelist_mints, args.max_basket_size)

    if not baskets:
        logger.error("No baskets found")
        sys.exit(1)

    candles = build_candles(price_cache, baskets, args.interval)

    if not candles:
        logger.error("No candles generated")
        sys.exit(1)

    write_output_db(args.output_db, candles, baskets)
    logger.info(f"Done. Output: {args.output_db}")


if __name__ == "__main__":
    main()
