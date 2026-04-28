"""
Risk controls for statalayer.
All checks must pass before a new position is opened.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskCheck:
    allowed: bool
    reason: str = ""


class RiskManager:
    """Evaluates risk before allowing new positions."""

    def __init__(self, config, portfolio, slippage_monitor=None, rl_enabled=False, regime_detector=None):
        self.config = config
        self.portfolio = portfolio
        self.slippage_monitor = slippage_monitor
        self.rl_enabled = rl_enabled
        self.regime_detector = regime_detector
        self.entries_this_hour: list = []  # timestamps
        self.kill_switch: bool = False
        self.kill_switch_time: float = 0  # when kill switch was engaged
        self.kill_switch_cooldown: float = 600  # 10 minutes

    def check_entry(self, signal, pair_state) -> RiskCheck:
        """Run all risk checks before allowing a new entry."""
        # 1. Kill switch — disabled
        # if self.kill_switch:
        #     pass

        # 1a. Time-of-day gate — skip high-loss UTC hours
        import time as _time
        hour_utc = int(_time.strftime('%H'))
        blocked_hours = getattr(self.config, 'blocked_hours_utc', None)
        if blocked_hours and hour_utc in blocked_hours:
            return RiskCheck(False, f"Time gate: UTC {hour_utc} blocked")

        # 1b. Regime check — block entries in danger mode
        if self.regime_detector is not None:
            regime = self.regime_detector.get_regime()
            if regime == "danger":
                return RiskCheck(False, "Regime: danger — pausing entries")

        # 1b. Entry z-score cap (per-direction + per-pair overrides)
        abs_z = abs(signal.zscore)
        if self.rl_enabled:
            hard_cap = self.config.stop_loss_zscore * 0.9
        else:
            hard_cap = self.config.max_entry_zscore
            is_short = signal.zscore > 0
            if is_short and self.config.short_max_entry_zscore is not None:
                hard_cap = self.config.short_max_entry_zscore

            # Widen max-entry-z for pairs with direct pool (cheaper execution)
            try:
                from direct_swap import has_direct_pool
                from constants import SOL_MINT
                if any(has_direct_pool(m, SOL_MINT) for m in signal.mints if m != SOL_MINT):
                    direct_long_cap = getattr(self.config, 'direct_max_entry_zscore', 1.50)
                    direct_short_cap = getattr(self.config, 'direct_short_max_entry_zscore', 1.80)
                    hard_cap = direct_short_cap if is_short else direct_long_cap
            except ImportError:
                pass

        if abs_z > hard_cap:
            return RiskCheck(False, f"|z|={abs_z:.1f} > max entry {hard_cap:.1f}")

        # 1c. In caution regime, require higher z to enter
        if self.regime_detector and self.regime_detector.get_regime() == "caution":
            effective_entry_z = abs_z / self.regime_detector.get_entry_z_multiplier()
            if effective_entry_z < self.config.entry_zscore:
                return RiskCheck(False, f"Regime: caution — effective |z|={effective_entry_z:.1f} < entry {self.config.entry_zscore:.1f}")

        # 2. Already in this pair
        if self.portfolio.has_position(signal.pair_key):
            return RiskCheck(False, f"Already in {signal.pair_key}")

        # 3. Max positions
        num_open = len(self.portfolio.positions)
        if num_open >= self.config.max_positions:
            return RiskCheck(False, f"Max positions ({self.config.max_positions}) reached")

        # 3b. Concentration limit — max positions sharing a token
        # Per-token limits based on liquidity tier (deep liquidity = higher limit)
        from constants import STABLECOIN_MINTS, TOKEN_LIQUIDITY_TIER
        base_max = self.config.max_positions_per_token
        for mint in signal.mints:
            if mint in STABLECOIN_MINTS:
                continue
            tier_mult = TOKEN_LIQUIDITY_TIER.get(mint, 1.0)
            max_for_token = int(base_max * tier_mult)
            count = sum(
                1 for p in self.portfolio.positions.values()
                if mint in p.mints
            )
            if count >= max_for_token:
                return RiskCheck(False, f"Concentration limit ({max_for_token}) for {mint[:8]}.. reached")

        # 3b2. Slippage check — block entries until first measurement, then reject unmeasured tokens
        if self.slippage_monitor is not None:
            if not self.slippage_monitor.is_ready():
                return RiskCheck(False, "Slippage monitor warming up")
            if not self.slippage_monitor.is_basket_tradeable(signal.mints):
                return RiskCheck(False, "Unmeasured token in basket")

        # 3c. Rate limit — max entries per hour
        # When RL is enabled, allow 3x the configured rate (RL controls pacing via PASS)
        self._prune_entries()
        rate_cap = self.config.max_positions_per_hour * (3 if self.rl_enabled else 1)
        if len(self.entries_this_hour) >= rate_cap:
            return RiskCheck(False, f"Rate limit ({rate_cap}/hr) reached")

        # 4. Max exposure (scales with portfolio capital)
        exposure = self.portfolio.get_total_exposure()
        max_exposure = self.portfolio.initial_capital * self.config.max_exposure_ratio
        if exposure >= max_exposure:
            return RiskCheck(False, f"Max exposure (${max_exposure:.0f}) reached")

        # 5. Drawdown check — disabled
        # if self.check_drawdown():
        #     pass

        # 6. Pair staleness
        analyzed_at = pair_state.cointegration_analyzed_at
        if analyzed_at > 0:
            age_hours = (time.time() - analyzed_at) / 3600
            if age_hours > self.config.pair_staleness_hours:
                return RiskCheck(False, f"Pair stale ({age_hours:.0f}h > {self.config.pair_staleness_hours}h)")

        # 7. Half-life filter (skip for LST-only pairs — they revert via peg regardless)
        from constants import SOL_MINT, BSOL_MINT, MSOL_MINT, JITOSOL_MINT, JUPSOL_MINT, INF_MINT
        _LST_MINTS = {SOL_MINT, BSOL_MINT, MSOL_MINT, JITOSOL_MINT, JUPSOL_MINT, INF_MINT}
        is_lst_only = all(m in _LST_MINTS for m in signal.mints)
        if not is_lst_only:
            hl = pair_state.half_life
            if hl < self.config.min_half_life:
                return RiskCheck(False, f"Half-life too short ({hl:.1f} < {self.config.min_half_life})")
            lookback_blocks = self.config.lookback_window * self.config.signal_resample_secs / 0.4
            max_hl = lookback_blocks * self.config.max_half_life_ratio
            if hl > max_hl and hl != float('inf'):
                return RiskCheck(False, f"Half-life too long ({hl:.1f} > {max_hl:.1f})")
            hl_secs = hl * 0.4
            if hl_secs > self.config.max_half_life_secs:
                return RiskCheck(False, f"Half-life {hl_secs:.0f}s > {self.config.max_half_life_secs:.0f}s max")

        return RiskCheck(True)

    def check_exit(self, signal) -> RiskCheck:
        """Check if we should exit a position."""
        if not self.portfolio.has_position(signal.pair_key):
            return RiskCheck(False, "No position to exit")
        return RiskCheck(True)

    def check_drawdown(self) -> bool:
        """If drawdown exceeds threshold, engage kill switch. Returns True if triggered."""
        dd = self.portfolio.get_drawdown()
        if dd > self.config.max_drawdown_pct:
            if not self.kill_switch:
                logger.warning(f"KILL SWITCH engaged: drawdown {dd:.1%} > {self.config.max_drawdown_pct:.1%}")
                self.kill_switch = True
                self.kill_switch_time = time.time()
            return True
        return False

    def record_entry(self):
        self.entries_this_hour.append(time.time())

    def _prune_entries(self):
        cutoff = time.time() - 3600
        self.entries_this_hour = [t for t in self.entries_this_hour if t > cutoff]

    def reset_kill_switch(self):
        logger.info("Kill switch manually reset")
        self.kill_switch = False
