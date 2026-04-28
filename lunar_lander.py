"""Lunar Lander tx submission.

Submits signed Solana txs via Hellomoon's relay at
http://fra.lunar-lander.hellomoon.io/. Default endpoint is /send (single tx,
JSON-RPC sendTransaction). /sendBatch is exposed for bulk requote sweeps.

Auth: ?api-key=<LUNAR_LANDER_API_KEY> query param.

Tips: built as separate system_program::transfer ix to a random moon* account.
The caller composes the tip ix into their tx (or a sibling tx for /sendBatch).
For makerator the tip is OPTIONAL and policy-driven — most maker ops should
go without a tip; reserve tips for must-land-now cancels.

Reference: ../statalyzer/executor.py:914 (sendbundle), :955 (sendBatch).
The /send endpoint is INFERRED from the family — verify on first live use.
"""
import base64
import json
import logging
import os
import random
import struct
import urllib.error
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

# Tip accounts copied from ../statalyzer/constants.py:137. Hellomoon rotates
# load across these — pick one uniformly at random per tx.
TIP_ACCOUNTS = [
    "moon17L6BgxXRX5uHKudAmqVF96xia9h8ygcmG2sL3F",
    "moon26Sek222Md7ZydcAGxoKG832DK36CkLrS3PQY4c",
    "moon7fwyajcVstMoBnVy7UBcTx87SBtNoGGAaH2Cb8V",
    "moonBtH9HvLHjLqi9ivyrMVKgFUsSfrz9BwQ9khhn1u",
    "moonCJg8476LNFLptX1qrK8PdRsA1HD1R6XWyu9MB93",
    "moonF2sz7qwAtdETnrgxNbjonnhGGjd6r4W4UC9284s",
    "moonKfftMiGSak3cezvhEqvkPSzwrmQxQHXuspC96yj",
    "moonQBUKBpkifLcTd78bfxxt4PYLwmJ5admLW6cBBs8",
    "moonXwpKwoVkMegt5Bc776cSW793X1irL5hHV1vJ3JA",
    "moonZ6u9E2fgk6eWd82621eLPHt9zuJuYECXAYjMY1C",
]


class LunarLander:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or os.getenv(
            'LUNAR_LANDER_ENDPOINT', 'http://fra.lunar-lander.hellomoon.io'
        )).rstrip('/')
        # The .env distributed with statalyzer uses LUNAR_LANDER_UUID; the
        # statalyzer code expects LUNAR_LANDER_API_KEY. They're the same value
        # (Hellomoon's hml-... UUID is the API key). Accept either.
        self.api_key = (
            api_key
            or os.getenv('LUNAR_LANDER_API_KEY')
            or os.getenv('LUNAR_LANDER_UUID')
            or ''
        )

    def _url(self, path: str) -> str:
        u = f"{self.base_url}{path}"
        if self.api_key:
            u += f"?api-key={self.api_key}"
        return u

    def send(self, tx_bytes: bytes, skip_preflight: bool = True, timeout_s: float = 10) -> Optional[str]:
        """POST a single signed tx to /send. Returns signature string on success, None on failure.

        The relay returns after accepting the tx — does NOT wait for chain landing.
        Caller must poll getSignatureStatuses to confirm."""
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(tx_bytes).decode(),
                {"encoding": "base64", "skipPreflight": skip_preflight},
            ],
        }).encode()
        try:
            req = urllib.request.Request(
                self._url('/send'),
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = ''
            try:
                err_body = e.read().decode()[:500]
            except Exception:
                pass
            logger.error(f"LunarLander /send HTTP {e.code}: {err_body}")
            return None
        except Exception as e:
            logger.error(f"LunarLander /send failed: {e}")
            return None

        if body.get("error"):
            logger.error(f"LunarLander /send rpc error: {body['error']}")
            return None
        sig = body.get("result")
        if sig:
            logger.info(f"LunarLander /send accepted: {sig}")
        return sig

    def send_batch(self, txs: List[bytes], timeout_s: float = 15) -> dict:
        """POST a batch of independent signed txs to /sendBatch.

        Wire format: [u16 BE length][tx_bytes] per tx, content-type octet-stream.
        Returns {attempted, accepted, rejected, ...}. HTTP 400 is normal when
        some txs are rejected — the body still contains the same JSON shape."""
        body = b"".join(struct.pack(">H", len(t)) + t for t in txs)
        try:
            req = urllib.request.Request(
                self._url('/sendBatch'),
                data=body,
                headers={"Content-Type": "application/octet-stream"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = ''
            try:
                err_body = e.read().decode()
            except Exception:
                pass
            if e.code == 400 and err_body:
                try:
                    return json.loads(err_body)
                except json.JSONDecodeError:
                    pass
            logger.error(f"LunarLander /sendBatch HTTP {e.code}: {err_body[:500]}")
            return {"attempted": 0, "accepted": 0, "rejected": len(txs),
                    "parse_error": err_body[:200]}
        except Exception as e:
            logger.error(f"LunarLander /sendBatch failed: {e}")
            return {"attempted": 0, "accepted": 0, "rejected": len(txs)}


def random_tip_account() -> str:
    """Pick a moon* tip account uniformly at random."""
    return random.choice(TIP_ACCOUNTS)


def build_tip_ix(payer_pubkey: str, lamports: int = 1_000_000):
    """Build a system_program::transfer ix from payer to a random moon* account.

    Default 1_000_000 lamports = 0.001 SOL. Caller composes this into their tx
    when they want LL priority landing. Returns a solders Instruction."""
    from solders.pubkey import Pubkey
    from solders.system_program import transfer, TransferParams
    return transfer(TransferParams(
        from_pubkey=Pubkey.from_string(payer_pubkey),
        to_pubkey=Pubkey.from_string(random_tip_account()),
        lamports=lamports,
    ))
