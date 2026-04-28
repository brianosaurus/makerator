"""gRPC streaming feed for our wallet's transactions.

Subscribes to all transactions where our wallet pubkey appears in the
account list (signer or writable). Each event triggers a re-eval in the
main loop, so fills are visible within the slot they confirm rather than
on the next polled tick.

Pairs with manifest_stream.ManifestStreamFeed: book updates drive quoting,
tx updates drive instant fill recognition.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import base58
import grpc

import geyser_pb2
import geyser_pb2_grpc

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
class WalletTxEvent:
    signature: str           # base58
    slot: int
    err: bool                # True if tx failed
    log_messages: list       # raw log strings (for parsing Manifest "Program data:" events)


class WalletTxStream:
    def __init__(self, wallet_pubkey: str,
                 endpoint: Optional[str] = None, token: Optional[str] = None):
        self.wallet = wallet_pubkey
        self.endpoint = (endpoint or os.getenv('GRPC_ENDPOINT', '')).strip()
        self.token = (token or os.getenv('GRPC_TOKEN', '')).strip()

    def _create_channel(self):
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
        tx_filter = request.transactions["wallet_txs"]
        tx_filter.account_include.append(self.wallet)
        # vote/failed left unset → default = both included; we'll filter below.
        return request

    async def stream(self) -> AsyncGenerator[WalletTxEvent, None]:
        if not self.endpoint:
            logger.error("WalletTxStream: GRPC_ENDPOINT not set")
            return
        retry = 0
        while True:
            channel = self._create_channel()
            try:
                stub = geyser_pb2_grpc.GeyserStub(channel)
                metadata = self._metadata()
                logger.info(f"WalletTxStream subscribing to txs touching {self.wallet[:8]}..")
                retry = 0

                request = self._build_request()
                stream = stub.Subscribe(iter([request]), metadata=metadata)
                async for upd in stream:
                    if not upd.HasField('transaction'):
                        continue
                    tx = upd.transaction
                    sig_bytes = tx.transaction.signature
                    sig = base58.b58encode(sig_bytes).decode()
                    err = tx.transaction.meta.err.err if tx.transaction.meta.HasField('err') else b''
                    logs = list(tx.transaction.meta.log_messages or [])
                    yield WalletTxEvent(
                        signature=sig,
                        slot=tx.slot,
                        err=bool(err),
                        log_messages=logs,
                    )
            except grpc.aio.AioRpcError as e:
                retry += 1
                wait = min(retry * 2, 30)
                logger.error(f"WalletTxStream gRPC error: {e.code()} {e.details()}, retry in {wait}s")
                await asyncio.sleep(wait)
            except Exception as e:
                retry += 1
                logger.exception(f"WalletTxStream error: {e}")
                await asyncio.sleep(5)
            finally:
                try:
                    await channel.close()
                except Exception:
                    pass
