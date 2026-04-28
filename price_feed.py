"""
Jupiter Price API feed for statalyzer.
Polls Jupiter's price oracle at regular intervals to get token prices,
replacing the heavyweight gRPC block streaming + swap detection pipeline.
"""

import asyncio
import json
import logging
import os
import urllib.request
from typing import AsyncGenerator, Dict, Set

from constants import SOL_MINT

logger = logging.getLogger(__name__)

PRICE_API_URL = "https://api.jup.ag/price/v3"


class JupiterPriceFeed:
    """Polls Jupiter Price API for token prices at regular intervals."""

    def __init__(self, config, mints: Set[str]):
        self.config = config
        self.mints: Set[str] = set(mints)
        self.mints.add(SOL_MINT)  # always need SOL/USD
        self.sol_usd_price: float = 0.0
        self._api_key = os.getenv('JUPITER_API_KEY', '')
        self._last_prices: Dict[str, float] = {}  # cached fallback for API outages

    def update_mints(self, new_mints: Set[str]):
        """Add mints to the poll set (e.g. when discovery finds new pairs)."""
        self.mints.update(new_mints)
        self.mints.add(SOL_MINT)

    async def poll(self) -> AsyncGenerator[Dict[str, float], None]:
        """Yield {mint: usd_price} dicts every poll_interval seconds."""
        while True:
            prices = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_prices
            )
            if prices:
                self._last_prices = prices
                yield prices
            else:
                logger.warning("Jupiter price poll failed, using cached prices")
                yield self._last_prices
            await asyncio.sleep(self.config.price_poll_interval)

    def _fetch_prices(self) -> Dict[str, float]:
        """Batch fetch USD prices from Jupiter Price API."""
        if not self.mints:
            return {}

        mint_list = list(self.mints)
        prices = {}

        # Jupiter API may have URL length limits; batch in chunks of 100
        chunk_size = 100
        for i in range(0, len(mint_list), chunk_size):
            chunk = mint_list[i:i + chunk_size]
            ids = ",".join(chunk)
            url = f"{PRICE_API_URL}?ids={ids}"
            try:
                headers = {
                    "Accept": "application/json",
                    "User-Agent": "statalyzer/1.0",
                }
                if self._api_key:
                    headers["x-api-key"] = self._api_key
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                for mint in chunk:
                    entry = data.get(mint)
                    if entry and entry.get("usdPrice"):
                        prices[mint] = float(entry["usdPrice"])
            except Exception as e:
                logger.warning(f"Jupiter price API failed for chunk {i}: {e}")

        # Update SOL/USD cache
        sol_price = prices.get(SOL_MINT)
        if sol_price and sol_price > 0:
            self.sol_usd_price = sol_price

        if prices:
            logger.info(f"Jupiter poll: {len(prices)}/{len(self.mints)} prices"
                        f" | SOL/USD=${self.sol_usd_price:.2f}")
        return prices
