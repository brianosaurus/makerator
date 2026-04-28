"""
Portfolio manager for statalayer.
Tracks open positions, marks-to-market, computes P&L, persists state.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PositionStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    STOPPED_OUT = "stopped_out"


@dataclass
class Position:
    pair_key: str             # basket key
    basket_size: int
    mints: List[str]          # sorted token mints
    direction: str            # "long" or "short" (refers to the spread)
    hedge_ratios: List[float] # Johansen eigenvector weights (length N)

    # Entry state
    entry_time: int
    entry_slot: int
    entry_zscore: float
    entry_prices: List[float]    # USD price per token at entry
    quantities: List[float]      # human-readable amount per token
    quantities_raw: List[int]    # raw lamports/units per token
    entry_values: List[float]    # USD value per leg at entry

    # Current state
    current_prices: List[float] = field(default_factory=list)
    current_zscore: float = 0.0
    unrealized_pnl: float = 0.0

    # Exit state
    exit_time: Optional[int] = None
    exit_slot: Optional[int] = None
    exit_zscore: Optional[float] = None
    exit_prices: Optional[List[float]] = None
    realized_pnl: float = 0.0

    status: PositionStatus = PositionStatus.OPEN
    is_paper: bool = True
    id: Optional[int] = None
    fees_usd: float = 0.0  # accumulated transaction fees (entry + exit)
    features: dict = field(default_factory=dict)


class PortfolioManager:
    """Tracks positions, P&L, and portfolio state."""

    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.positions: Dict[str, Position] = {}  # pair_key -> Position
        self.closed_positions: List[Position] = []
        self.initial_capital: float = config.initial_capital
        self.total_realized_pnl: float = 0.0
        self.peak_value: float = config.initial_capital
        self._wallet_synced_value: Optional[float] = None  # actual on-chain wallet value
        self._load_state()

    def _load_state(self):
        """Load state from DB for crash recovery."""
        saved_capital = self.db.get_state('initial_capital')
        if saved_capital:
            self.initial_capital = float(saved_capital)

        saved_peak = self.db.get_state('peak_value')
        if saved_peak:
            self.peak_value = float(saved_peak)
        else:
            self.peak_value = self.initial_capital

        saved_pnl = self.db.get_state('total_realized_pnl')
        if saved_pnl:
            self.total_realized_pnl = float(saved_pnl)

        # Load open positions
        rows = self.db.get_open_positions()
        columns = self.db.get_position_columns()

        for row in rows:
            data = dict(zip(columns, row))
            mints = json.loads(data['mints_json'])
            hedge_ratios = json.loads(data['hedge_ratios_json'])
            entry_prices = json.loads(data['entry_prices_json'])
            quantities = json.loads(data['quantities_json'])
            quantities_raw = json.loads(data['quantities_raw_json'])
            entry_values = json.loads(data['entry_values_json'])

            position = Position(
                pair_key=data['pair_key'],
                basket_size=data['basket_size'],
                mints=mints,
                direction=data['direction'],
                hedge_ratios=hedge_ratios,
                entry_time=data['entry_time'],
                entry_slot=data['entry_slot'],
                entry_zscore=data['entry_zscore'],
                entry_prices=entry_prices,
                quantities=quantities,
                quantities_raw=quantities_raw,
                entry_values=entry_values,
                current_prices=list(entry_prices),  # init to entry prices
                status=PositionStatus.OPEN,
                is_paper=bool(data['is_paper']),
                id=data['id'],
            )
            self.positions[position.pair_key] = position

        if self.positions:
            logger.info(f"Recovered {len(self.positions)} open positions from DB")

    def open_position(self, signal, size, is_paper: bool = True,
                      prices: List[float] = None,
                      fees_usd: float = 0.0,
                      signal_id: int = None) -> Position:
        """Create a new position from a signal and size."""
        direction = "long" if signal.signal_type.value == "entry_long" else "short"

        position = Position(
            pair_key=signal.pair_key,
            basket_size=signal.basket_size,
            mints=signal.mints,
            direction=direction,
            hedge_ratios=signal.hedge_ratios,
            entry_time=signal.timestamp,
            entry_slot=signal.slot,
            entry_zscore=signal.zscore,
            entry_prices=prices if prices else [0.0] * signal.basket_size,
            quantities=size.amounts,
            quantities_raw=size.amounts_raw,
            entry_values=size.dollar_amounts,
            is_paper=is_paper,
        )
        position.current_prices = list(position.entry_prices)
        position.fees_usd = fees_usd

        # Persist immediately
        self.db.save_position(position, signal_id=signal_id)
        self.positions[position.pair_key] = position

        sol_price = getattr(self, "_last_sol_price", 0) or 85.0
        exposure_sol = size.total_exposure_usd / sol_price if sol_price > 0 else 0
        logger.info(f"Opened {direction} position: {signal.pair_key[:16]}.. "
                     f"z={signal.zscore:+.2f} "
                     f"{exposure_sol:.4f} SOL exposure")
        return position

    def close_position(self, pair_key: str, exit_zscore: float,
                       exit_slot: int, reason: str,
                       exit_fees_usd: float = 0.0) -> Optional[Position]:
        """Close an existing position and compute realized P&L."""
        position = self.positions.get(pair_key)
        if not position:
            return None

        position.exit_time = int(time.time())
        position.exit_slot = exit_slot
        position.exit_zscore = exit_zscore
        position.exit_prices = list(position.current_prices)

        # Detect and fix swapped exit prices (intermittent bug where exit price
        # array has values in reversed order vs entry prices)
        if position.basket_size == 2 and all(p > 0 for p in position.entry_prices):
            r0 = position.exit_prices[0] / position.entry_prices[0] if position.entry_prices[0] > 0 else 1.0
            r1 = position.exit_prices[1] / position.entry_prices[1] if position.entry_prices[1] > 0 else 1.0
            if r0 > 10 or r0 < 0.1 or r1 > 10 or r1 < 0.1:
                # Check if swapping fixes it
                sr0 = position.exit_prices[1] / position.entry_prices[0] if position.entry_prices[0] > 0 else 999
                sr1 = position.exit_prices[0] / position.entry_prices[1] if position.entry_prices[1] > 0 else 999
                if 0.5 < sr0 < 2.0 and 0.5 < sr1 < 2.0:
                    logger.warning(f"Exit prices swapped for {pair_key[:16]}.. — "
                                   f"entry={position.entry_prices}, exit={position.exit_prices} — correcting")
                    position.exit_prices[0], position.exit_prices[1] = position.exit_prices[1], position.exit_prices[0]
                    position.current_prices[0], position.current_prices[1] = position.current_prices[1], position.current_prices[0]

        # Compute realized P&L per leg
        pnl = 0.0
        one_sided = getattr(self.config, "one_sided_trades", True)
        signed_hrs = [position.hedge_ratios[i] if position.direction == "long" else -position.hedge_ratios[i]
                      for i in range(position.basket_size)]
        is_pair_trade = all(s > 0 for s in signed_hrs) or all(s < 0 for s in signed_hrs)
        for i in range(position.basket_size):
            signed_hr = signed_hrs[i]
            # Skip hedge leg in one-sided mode (was never traded) — but not for pair trades
            if one_sided and signed_hr < 0 and not is_pair_trade:
                continue
            if signed_hr > 0:
                # Bought this token
                pnl += (position.current_prices[i] - position.entry_prices[i]) * position.quantities[i]
            else:
                # Sold this token
                pnl += (position.entry_prices[i] - position.current_prices[i]) * position.quantities[i]

        # Convert to SOL (divide USD pnl by SOL price, fallback to 85)
        sol_price = getattr(self, "_last_sol_price", 85.0)
        position.realized_pnl = pnl / sol_price if sol_price > 0 else pnl / 85.0

        # Deduct transaction fees in SOL
        position.fees_usd += exit_fees_usd
        position.realized_pnl -= (position.fees_usd / sol_price) if sol_price > 0 else 0

        # Sanity cap: can't lose more than total entry value (in SOL)
        entry_value_sol = sum(position.entry_values) / sol_price if sol_price > 0 else 0
        if entry_value_sol > 0 and position.realized_pnl < -entry_value_sol:
            logger.warning(f"P&L {position.realized_pnl:+.6f} SOL exceeds entry value {entry_value_sol:.6f} SOL — "
                           f"capping to -{entry_value_sol:.6f} SOL (price data error)")
            position.realized_pnl = -entry_value_sol

        position.status = PositionStatus.STOPPED_OUT if reason == "stop_loss" else PositionStatus.CLOSED

        # Add exit features for ML
        hold_secs = position.exit_time - position.entry_time if position.exit_time and position.entry_time else 0
        position.features.update({
            'exit_z': exit_zscore,
            'exit_reason': reason,
            'hold_secs': hold_secs,
            'fees_usd': round(position.fees_usd, 6),
            'pnl': round(position.realized_pnl, 6),
            'won': bool(position.realized_pnl > 0),
        })

        # Update totals
        self.total_realized_pnl += position.realized_pnl
        self.db.set_state('total_realized_pnl', str(self.total_realized_pnl))

        # Compound reinvest — skip if wallet-synced (next sync picks up actual balance)
        if self._wallet_synced_value is None:
            self.initial_capital += position.realized_pnl
            self.db.set_state('initial_capital', str(self.initial_capital))
        if position.realized_pnl >= 0:
            logger.info(f"Closed P&L +{position.realized_pnl:.6f} SOL → capital {self.initial_capital:.6f} SOL")
        else:
            logger.info(f"Closed P&L {position.realized_pnl:.6f} SOL → capital {self.initial_capital:.6f} SOL")

        # Persist
        self.db.update_position(position)
        del self.positions[pair_key]
        self.closed_positions.append(position)

        fees_sol = (position.fees_usd / sol_price) if sol_price > 0 and position.fees_usd > 0 else 0
        logger.info(f"Closed {position.direction} {pair_key[:16]}.. ({reason}): "
                     f"P&L {position.realized_pnl:+.6f} SOL"
                     f"{f' (fees: {fees_sol:.6f} SOL)' if fees_sol > 0 else ''}")
        return position

    def mark_to_market(self, prices: Dict[str, float]):
        """Update all open positions with latest prices."""
        for position in self.positions.values():
            for i, mint in enumerate(position.mints):
                p = prices.get(mint)
                if p and p > 0:
                    position.current_prices[i] = p

            # Compute unrealized P&L (only traded legs in one-sided mode, both legs for pair trades)
            pnl = 0.0
            one_sided = getattr(self.config, "one_sided_trades", True)
            signed_hrs_mtm = [position.hedge_ratios[j] if position.direction == "long" else -position.hedge_ratios[j]
                              for j in range(position.basket_size)]
            is_pair_trade_mtm = all(s > 0 for s in signed_hrs_mtm) or all(s < 0 for s in signed_hrs_mtm)
            for i in range(position.basket_size):
                signed_hr = signed_hrs_mtm[i]
                if one_sided and signed_hr < 0 and not is_pair_trade_mtm:
                    continue
                if signed_hr > 0:
                    pnl += (position.current_prices[i] - position.entry_prices[i]) * position.quantities[i]
                else:
                    pnl += (position.entry_prices[i] - position.current_prices[i]) * position.quantities[i]

            position.unrealized_pnl = pnl

        # Update peak value (realized capital only — unrealized is too noisy)
        if self.initial_capital > self.peak_value:
            self.peak_value = self.initial_capital
            self.db.set_state('peak_value', str(self.peak_value))

    def get_total_exposure(self) -> float:
        total = 0.0
        for p in self.positions.values():
            n = min(p.basket_size, len(p.current_prices), len(p.quantities))
            for i in range(n):
                total += abs(p.current_prices[i] * p.quantities[i])
        return total

    def get_total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def get_total_value(self) -> float:
        # In live mode with wallet sync, use the actual on-chain wallet value.
        # The computed initial_capital + unrealized_pnl double-counts because
        # sync_wallet_capital already includes position token values.
        if self._wallet_synced_value is not None:
            return self._wallet_synced_value
        return self.initial_capital + self.get_total_unrealized_pnl()

    def get_drawdown(self) -> float:
        """Drawdown from peak wallet value."""
        if self.peak_value <= 0:
            return 0.0
        current = self._wallet_synced_value if self._wallet_synced_value is not None else self.initial_capital
        return max(0.0, (self.peak_value - current) / self.peak_value)

    def has_position(self, pair_key: str) -> bool:
        return pair_key in self.positions

    def take_snapshot(self):
        """Save a portfolio snapshot to DB."""
        self.db.save_snapshot(
            total_value=self.get_total_value(),
            num_positions=len(self.positions),
            total_exposure=self.get_total_exposure(),
            realized_pnl=self.total_realized_pnl,
            unrealized_pnl=self.get_total_unrealized_pnl(),
            drawdown_pct=self.get_drawdown(),
        )

    def sync_wallet_capital(self, sol_balance: float, sol_usd_price: float,
                            token_balances: list = None, token_prices: dict = None):
        """Sync capital from actual on-chain wallet value.

        Sets initial_capital to SOL value only (free cash not in positions).
        Sets _wallet_synced_value to full wallet value (SOL + position tokens,
        excluding orphans) for accurate portfolio value display.

        Args:
            sol_balance: SOL balance in SOL units
            sol_usd_price: current SOL/USD price
            token_balances: list of (mint, ui_amount) for non-SOL tokens
            token_prices: {mint: usd_price} from price feed
        """
        if sol_usd_price <= 0:
            return
        self._last_sol_price = sol_usd_price
        sol_usd = sol_balance * sol_usd_price

        # Separate position tokens from orphans
        position_mints = set()
        for pos in self.positions.values():
            position_mints.update(pos.mints)

        position_token_value = 0.0
        orphan_value = 0.0
        if token_balances and token_prices:
            for mint, ui_amount in token_balances:
                price = token_prices.get(mint, 0)
                if price > 0 and ui_amount > 0:
                    if mint in position_mints:
                        position_token_value += ui_amount * price
                    else:
                        orphan_value += ui_amount * price

        # initial_capital = full wallet value (SOL + position tokens)
        old_capital = self.initial_capital
        self.initial_capital = sol_usd + position_token_value
        self.db.set_state('initial_capital', str(self.initial_capital))

        # Wallet-synced total = SOL + position tokens (excludes orphans)
        self._wallet_synced_value = sol_usd + position_token_value

        # Update peak based on total wallet value
        if self._wallet_synced_value > self.peak_value:
            self.peak_value = self._wallet_synced_value
            self.db.set_state('peak_value', str(self.peak_value))

        if abs(old_capital - self.initial_capital) > 0.01:
            token_value_sol = position_token_value / sol_usd_price if sol_usd_price > 0 else 0
            total_sol = self._wallet_synced_value / sol_usd_price if sol_usd_price > 0 else 0
            orphan_sol = orphan_value / sol_usd_price if sol_usd_price > 0 else 0
            logger.debug(f"Capital synced: SOL={sol_balance:.4f} tokens={token_value_sol:.4f} SOL "
                         f"total={total_sol:.4f} SOL"
                         f"{f' [orphans: {orphan_sol:.4f} SOL excluded]' if orphan_sol > 0.001 else ''}")

    def save_capital(self):
        """Persist initial capital to DB and ensure peak_value is consistent."""
        self.db.set_state('initial_capital', str(self.initial_capital))
        if self.initial_capital > self.peak_value:
            self.peak_value = self.initial_capital
            self.db.set_state('peak_value', str(self.peak_value))
