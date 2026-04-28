"""gRPC streaming feed for Manifest market accounts.

Subscribes to the 5 LST/SOL Manifest market accounts via Yellowstone Geyser.
Each account update yields:
  - Updated TopOfBook (best_bid, best_ask) per market — in SOL-per-LST units
  - The full account bytes (so callers can walk the full RB tree if needed)

Replaces polling getMultipleAccounts every 6s with sub-slot push updates
(every Manifest market state change → ~400ms or faster).

We do NOT yield USD prices. The cointegration signal generator only cares
about cross-LST log ratios; USD anchor cancels in the spread computation.
For 2-token baskets with hedge_ratios = [+1, -h] (h ≈ 1 for LSTs), spread
in SOL units = spread in USD units modulo a constant offset that doesn't
affect z-score.

Modeled on statalyzer/grpc_price_feed.py:WhirlpoolPriceFeed.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import AsyncGenerator, Dict, Optional, Tuple

import base58
import grpc

import geyser_pb2
import geyser_pb2_grpc
import manifest
import quoter

logger = logging.getLogger(__name__)


CHANNEL_OPTIONS = [
    ('grpc.keepalive_time_ms', 10000),
    ('grpc.keepalive_timeout_ms', 3000),
    ('grpc.keepalive_permit_without_calls', True),
    ('grpc.http2.max_pings_without_data', 0),
    ('grpc.http2.min_time_between_pings_ms', 10000),
    ('grpc.http2.min_ping_interval_without_data_ms', 300000),
    ('grpc.max_receive_message_length', 64 * 1024 * 1024),
    ('grpc.max_send_message_length', 64 * 1024 * 1024),
]


@dataclass
class MarketUpdate:
    """One Manifest market account update from the gRPC stream."""
    market_pubkey: str          # base58 string
    base_mint: str              # the LST mint
    slot: int
    raw_data: bytes             # full account bytes; caller may walk RB trees
    best_bid: Optional[float]   # SOL per LST (i.e. quote per base)
    best_ask: Optional[float]
    best_bid_size: float        # human LST units
    best_ask_size: float
    base_decimals: int
    quote_decimals: int


class ManifestStreamFeed:
    """Subscribe to the 5 LST/SOL Manifest markets, yield MarketUpdate per change."""

    def __init__(self, endpoint: Optional[str] = None, token: Optional[str] = None):
        self.endpoint = (endpoint or os.getenv('GRPC_ENDPOINT', '')).strip()
        self.token = (token or os.getenv('GRPC_TOKEN', '')).strip()
        # market_pubkey_str → base_mint_str (since we know which LST is the base)
        self._market_to_base: Dict[str, str] = {
            str(market): mint
            for mint, market in quoter.MANIFEST_LST_SOL_MARKETS.items()
        }

    def _create_channel(self):
        # GRPC_ENDPOINT format: host:port (no scheme). Hellomoon's parallel-titan
        # is on :889 plaintext (not TLS — TLS is reserved for :443). Same
        # heuristic statalyzer uses in grpc_price_feed.py.
        if 'localhost' in self.endpoint or ':443' not in self.endpoint:
            return grpc.aio.insecure_channel(self.endpoint, options=CHANNEL_OPTIONS)
        return grpc.aio.secure_channel(
            self.endpoint, grpc.ssl_channel_credentials(), options=CHANNEL_OPTIONS,
        )

    def _metadata(self):
        return [('x-token', self.token)] if self.token else []

    def _build_request(self) -> geyser_pb2.SubscribeRequest:
        request = geyser_pb2.SubscribeRequest(
            commitment=geyser_pb2.CommitmentLevel.PROCESSED,
        )
        accounts_filter = request.accounts["manifest_markets"]
        accounts_filter.account.extend(self._market_to_base.keys())
        return request

    def _decode(self, pubkey_bytes: bytes, data: bytes, slot: int) -> Optional[MarketUpdate]:
        market_str = base58.b58encode(pubkey_bytes).decode()
        base_mint = self._market_to_base.get(market_str)
        if base_mint is None:
            return None  # account update for something we didn't subscribe to (shouldn't happen)
        try:
            h = manifest.parse_market_header(data)
        except Exception as e:
            logger.warning(f"failed to parse {market_str[:8]}.. : {e}")
            return None
        bb, ba = manifest.top_of_book(data)
        return MarketUpdate(
            market_pubkey=market_str,
            base_mint=base_mint,
            slot=slot,
            raw_data=data,
            best_bid=bb.price_human(h.base_decimals, h.quote_decimals) if bb else None,
            best_ask=ba.price_human(h.base_decimals, h.quote_decimals) if ba else None,
            best_bid_size=(bb.num_base_atoms / (10 ** h.base_decimals)) if bb else 0.0,
            best_ask_size=(ba.num_base_atoms / (10 ** h.base_decimals)) if ba else 0.0,
            base_decimals=h.base_decimals,
            quote_decimals=h.quote_decimals,
        )

    async def stream(self) -> AsyncGenerator[MarketUpdate, None]:
        """Yield a MarketUpdate for every account state change on any of the 5 markets.

        Reconnects with exponential backoff on gRPC errors.
        """
        if not self.endpoint:
            logger.error("ManifestStreamFeed: GRPC_ENDPOINT not set")
            return
        retry = 0
        while True:
            channel = self._create_channel()
            try:
                stub = geyser_pb2_grpc.GeyserStub(channel)
                metadata = self._metadata()
                version = await stub.GetVersion(
                    geyser_pb2.GetVersionRequest(), metadata=metadata,
                )
                logger.info(
                    f"ManifestStreamFeed connected (Geyser v{version.version}); "
                    f"subscribing to {len(self._market_to_base)} markets"
                )
                retry = 0

                request = self._build_request()
                stream = stub.Subscribe(iter([request]), metadata=metadata)
                async for update in stream:
                    if not update.HasField('account'):
                        continue
                    info = update.account.account
                    out = self._decode(info.pubkey, info.data, update.account.slot)
                    if out is not None:
                        yield out
            except grpc.aio.AioRpcError as e:
                retry += 1
                wait = min(retry * 2, 30)
                logger.error(
                    f"ManifestStreamFeed gRPC error: {e.code()} {e.details()}, "
                    f"retry in {wait}s ({retry})"
                )
                await asyncio.sleep(wait)
            except Exception as e:
                retry += 1
                logger.exception(f"ManifestStreamFeed error, retry in 5s ({retry}): {e}")
                await asyncio.sleep(5)
            finally:
                try:
                    await channel.close()
                except Exception:
                    pass
