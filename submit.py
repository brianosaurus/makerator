"""Tx submission router for makerator.

Two paths, locked-in policy:
  - normal     → SwQOS (`$SWQOS_ENDPOINT/sendTransaction`), tipless
  - must_land  → LunarLander `/send`, requires inline 1M-lamport tip

The runner picks `urgency` based on the action: routine places + soft cancels
go via SwQOS; emergency cancels against detected toxic flow use LL.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

from lunar_lander import LunarLander

logger = logging.getLogger(__name__)


class Submitter:
    def __init__(self, swqos_url: Optional[str] = None,
                 lunar_lander: Optional[LunarLander] = None):
        self.swqos_url = (swqos_url or os.getenv('SWQOS_ENDPOINT', '')).rstrip('?')
        if not self.swqos_url:
            raise ValueError("SWQOS_ENDPOINT not set")
        self.lunar = lunar_lander or LunarLander()

    def submit(self, tx_bytes: bytes, urgency: str = "normal",
               timeout_s: float = 10) -> Optional[str]:
        """Submit a signed tx. Returns the signature string on accept, None on reject.

        urgency:
          "normal"     → SwQOS RPC sendTransaction, tipless. ~1-2 block landing.
          "must_land"  → LunarLander /send, requires inline tip (caller's job
                         to include a transfer ix to a moon* account).
        """
        if urgency == "must_land":
            return self.lunar.send(tx_bytes, skip_preflight=True, timeout_s=timeout_s)
        return self._send_swqos(tx_bytes, timeout_s)

    def _send_swqos(self, tx_bytes: bytes, timeout_s: float) -> Optional[str]:
        import base64
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [
                base64.b64encode(tx_bytes).decode(),
                {"encoding": "base64", "skipPreflight": True},
            ],
        }).encode()
        try:
            req = urllib.request.Request(
                self.swqos_url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = ""
            try:
                err = e.read().decode()[:300]
            except Exception:
                pass
            logger.error(f"SwQOS HTTP {e.code}: {err}")
            return None
        except Exception as e:
            logger.error(f"SwQOS submit failed: {e}")
            return None
        if body.get("error"):
            logger.error(f"SwQOS rpc error: {body['error']}")
            return None
        return body.get("result")
