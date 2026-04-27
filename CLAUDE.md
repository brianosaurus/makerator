# CLAUDE.md — Makerator

## Project

Makerator — market-making bot for Solana LST tokens. Places limit orders on on-chain order books (Manifest, Phoenix) and concentrated liquidity positions (Meteora DLMM bins) to earn spread from cointegrated LST pairs.

**Evolution from statalyzer**: The statalyzer rebalancer proved that LST mean-reversion signals are real (90-98% reversion rate) but market-order execution costs (10-15 bps) eat the entire 12-45 bps edge. Makerator flips the model: instead of paying slippage to swap, place limit orders and earn spread when others fill against you.

**Stack:** Python 3 · Solana RPC · Manifest SDK · asyncio · numpy

## Architecture Vision

- **Signal source**: Cointegration z-scores from statalyzer's signal generator (or inline)
- **Order placement**: Manifest on-chain limit orders only for v1. Meteora DLMM deferred.
- **Inventory management**: Hold 10 LST tokens, rebalance via order fills
- **Risk**: Position limits per token, max inventory deviation from target weight

## Locked v1 decisions (2026-04-26)

- **Venue:** Manifest only. Phoenix/DLMM are post-v1 extensions.
- **Capital:** 10 SOL target, split across the 10 LSTs. Top up from current ~2 SOL split when Phase 4 begins.
- **DB:** Fresh `makerator.db`, not shared with statalyzer.
- **Scanner DB:** Read-only at `../arbitrage_tracker/arb_tracker.db` (same as statalyzer).
- **`.env`:** Symlink to `../statalyzer/.env` (locally and on `frankfurt`). One source of truth for RPC, wallet, Jupiter creds. Makerator-specific tunables (capital target, spread, TTL) live in `config.py` defaults, not in the shared `.env`.

## Build phases

0. **Skeleton** — repo, deps, env, deploy. *(this phase)*
1. **Lift reusables** — copy constants/price_feed/signals/inventory/portfolio/risk from `../statalyzer` and trim.
2. **Manifest primitives** — PlaceOrder/CancelOrder/SettleFunds ix builders + orderbook parser. **Biggest unknown.**
3. **Paper loop** — `makerator.py` main + `orders.py` lifecycle, no chain writes.
4. **Live single-pair** — one Manifest market (jupSOL/SOL), 24h soak.
5. **Multi-pair** — all 5 LST/SOL Manifest markets.

## Deploy

- Server: `frankfurt` (same as statalyzer)
- Wallet: `Dx4wVQL1ZofsypesxS96mga2uhygLwGRwVc4egju4tq5`
- Keypair on server: `/home/ubuntu/leeroy-mainnet.json`
- `./deploy.sh [args]` — tars `*.py` + `requirements.txt`, ships, installs, runs `makerator.py`.

## Commands

```bash
# SSH to server
ssh frankfurt

# Companion bot
ls ../statalyzer

# Scanner DB (read-only)
ls ../arbitrage_tracker/arb_tracker.db
```
