"""
Inventory management for LST token-to-token trading.

Instead of holding all capital in SOL, split across 6 LSTs so we can
swap directly between them (1 swap instead of routing through SOL = 2 swaps).

This cuts execution cost from ~12 bps to ~3 bps per trade.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from constants import (
    SOL_MINT, BSOL_MINT, MSOL_MINT, JITOSOL_MINT, JUPSOL_MINT, INF_MINT,
    VSOL_MINT, DSOL_MINT, EDGESOL_MINT, BONKSOL_MINT,
)

logger = logging.getLogger(__name__)

# The 6 LST tokens we hold inventory in
INVENTORY_MINTS = {
    SOL_MINT: "SOL",
    BSOL_MINT: "bSOL",
    MSOL_MINT: "mSOL",
    JITOSOL_MINT: "jitoSOL",
    JUPSOL_MINT: "jupSOL",
    INF_MINT: "INF",
    VSOL_MINT: "vSOL",
    DSOL_MINT: "dSOL",
    EDGESOL_MINT: "edgeSOL",
    BONKSOL_MINT: "bonkSOL",
}

# Target allocation: equal weight across all 6 tokens
TARGET_WEIGHT = 1.0 / len(INVENTORY_MINTS)


@dataclass
class InventoryState:
    """Track current holdings across LST tokens."""
    balances: Dict[str, float] = field(default_factory=dict)  # mint -> SOL-equivalent value
    total_value_sol: float = 0.0

    def get_balance_sol(self, mint: str) -> float:
        """Get balance of a token in SOL-equivalent terms."""
        return self.balances.get(mint, 0.0)

    def get_available_to_sell(self, mint: str) -> float:
        """How much of a token can we sell (above minimum reserve)."""
        balance = self.get_balance_sol(mint)
        # Keep 10% reserve to avoid depleting any token completely
        min_reserve = self.total_value_sol * TARGET_WEIGHT * 0.1
        return max(0, balance - min_reserve)

    def can_trade_pair(self, sell_mint: str, buy_mint: str, amount_sol: float) -> bool:
        """Check if we have enough of sell_mint to fund this trade."""
        return self.get_available_to_sell(sell_mint) >= amount_sol

    def update_from_wallet(self, token_balances: List[Tuple[str, float]],
                           token_prices: Dict[str, float], sol_balance: float,
                           sol_price: float):
        """Sync inventory state from actual wallet balances."""
        self.balances = {}
        self.total_value_sol = 0.0

        # SOL balance
        self.balances[SOL_MINT] = sol_balance
        self.total_value_sol += sol_balance

        # Token balances (convert to SOL-equivalent)
        for mint, ui_amount in token_balances:
            if mint in INVENTORY_MINTS and mint != SOL_MINT:
                usd_price = token_prices.get(mint, 0)
                if usd_price > 0 and sol_price > 0:
                    sol_equiv = ui_amount * usd_price / sol_price
                    self.balances[mint] = sol_equiv
                    self.total_value_sol += sol_equiv

    def log_state(self):
        """Log current inventory allocations."""
        if self.total_value_sol <= 0:
            return
        parts = []
        for mint, name in sorted(INVENTORY_MINTS.items(), key=lambda x: x[1]):
            bal = self.balances.get(mint, 0)
            pct = bal / self.total_value_sol * 100 if self.total_value_sol > 0 else 0
            parts.append(f"{name}={bal:.4f}({pct:.0f}%)")
        logger.info(f"Inventory: {' '.join(parts)} total={self.total_value_sol:.4f} SOL")


def get_trade_route(signal_mints: List[str], signal_type, hedge_ratios: List[float]
                    ) -> Optional[Tuple[str, str]]:
    """
    Determine the optimal swap route for a trade signal.

    For inventory-based trading:
    - Identify which token to sell and which to buy
    - Check if a direct pool exists between them

    Returns (sell_mint, buy_mint) or None if can't determine.
    """
    from signals import SignalType

    if len(signal_mints) != 2 or len(hedge_ratios) != 2:
        return None

    # Compute signed HRs to determine buy/sell sides
    if signal_type == SignalType.ENTRY_LONG:
        signed = [hedge_ratios[i] for i in range(2)]
    else:
        signed = [-hedge_ratios[i] for i in range(2)]

    # For one-sided: one positive (buy), one negative (sell/skip)
    # For pair trade: both same sign
    buy_mints = [signal_mints[i] for i in range(2) if signed[i] > 0]
    sell_mints = [signal_mints[i] for i in range(2) if signed[i] < 0]

    if len(buy_mints) == 1 and len(sell_mints) == 1:
        # One-sided: buy one, skip the other
        # With inventory: sell the "skip" token to buy the "buy" token directly
        return (sell_mints[0], buy_mints[0])

    elif len(buy_mints) == 2:
        # Both positive (pair trade long) — buy both
        # With inventory: sell SOL to buy both, or sell one LST to buy the other
        # For now, sell the one we hold more of
        return None  # handled by pair trade logic

    elif len(sell_mints) == 2:
        # Both negative (pair trade short) — want to sell both
        # With inventory: sell overpriced one, buy underpriced one directly
        # The "overpriced" one has the higher absolute z contribution
        # For simplicity: sell mint[0], buy mint[1] (the reference/hedge structure)
        return (signal_mints[0], signal_mints[1])

    return None


def find_direct_pool(mint_a: str, mint_b: str) -> bool:
    """Check if a direct pool exists between two tokens."""
    try:
        from direct_swap import has_direct_pool
        return has_direct_pool(mint_a, mint_b)
    except ImportError:
        return False


def needs_initial_split(sol_balance: float, token_balances: List[Tuple[str, float]],
                        token_prices: Dict[str, float], sol_price: float) -> bool:
    """Check if we need to do the initial SOL → LST split."""
    if sol_balance <= 0 or sol_price <= 0:
        return False

    total_sol = sol_balance
    lst_value_sol = 0
    lst_count = 0
    for mint, ui_amount in token_balances:
        if mint in INVENTORY_MINTS and mint != SOL_MINT and ui_amount > 0.0001:
            usd = token_prices.get(mint, 0)
            if usd > 0 and sol_price > 0:
                lst_value_sol += ui_amount * usd / sol_price
            lst_count += 1

    total = total_sol + lst_value_sol
    if total <= 0:
        return False

    # Need split if: any LST is missing, or SOL is way above target (~17%)
    expected_lst_count = len(INVENTORY_MINTS) - 1  # 5 non-SOL LSTs
    if lst_count < expected_lst_count:
        return True  # missing LSTs — need to buy them

    # All LSTs present — only split if SOL is >40% of total (way above 17% target)
    sol_pct = sol_balance / total
    return sol_pct > 0.40
