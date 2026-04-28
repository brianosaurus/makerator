"""Makerator main loop.

Conducts each tick:
  prices → signals → fetch books → quoter → orders.reconcile
  → per-market BatchUpdate (cancels + places) → SwQOS submit
  → poll signature → parse program return data → register placed orders

Modes:
  --dry-run (default)  print what it would do, no chain writes
  --live               actually build, submit, register

Safety:
  - Default is dry-run; --live must be explicit.
  - On startup, prints a summary and waits for confirm unless --no-confirm.
  - Bootstrap from chain on startup (walks seats trees + filters resting
    orders by trader_index) so a crash/restart reconstructs open orders.
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
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
load_dotenv()

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config import Config
from price_feed import JupiterPriceFeed
from signals import SignalGenerator
import manifest
import quoter
import orders
from submit import Submitter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("makerator")


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 15) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_market_accounts(rpc_url: str, commitment: str = "confirmed") -> Dict[str, bytes]:
    """One getMultipleAccounts call → market_pubkey_str → account bytes.

    Default commitment is `confirmed` (not `finalized` which is the RPC
    default) so bootstrap and tick reads see freshly-landed orders within
    ~1-2 slots instead of waiting for finalization (~12-30 slots)."""
    addrs = [str(m) for m in quoter.MANIFEST_LST_SOL_MARKETS.values()]
    resp = rpc_call(rpc_url, "getMultipleAccounts", [
        addrs, {"encoding": "base64", "commitment": commitment}
    ])
    values = resp.get("result", {}).get("value") or []
    out: Dict[str, bytes] = {}
    for addr, info in zip(addrs, values):
        if info:
            out[addr] = base64.b64decode(info["data"][0])
    return out


def derive_book_state(market_data: Dict[str, bytes]) -> Tuple[
    Dict[str, quoter.TopOfBook],
    Dict[Tuple[str, str], Set[int]],
    Dict[str, manifest.MarketHeader],
]:
    """From raw account bytes per market → (top-of-book map, live-seqs map, headers)."""
    tobs: Dict[str, quoter.TopOfBook] = {}
    live_seqs: Dict[Tuple[str, str], Set[int]] = {}
    headers: Dict[str, manifest.MarketHeader] = {}
    for market_str, data in market_data.items():
        try:
            h = manifest.parse_market_header(data)
        except Exception as e:
            logger.warning(f"parse failed for {market_str}: {e}")
            continue
        headers[market_str] = h
        bb, ba = manifest.top_of_book(data)
        tobs[market_str] = quoter.TopOfBook(
            best_bid=bb.price_human(h.base_decimals, h.quote_decimals) if bb else None,
            best_ask=ba.price_human(h.base_decimals, h.quote_decimals) if ba else None,
            best_bid_size=(bb.num_base_atoms / (10 ** h.base_decimals)) if bb else 0,
            best_ask_size=(ba.num_base_atoms / (10 ** h.base_decimals)) if ba else 0,
        )
        live_seqs[(market_str, "bid")] = {
            o.sequence_number for o in manifest.walk_orders_inorder(data, h.bids_root)
        }
        live_seqs[(market_str, "ask")] = {
            o.sequence_number for o in manifest.walk_orders_inorder(data, h.asks_root)
        }
    return tobs, live_seqs, headers


def group_actions_by_market(
    cancels: List[orders.CancelAction],
    places: List[orders.PlaceAction],
) -> Dict[Pubkey, Tuple[List[orders.CancelAction], List[orders.PlaceAction]]]:
    by_market: Dict[Pubkey, Tuple[list, list]] = {}
    for c in cancels:
        by_market.setdefault(c.market, ([], []))[0].append(c)
    for p in places:
        by_market.setdefault(p.intent.market, ([], []))[1].append(p)
    return by_market


def build_batch_update_tx(
    payer_kp: Keypair,
    market: Pubkey,
    cancels: List[orders.CancelAction],
    places: List[orders.PlaceAction],
    headers: Dict[str, manifest.MarketHeader],
    blockhash: str,
) -> Tuple[bytes, str]:
    """Build + sign a single BatchUpdate tx with this market's cancels + places.
    Returns (tx_bytes, signature_string)."""
    h = headers[str(market)]
    cancel_params = [
        manifest.CancelOrderParams(
            order_sequence_number=c.sequence_number,
            order_index_hint=c.order_index_hint,
        ) for c in cancels
    ]
    order_params = []
    for pa in places:
        intent = pa.intent
        mantissa, exponent = manifest.encode_price(intent.price, h.base_decimals, h.quote_decimals)
        base_atoms = int(intent.size_base * (10 ** h.base_decimals))
        order_params.append(manifest.PlaceOrderParams(
            base_atoms=base_atoms,
            price_mantissa=mantissa,
            price_exponent=exponent,
            is_bid=(intent.side == "bid"),
            last_valid_slot=manifest.NO_EXPIRATION,
            order_type=manifest.OrderType.POST_ONLY,
        ))

    ix = manifest.build_batch_update_ix(
        payer_kp.pubkey(), market,
        cancels=cancel_params, orders=order_params,
    )
    msg = MessageV0.try_compile(
        payer=payer_kp.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(blockhash),
    )
    tx = VersionedTransaction(msg, [payer_kp])
    tx_bytes = bytes(tx)
    sig = str(tx.signatures[0])
    return tx_bytes, sig


def fetch_program_return(rpc_url: str, signature: str,
                         deadline_s: float = 30) -> Optional[bytes]:
    """Fetch the confirmed tx and extract the Manifest program's return data."""
    end = time.time() + deadline_s
    while time.time() < end:
        resp = rpc_call(rpc_url, "getTransaction", [
            signature, {"encoding": "base64", "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0}
        ])
        result = resp.get("result")
        if not result:
            time.sleep(1)
            continue
        meta = result.get("meta") or {}
        if meta.get("err"):
            logger.error(f"tx {signature[:16]}... errored on chain: {meta['err']}")
            return None
        rd = meta.get("returnData")
        if not rd:
            return b""  # no orders placed (e.g. cancel-only tx)
        program_id, data_b64 = rd.get("programId"), rd.get("data", [])
        if program_id != str(manifest.PROGRAM_ID):
            return b""
        if isinstance(data_b64, list):
            data_b64 = data_b64[0]
        return base64.b64decode(data_b64)
    return None


def confirm_signature(rpc_url: str, signature: str, deadline_s: float = 30) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        resp = rpc_call(rpc_url, "getSignatureStatuses", [[signature]])
        status = (resp.get("result", {}).get("value") or [None])[0]
        if status:
            if status.get("err"):
                return False
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return True
        time.sleep(1)
    return False


def bootstrap_open_orders(
    market_data: Dict[str, bytes],
    headers: Dict[str, manifest.MarketHeader],
    trader: Pubkey,
) -> List[orders.OpenOrder]:
    """Reconstruct our open orders by walking each market's seats + resting trees.

    For each market: find our seat by trader pubkey, get its DataIndex (= our
    trader_index), then walk bids+asks filtering by trader_index match. Build
    OpenOrder records. `placed_at` is set to `now` since we don't have the
    original placement time — TTL will start fresh from bootstrap.
    """
    now = time.time()
    found: List[orders.OpenOrder] = []
    label_for_market = {
        str(market): f"{quoter.LST_SYMBOLS.get(mint, mint[:6])}/SOL"
        for mint, market in quoter.MANIFEST_LST_SOL_MARKETS.items()
    }
    for market_str, data in market_data.items():
        h = headers.get(market_str)
        if h is None:
            continue
        seat = manifest.find_seat_for_trader(data, trader)
        if seat is None:
            continue
        our_idx = seat.data_index
        market_pk = Pubkey.from_string(market_str)
        for resting in manifest.walk_orders_inorder(data, h.bids_root):
            if resting.trader_index != our_idx:
                continue
            found.append(orders.OpenOrder(
                market=market_pk,
                market_label=label_for_market.get(market_str, market_str[:8]),
                side="bid",
                price=resting.price_human(h.base_decimals, h.quote_decimals),
                size_base=resting.num_base_atoms / (10 ** h.base_decimals),
                sequence_number=resting.sequence_number,
                order_index_hint=resting.data_index,
                placed_at=now,
                intent_pair="(bootstrapped)",
            ))
        for resting in manifest.walk_orders_inorder(data, h.asks_root):
            if resting.trader_index != our_idx:
                continue
            found.append(orders.OpenOrder(
                market=market_pk,
                market_label=label_for_market.get(market_str, market_str[:8]),
                side="ask",
                price=resting.price_human(h.base_decimals, h.quote_decimals),
                size_base=resting.num_base_atoms / (10 ** h.base_decimals),
                sequence_number=resting.sequence_number,
                order_index_hint=resting.data_index,
                placed_at=now,
                intent_pair="(bootstrapped)",
            ))
    return found


async def run(args):
    config = Config()
    if args.entry_z is not None:
        config.entry_zscore = args.entry_z
    config.price_poll_interval = float(args.tick_interval)
    if not hasattr(config, "quote_size_sol"):
        config.quote_size_sol = float(args.quote_size_sol)
    config.requote_threshold_bps = float(args.requote_bps)
    config.order_ttl_seconds = float(args.ttl)

    logger.info(f"mode={'LIVE' if args.live else 'DRY-RUN'}  "
                f"entry_z={config.entry_zscore}  ttl={config.order_ttl_seconds}s  "
                f"requote_thresh={config.requote_threshold_bps}bp")

    sigs = SignalGenerator(config, scanner_db_path=args.scanner_db, db=None)
    sigs.load_baskets()

    feed = JupiterPriceFeed(config, set(quoter.MANIFEST_LST_SOL_MARKETS.keys()))
    if sigs.monitored_mints:
        feed.update_mints(sigs.monitored_mints)

    order_mgr = orders.OrderManager(config)

    payer_kp: Optional[Keypair] = None
    submitter: Optional[Submitter] = None
    payer_pubkey: Optional[Pubkey] = None
    if args.live:
        with open(args.keypair) as f:
            payer_kp = Keypair.from_bytes(bytes(json.load(f)))
        submitter = Submitter()
        payer_pubkey = payer_kp.pubkey()
        logger.info(f"live submit as {payer_pubkey} via SwQOS")
        if not args.no_confirm and sys.stdin.isatty():
            if input("LIVE mode — really? [y/N] ").strip().lower() != "y":
                return
    elif args.bootstrap_pubkey:
        # Dry-run can still bootstrap to observe (but never submits)
        payer_pubkey = Pubkey.from_string(args.bootstrap_pubkey)
        logger.info(f"dry-run with bootstrap from {payer_pubkey}")

    # Bootstrap open orders from chain so a restart doesn't lose track of
    # orders we placed in a previous run.
    if payer_pubkey is not None:
        boot_data = fetch_market_accounts(args.rpc)
        _, _, boot_headers = derive_book_state(boot_data)
        bootstrapped = bootstrap_open_orders(boot_data, boot_headers, payer_pubkey)
        for o in bootstrapped:
            order_mgr.open_orders[o.sequence_number] = o
        logger.info(f"bootstrap: recovered {len(bootstrapped)} open orders from chain")
        for o in bootstrapped:
            logger.info(f"  {o.market_label:>14s} {o.side:>3s} {o.size_base:.4f} "
                        f"@ {o.price:.9f} seq={o.sequence_number} hint={o.order_index_hint}")

    tick = 0
    async for prices in feed.poll():
        tick += 1
        sigs.token_prices.update(prices)
        sigs.sol_usd_price = feed.sol_usd_price

        emitted = sigs.process_prices(prices, time.time())
        manifest_lsts = set(quoter.MANIFEST_LST_SOL_MARKETS.keys())
        actionable = [s for s in emitted
                      if s.basket_size == 2 and all(m in manifest_lsts for m in s.mints)]

        market_data = fetch_market_accounts(args.rpc)
        tobs, live_seqs, headers = derive_book_state(market_data)
        intents = quoter.decide_quotes(config, actionable, tobs)
        result = order_mgr.reconcile(intents, live_seqs)

        logger.info(
            f"tick {tick}  signals={len(emitted)}/actionable={len(actionable)}  "
            f"open={order_mgr.open_count()}  intents={len(intents)}  "
            f"actions: place={len(result.places)} cancel={len(result.cancels)} "
            f"kept={result.kept} cleaned={len(result.cleaned_from_book)}"
        )
        for c in result.cancels:
            logger.info(f"  CANCEL {c.market_label:>14s}  seq={c.sequence_number}  "
                        f"reason={c.reason.value}")
        for p in result.places:
            it = p.intent
            logger.info(f"  PLACE  {it.market_label:>14s} {it.side:>3s} "
                        f"{it.size_base:.4f} @ {it.price:.9f}  ({it.pair_label})")

        # Group + dispatch
        by_market = group_actions_by_market(result.cancels, result.places)
        if by_market and args.live:
            assert payer_kp and submitter
            bh = rpc_call(args.rpc, "getLatestBlockhash", [{"commitment": "processed"}])
            blockhash = bh["result"]["value"]["blockhash"]

            for market, (cancels, places) in by_market.items():
                # Pre-mark cancels so cleanup doesn't double-count
                for c in cancels:
                    order_mgr.register_cancelled(c.sequence_number)
                tx_bytes, sig = build_batch_update_tx(
                    payer_kp, market, cancels, places, headers, blockhash,
                )
                logger.info(f"  TX {market}: cancels={len(cancels)} places={len(places)} "
                            f"size={len(tx_bytes)}B sig={sig[:16]}...")
                relay_sig = submitter.submit(tx_bytes, urgency="normal")
                if relay_sig is None:
                    logger.error(f"  submit failed for {market}")
                    continue
                if not confirm_signature(args.rpc, sig, deadline_s=20):
                    logger.error(f"  not confirmed: {sig}")
                    continue
                # Fetch return data → register placed orders
                ret_data = fetch_program_return(args.rpc, sig, deadline_s=15)
                placed_keys = manifest.parse_batch_update_return(ret_data or b"")
                if len(placed_keys) != len(places):
                    logger.warning(f"  expected {len(places)} placed, return data has "
                                   f"{len(placed_keys)} entries — check parse")
                for pa, (seq, hint) in zip(places, placed_keys):
                    order_mgr.register_placed(pa.intent, seq, hint)
                    logger.info(f"  ✓ placed seq={seq} hint={hint} "
                                f"({pa.intent.market_label} {pa.intent.side})")

        if args.ticks and tick >= args.ticks:
            break

    logger.info(f"done: {tick} ticks. final open_orders={order_mgr.open_count()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=0,
                    help="Stop after N ticks (0 = run forever)")
    ap.add_argument("--tick-interval", type=float, default=6.0)
    ap.add_argument("--entry-z", type=float, default=None,
                    help="Override config.entry_zscore (default 2.5 from config.py)")
    ap.add_argument("--ttl", type=float, default=60.0, help="order_ttl_seconds")
    ap.add_argument("--requote-bps", type=float, default=2.0)
    ap.add_argument("--quote-size-sol", type=float, default=0.5)
    ap.add_argument("--scanner-db", default=os.getenv("SCANNER_DB", "../arbitrage_tracker/arb_tracker.db"))
    ap.add_argument("--rpc", default=os.getenv("SOLANA_RPC_URL"))
    ap.add_argument("--keypair", default=os.getenv("KEYPAIR_FILE", "/home/ubuntu/leeroy-mainnet.json"))
    ap.add_argument("--live", action="store_true",
                    help="Actually submit transactions (default: dry-run)")
    ap.add_argument("--no-confirm", action="store_true")
    ap.add_argument("--bootstrap-pubkey", default=os.getenv("WALLET_ADDRESS"),
                    help="Wallet pubkey for bootstrap (dry-run mode only; live mode "
                         "uses the keypair). Default reads $WALLET_ADDRESS.")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
