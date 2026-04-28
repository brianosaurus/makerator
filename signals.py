"""
Signal generator for statalayer.
Monitors cointegrated baskets (2-4 tokens), computes rolling z-scores
from Jupiter prices, and emits entry/exit signals.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

import numpy as np

from constants import WELL_KNOWN_TOKENS, STABLECOIN_MINTS, SOL_MINT

logger = logging.getLogger(__name__)


class SignalType(Enum):
    ENTRY_LONG = "entry_long"
    ENTRY_SHORT = "entry_short"
    EXIT = "exit"
    STOP_LOSS = "stop_loss"


@dataclass
class Signal:
    signal_type: SignalType
    pair_key: str             # basket key (comma-separated sorted mints)
    basket_size: int          # 2, 3, or 4
    mints: List[str]          # sorted token mints
    symbols: List[str]        # corresponding symbols
    hedge_ratios: List[float] # Johansen eigenvector weights (length N)
    zscore: float
    spread: float
    spread_mean: float
    spread_std: float
    timestamp: int
    slot: int
    # Rich features for ML training data
    spread_velocity: float = 0.0       # normalized slope of spread over last 10 candles
    zscore_velocity: float = 0.0       # z-score change over last 5 candles
    spread_volatility: float = 0.0     # std of spread changes (realized vol)
    half_life_secs: float = 0.0        # pair half-life in seconds
    eg_p_value: float = 1.0            # cointegration p-value
    buffer_count: int = 0              # candles in buffer (data quality)
    price_momentum: List[float] = field(default_factory=list)  # per-token log returns over last 10 candles


class CircularBuffer:
    """Fixed-size numpy circular buffer for O(1) append."""

    def __init__(self, capacity: int):
        self.data = np.empty(capacity, dtype=np.float64)
        self.capacity = capacity
        self.write_idx = 0
        self.count = 0

    def append(self, value: float):
        self.data[self.write_idx] = value
        self.write_idx = (self.write_idx + 1) % self.capacity
        self.count = min(self.count + 1, self.capacity)

    def get_array(self) -> np.ndarray:
        if self.count == 0:
            return np.array([], dtype=np.float64)
        if self.count < self.capacity:
            return self.data[:self.count].copy()
        return np.concatenate([
            self.data[self.write_idx:],
            self.data[:self.write_idx]
        ])

    @property
    def latest(self) -> float:
        if self.count == 0:
            return 0.0
        idx = (self.write_idx - 1) % self.capacity
        return self.data[idx]


class SpreadKalmanFilter:
    """1-D Kalman filter for spread denoising.

    State model: spread follows a random walk (process noise Q).
    Observation model: observed spread = true spread + noise (measurement noise R).

    Q controls how quickly the filter adapts to real spread changes.
    R controls how much it trusts each new observation.
    Higher R/Q ratio = more smoothing.
    """

    def __init__(self, process_noise: float = 1e-5, measurement_noise: float = 1e-3):
        self.Q = process_noise       # process noise variance
        self.R = measurement_noise   # measurement noise variance
        self.x = 0.0                 # state estimate (filtered spread)
        self.P = 1.0                 # estimate uncertainty
        self.initialized = False

    def update(self, observation: float) -> float:
        """Feed a new spread observation, return filtered spread."""
        if not self.initialized:
            self.x = observation
            self.P = self.R
            self.initialized = True
            return self.x

        # Predict
        x_pred = self.x
        P_pred = self.P + self.Q

        # Update
        K = P_pred / (P_pred + self.R)    # Kalman gain
        self.x = x_pred + K * (observation - x_pred)
        self.P = (1 - K) * P_pred

        return self.x

    def filter_live(self, observation: float) -> float:
        """Get filtered value for a live observation without updating state.
        Used for tick-level z-score between candle resamples."""
        if not self.initialized:
            return observation
        P_pred = self.P + self.Q
        K = P_pred / (P_pred + self.R)
        return self.x + K * (observation - self.x)


@dataclass
class BasketState:
    """Tracks rolling price window and cointegration params for one basket."""
    pair_key: str             # basket key (comma-separated sorted mints)
    basket_size: int          # 2, 3, or 4
    mints: List[str]          # sorted token mints
    symbols: List[str]        # corresponding symbols
    hedge_ratios: List[float] # Johansen eigenvector weights (length N)
    half_life: float
    eg_p_value: float

    # Rolling log-price buffers: one per token
    price_buffers: List[CircularBuffer] = field(default=None, repr=False)

    # Cached z-score
    current_zscore: float = 0.0
    current_spread: float = 0.0
    spread_mean: float = 0.0
    spread_std: float = 0.0
    last_update_slot: int = 0
    cointegration_analyzed_at: int = 0
    in_position: bool = False
    position_entry_time: float = 0.0
    position_entry_zscore: float = 0.0  # sign indicates direction

    # Resampling state: latest pending price per token
    pending_prices: List[float] = field(default=None, repr=False)
    last_resample_time: float = 0.0

    # Kalman filter for spread denoising
    kalman: Optional[SpreadKalmanFilter] = field(default=None, repr=False)

    # Stop-loss confirmation: require N consecutive ticks above threshold
    stop_loss_ticks: int = 0

    def init_buffers(self, capacity: int):
        self.price_buffers = [CircularBuffer(capacity) for _ in range(self.basket_size)]
        self.pending_prices = [0.0] * self.basket_size
        self.kalman = SpreadKalmanFilter()


def make_basket_key(mints: List[str]) -> str:
    """Canonical basket key — comma-separated sorted mints."""
    return ",".join(sorted(mints))


# Backward compat alias
def make_pair_key(mint_a: str, mint_b: str) -> str:
    return make_basket_key([mint_a, mint_b])


def token_symbol(mint: str) -> str:
    info = WELL_KNOWN_TOKENS.get(mint)
    return info['symbol'] if info else mint[:8] + '..'


class SignalGenerator:
    """Monitors cointegrated baskets and generates trading signals."""

    def __init__(self, config, scanner_db_path: str = None, db=None):
        self.config = config
        self.scanner_db_path = scanner_db_path
        self.db = db  # For candle persistence
        self.baskets: Dict[str, BasketState] = {}
        self.token_prices: Dict[str, float] = {}  # USD-normalized prices
        self.monitored_mints: Set[str] = set()
        self.last_pair_reload = 0
        self.pair_reload_interval = 900  # 15 minutes
        self.sol_usd_price: float = 0.0  # cached SOL/USD

        self.regime_detector = None  # Set externally for regime-aware exit timing

        # Inline cointegration discovery
        from cointegration import CointegrationDiscovery
        self.discovery = CointegrationDiscovery(config, WELL_KNOWN_TOKENS, STABLECOIN_MINTS)

    def load_baskets(self) -> int:
        """Load cointegrated baskets from scanner DB."""
        if not self.scanner_db_path:
            return 0

        from db import Database

        baskets_data = Database.read_cointegrated_baskets(self.scanner_db_path)
        if not baskets_data:
            logger.warning("No cointegrated baskets found in scanner DB")
            return 0

        loaded = 0
        skipped_stable = 0
        skipped_unknown = 0
        for b in baskets_data:
            # Half-life filter (scanner reports half-life in periods of its resample interval)
            hl_periods = b.get('half_life', float('inf'))
            if hl_periods == float('inf') or hl_periods <= 0:
                continue
            # Convert scanner periods to seconds (scanner uses ~300s resample)
            hl_secs = hl_periods * 300
            if hl_secs > self.config.max_half_life_secs:
                continue
            # Also convert to blocks for internal use
            hl = hl_secs / 0.4  # blocks
            if hl < self.config.min_half_life:
                continue

            # Staleness filter
            analyzed_at = b.get('analyzed_at', 0)
            if isinstance(analyzed_at, str):
                try:
                    analyzed_at = int(float(analyzed_at))
                except ValueError:
                    analyzed_at = 0
            age_hours = (time.time() - analyzed_at) / 3600 if analyzed_at > 0 else float('inf')
            if age_hours > self.config.pair_staleness_hours:
                continue

            mints = b['mints']
            symbols = b['symbols']
            hedge_ratios = b['hedge_ratios']
            basket_size = len(mints)

            # Well-known token filter: require at least ONE token to be known
            if not any(m in WELL_KNOWN_TOKENS for m in mints):
                skipped_unknown += 1
                continue

            # Skip baskets containing stablecoins
            if any(m in STABLECOIN_MINTS for m in mints):
                skipped_stable += 1
                continue

            # Token whitelist: if set, ALL mints must be whitelisted
            if self.config.token_whitelist_mints:
                if not all(m in self.config.token_whitelist_mints for m in mints):
                    continue

            # Correlation gate — use the LOWEST per-direction threshold to load all candidates
            min_corr = getattr(self.config, 'min_correlation', 0.0)
            if self.config.short_min_correlation is not None and self.config.short_min_correlation < min_corr:
                min_corr = self.config.short_min_correlation
            if self.config.long_min_correlation is not None and self.config.long_min_correlation < min_corr:
                min_corr = self.config.long_min_correlation
            if min_corr > 0:
                corr = b.get('correlation')
                if corr is None or corr < min_corr:
                    continue

            basket_key = make_basket_key(mints)

            if basket_key in self.baskets:
                # Update existing basket's params
                self.baskets[basket_key].hedge_ratios = hedge_ratios
                self.baskets[basket_key].half_life = hl
                self.baskets[basket_key].cointegration_analyzed_at = analyzed_at
            else:
                state = BasketState(
                    pair_key=basket_key,
                    basket_size=basket_size,
                    mints=mints,
                    symbols=symbols,
                    hedge_ratios=hedge_ratios,
                    half_life=hl,
                    eg_p_value=b.get('eg_p_value', 1.0) or 1.0,
                    cointegration_analyzed_at=analyzed_at,
                )
                state.init_buffers(self.config.lookback_window)
                self._restore_candles(state)
                self.baskets[basket_key] = state

            for m in mints:
                self.monitored_mints.add(m)
            loaded += 1

        if skipped_stable or skipped_unknown:
            logger.info(f"Skipped {skipped_stable} stablecoin baskets, "
                        f"{skipped_unknown} baskets with no well-known token")

        self.last_pair_reload = time.time()
        logger.info(f"Loaded {loaded} cointegrated baskets, monitoring {len(self.monitored_mints)} tokens")
        return loaded

    # Backward compat alias
    def load_pairs(self) -> int:
        return self.load_baskets()

    def load_discovered_pairs(self, results) -> int:
        """Load pairs discovered by inline cointegration analysis.
        Converts 2-token CointResult to basket format."""
        loaded = 0
        for r in results:
            if not r.eg_is_cointegrated:
                continue
            if r.token_a_mint in STABLECOIN_MINTS or r.token_b_mint in STABLECOIN_MINTS:
                continue
            # Token whitelist: if set, ALL mints must be whitelisted
            if self.config.token_whitelist_mints:
                if r.token_a_mint not in self.config.token_whitelist_mints or \
                   r.token_b_mint not in self.config.token_whitelist_mints:
                    continue
            # Convert half-life from resampled periods to blocks
            hl_blocks = r.half_life * (self.config.coint_resample_secs / 0.4)
            if hl_blocks < self.config.min_half_life:
                continue
            lookback_blocks = self.config.lookback_window * self.config.signal_resample_secs / 0.4
            max_hl = lookback_blocks * self.config.max_half_life_ratio
            if hl_blocks > max_hl:
                continue

            # Convert pair to basket format
            mints = sorted([r.token_a_mint, r.token_b_mint])
            symbols = [token_symbol(m) for m in mints]
            # Convert scalar hedge_ratio to eigenvector: [1.0, -hr] for canonical order
            # If token_a_mint < token_b_mint, spread = log(a) - hr*log(b) → [1.0, -hr]
            # If reversed, flip signs
            if r.token_a_mint == mints[0]:
                hedge_ratios = [1.0, -r.hedge_ratio]
            else:
                hedge_ratios = [-r.hedge_ratio, 1.0]

            basket_key = make_basket_key(mints)
            if basket_key in self.baskets:
                # Don't overwrite scanner-loaded baskets — they have better
                # hedge ratios from 16k+ observations vs inline's ~50
                continue
            else:
                state = BasketState(
                    pair_key=basket_key,
                    basket_size=2,
                    mints=mints,
                    symbols=symbols,
                    hedge_ratios=hedge_ratios,
                    half_life=hl_blocks,
                    eg_p_value=r.eg_p_value,
                    cointegration_analyzed_at=int(r.analyzed_at),
                )
                state.init_buffers(self.config.lookback_window)
                self._restore_candles(state)
                self.baskets[basket_key] = state
                logger.info(f"Discovered pair: {'/'.join(symbols)} "
                            f"p={r.eg_p_value:.4f} hl={hl_blocks:.0f}blk hr={hedge_ratios}")

            for m in mints:
                self.monitored_mints.add(m)
            loaded += 1
        return loaded

    def process_prices(self, new_prices: Dict[str, float], timestamp: float) -> List[Signal]:
        """Process a batch of Jupiter prices and return any triggered signals."""
        # Periodic basket reload from scanner DB
        if self.scanner_db_path and time.time() - self.last_pair_reload > self.pair_reload_interval:
            self.load_baskets()

        if not new_prices:
            self.discovery.update_prices(self.token_prices, timestamp)
            new_pairs = self.discovery.maybe_run_scan()
            if new_pairs:
                self.load_discovered_pairs(new_pairs)
            return []

        # Update SOL/USD
        sol_price = new_prices.get(SOL_MINT)
        if sol_price and sol_price > 0:
            self.sol_usd_price = sol_price

        # Update token prices (Jupiter prices are clean, light outlier rejection)
        for mint, new_price in new_prices.items():
            old_price = self.token_prices.get(mint)
            if old_price and old_price > 0:
                change_pct = abs(new_price - old_price) / old_price
                if change_pct > 1.0:  # >100% jump: reject
                    logger.debug(f"Price outlier REJECTED for {mint[:8]}.. "
                                 f"${old_price:.6f} -> ${new_price:.6f} ({change_pct:.0%})")
                    continue
            self.token_prices[mint] = new_price

        # Feed prices to cointegration discovery
        self.discovery.update_prices(self.token_prices, timestamp)
        new_pairs = self.discovery.maybe_run_scan()
        if new_pairs:
            added = self.load_discovered_pairs(new_pairs)
            if added > 0:
                logger.info(f"Discovery added {added} pairs, now monitoring {len(self.baskets)} total")

        # Update each monitored basket and check for signals
        signals = []
        for basket in self.baskets.values():
            # Gather prices for all tokens in the basket
            prices = []
            any_new = False
            for mint in basket.mints:
                p = new_prices.get(mint)
                if p is not None:
                    any_new = True
                    prices.append(p)
                else:
                    # Use latest known price
                    p = self.token_prices.get(mint)
                    if p is None:
                        break
                    prices.append(p)

            if len(prices) != basket.basket_size:
                continue  # Missing price for at least one token
            if not any_new:
                continue  # No new prices for any token
            if any(p <= 0 for p in prices):
                continue

            signal = self._update_basket(basket, prices, 0, timestamp)
            if signal is not None:
                signals.append(signal)

        return signals

    def all_mints(self) -> Set[str]:
        """Return all mints that need pricing (monitored baskets + discovery tokens)."""
        mints = set(self.monitored_mints)
        # Include well-known tokens for cointegration discovery
        for mint in WELL_KNOWN_TOKENS:
            if mint not in STABLECOIN_MINTS:
                mints.add(mint)
        mints.add(SOL_MINT)
        return mints

    def _restore_candles(self, basket: BasketState):
        """Restore saved candles from DB into the basket's circular buffers."""
        if not self.db:
            return
        candles = self.db.load_candles(basket.pair_key, self.config.lookback_window)
        if not candles:
            return
        for ts, log_prices in candles:
            if len(log_prices) != basket.basket_size:
                continue  # Schema mismatch, skip
            for i, lp in enumerate(log_prices):
                basket.price_buffers[i].append(lp)
        # Set last_resample_time so the next candle flushes at the right time
        basket.last_resample_time = candles[-1][0]
        logger.info(f"Restored {len(candles)} candles for {basket.pair_key[:16]}.. "
                    f"(count={basket.price_buffers[0].count})")

    def _update_basket(self, basket: BasketState, prices: List[float],
                       slot: int, block_time: float) -> Optional[Signal]:
        """Update basket with resampled candle-close prices.

        Instead of appending every raw price (irregular intervals), we:
        1. Cache the latest prices as "pending"
        2. Every signal_resample_secs, flush the pending prices to the buffers
        3. Compute z-score only from the resampled buffers
        """
        now = block_time or time.time()

        # Always update pending (latest known prices for this basket)
        basket.pending_prices = list(prices)
        basket.last_update_slot = slot

        # Initialize resample timer on first observation
        if basket.last_resample_time == 0:
            basket.last_resample_time = now
            return None

        # Check if it's time to flush a new candle
        elapsed = now - basket.last_resample_time
        if elapsed < self.config.signal_resample_secs:
            # Not time yet — but still compute live z-score for display
            # AND check for exit/stop signals on every tick (don't wait for candle close)
            if basket.price_buffers[0].count >= 2:
                self._compute_zscore_live(basket)
                if basket.in_position:
                    signal_type = self._check_signal(basket)
                    if signal_type in (SignalType.EXIT, SignalType.STOP_LOSS):
                        feat = self._compute_rich_features(basket)
                        return Signal(
                            signal_type=signal_type,
                            pair_key=basket.pair_key,
                            basket_size=basket.basket_size,
                            mints=basket.mints,
                            symbols=basket.symbols,
                            hedge_ratios=basket.hedge_ratios,
                            zscore=basket.current_zscore,
                            spread=basket.current_spread,
                            spread_mean=basket.spread_mean,
                            spread_std=basket.spread_std,
                            timestamp=int(block_time) if block_time else int(time.time()),
                            slot=slot,
                            **feat,
                        )
            return None

        # Time to resample: append the latest prices as candle close
        log_prices = [np.log(p) for p in basket.pending_prices]
        for i, lp in enumerate(log_prices):
            basket.price_buffers[i].append(lp)
        basket.last_resample_time = now

        # Persist candle to DB for warmup avoidance on restart
        if self.db:
            self.db.save_candle(basket.pair_key, now, log_prices)

        # Need minimum observations for meaningful statistics
        if basket.price_buffers[0].count < 30:
            return None

        # Compute spread and z-score from resampled buffers
        # spread = sum(hedge_ratios[i] * log_prices[i]) for each time step
        arrays = [buf.get_array() for buf in basket.price_buffers]
        price_matrix = np.column_stack(arrays)  # (T, N)
        hr = np.array(basket.hedge_ratios)       # (N,)
        spread = price_matrix @ hr               # (T,)

        # Kalman-filter the latest spread observation
        raw_spread = float(spread[-1])
        if basket.kalman is not None:
            filtered_spread = basket.kalman.update(raw_spread)
        else:
            filtered_spread = raw_spread

        basket.current_spread = filtered_spread
        basket.spread_mean = float(np.mean(spread))
        basket.spread_std = float(np.std(spread))

        if basket.spread_std < 1e-12:
            basket.current_zscore = 0.0
            return None

        basket.current_zscore = (basket.current_spread - basket.spread_mean) / basket.spread_std

        # Guard against degenerate z-scores from near-zero std
        if abs(basket.current_zscore) > 100:
            basket.current_zscore = 0.0
            return None

        # Check thresholds (only at resample boundaries)
        signal_type = self._check_signal(basket)
        if signal_type is None:
            return None

        # Token whitelist: block ENTRY signals for non-whitelisted baskets
        # (EXIT/STOP_LOSS must still fire so existing positions can close)
        if signal_type in (SignalType.ENTRY_LONG, SignalType.ENTRY_SHORT):
            if self.config.token_whitelist_mints:
                if not all(m in self.config.token_whitelist_mints for m in basket.mints):
                    return None
            # Basket size filter
            if basket.basket_size > self.config.max_basket_size:
                return None
            # Per-direction filters
            if signal_type == SignalType.ENTRY_LONG:
                if self.config.long_exclude_mints:
                    if any(m in self.config.long_exclude_mints for m in basket.mints):
                        return None
                long_corr = self.config.long_min_correlation or self.config.min_correlation
                if long_corr > 0 and hasattr(basket, 'correlation') and basket.correlation is not None:
                    if basket.correlation < long_corr:
                        return None
            elif signal_type == SignalType.ENTRY_SHORT:
                if self.config.short_exclude_mints:
                    if any(m in self.config.short_exclude_mints for m in basket.mints):
                        return None
                short_corr = self.config.short_min_correlation or self.config.min_correlation
                if short_corr > 0 and hasattr(basket, 'correlation') and basket.correlation is not None:
                    if basket.correlation < short_corr:
                        return None

        feat = self._compute_rich_features(basket)
        return Signal(
            signal_type=signal_type,
            pair_key=basket.pair_key,
            basket_size=basket.basket_size,
            mints=basket.mints,
            symbols=basket.symbols,
            hedge_ratios=basket.hedge_ratios,
            zscore=basket.current_zscore,
            spread=basket.current_spread,
            spread_mean=basket.spread_mean,
            spread_std=basket.spread_std,
            timestamp=int(block_time) if block_time else int(time.time()),
            slot=slot,
            **feat,
        )

    def _compute_rich_features(self, basket: BasketState) -> dict:
        """Compute ML training features from basket state at signal time."""
        features = {
            'spread_velocity': 0.0,
            'zscore_velocity': 0.0,
            'spread_volatility': 0.0,
            'half_life_secs': basket.half_life * 0.4,  # blocks to seconds
            'eg_p_value': basket.eg_p_value,
            'buffer_count': basket.price_buffers[0].count if basket.price_buffers else 0,
            'price_momentum': [],
        }

        count = basket.price_buffers[0].count if basket.price_buffers else 0
        if count < 10:
            return features

        arrays = [buf.get_array() for buf in basket.price_buffers]
        price_matrix = np.column_stack(arrays)
        hr = np.array(basket.hedge_ratios)
        spread = price_matrix @ hr

        # Spread velocity: normalized slope over last 10 candles
        recent = spread[-10:]
        try:
            x = np.arange(len(recent), dtype=np.float64)
            slope = np.polyfit(x, recent, 1)[0]
            std = np.std(recent)
            features['spread_velocity'] = float(slope / std) if std > 1e-12 else 0.0
        except (np.linalg.LinAlgError, ValueError):
            pass

        # Z-score velocity: z-score change over last 5 candles
        if len(spread) >= 5 and basket.spread_std > 1e-12:
            z_now = (spread[-1] - basket.spread_mean) / basket.spread_std
            z_5ago = (spread[-5] - basket.spread_mean) / basket.spread_std
            features['zscore_velocity'] = float(z_now - z_5ago)

        # Spread volatility: std of spread differences
        if len(spread) >= 3:
            features['spread_volatility'] = float(np.std(np.diff(spread)))

        # Per-token price momentum: log return over last 10 candles
        momentum = []
        for arr in arrays:
            if len(arr) >= 10:
                momentum.append(float(arr[-1] - arr[-10]))  # already log prices
            else:
                momentum.append(0.0)
        features['price_momentum'] = momentum

        return features

    def _compute_zscore_live(self, basket: BasketState):
        """Compute a live z-score using buffer + pending (tick-level) prices.
        Updates basket.current_zscore and basket.current_spread.
        Exit/stop signals are checked against this live z-score between resamples.
        Uses Kalman filter_live() to denoise without committing state."""
        if any(p <= 0 for p in basket.pending_prices):
            return
        arrays = [buf.get_array() for buf in basket.price_buffers]
        if len(arrays[0]) < 2:
            return
        price_matrix = np.column_stack(arrays)
        hr = np.array(basket.hedge_ratios)
        spread = price_matrix @ hr
        mean = float(np.mean(spread))
        std = float(np.std(spread))
        if std < 1e-12:
            return
        # Current spread using pending (live) prices
        live_log_prices = np.array([np.log(p) for p in basket.pending_prices])
        raw_live_spread = float(live_log_prices @ hr)

        # Kalman-denoise the live spread (peek without committing state)
        if basket.kalman is not None and basket.kalman.initialized:
            live_spread = basket.kalman.filter_live(raw_live_spread)
        else:
            live_spread = raw_live_spread

        z = (live_spread - mean) / std
        if abs(z) > 100:
            return  # degenerate — don't display garbage
        basket.current_zscore = z
        basket.current_spread = live_spread

    def _get_recent_spread(self, basket: BasketState, n: int = 10) -> Optional[np.ndarray]:
        """Compute spread from last n buffer entries."""
        arrays = [buf.get_array() for buf in basket.price_buffers]
        if len(arrays[0]) < n:
            return None
        price_matrix = np.column_stack(arrays)[-n:]
        hr = np.array(basket.hedge_ratios)
        return price_matrix @ hr

    def _compute_exit_threshold(self, basket: BasketState) -> float:
        """Dynamic exit z-score threshold based on spread velocity and position age."""
        base = self.config.exit_zscore

        # Z-velocity from last 10 spread values
        recent = self._get_recent_spread(basket, n=10)
        if recent is not None and len(recent) >= 3:
            try:
                x = np.arange(len(recent), dtype=np.float64)
                coeffs = np.polyfit(x, recent, 1)
                slope = coeffs[0]
                spread_range = np.std(recent)

                if spread_range > 1e-12:
                    # Normalized velocity: positive = spread increasing
                    norm_velocity = slope / spread_range

                    # Determine if reverting toward mean or away
                    # For a long position (entered at -z), reversion means spread increasing
                    # For a short position (entered at +z), reversion means spread decreasing
                    entry_z = basket.position_entry_zscore
                    if entry_z < 0:
                        # Long: want spread to go up (positive velocity = reverting)
                        is_reverting = norm_velocity > 0.3
                        is_stalled = norm_velocity < -0.1
                    else:
                        # Short: want spread to go down (negative velocity = reverting)
                        is_reverting = norm_velocity < -0.3
                        is_stalled = norm_velocity > 0.1

                    if is_reverting:
                        base *= 0.6  # Hold longer — momentum in our favor
                    elif is_stalled:
                        base *= 1.5  # Exit sooner — adverse movement
            except (np.linalg.LinAlgError, ValueError):
                pass

        # Position age vs half-life: exponential pressure to exit after 2× HL
        if basket.position_entry_time > 0 and basket.half_life > 0:
            age_secs = time.time() - basket.position_entry_time
            hl_secs = basket.half_life * 0.4  # blocks to seconds
            if hl_secs > 0:
                age_ratio = age_secs / hl_secs
                if age_ratio > 2.0:
                    # Exponential ramp: 2×HL→1.0x, 3×HL→1.6x, 4×HL→2.5x, 6×HL→6.3x
                    base *= 2 ** ((age_ratio - 2.0) / 1.5)

        # Clamp — keep exit threshold tight so trades capture full reversion.
        # Execution costs ~5-10 bps; exiting at 40% reversion loses money.
        base = max(0.1, min(0.5, base))
        return base

    def _check_signal(self, basket: BasketState) -> Optional[SignalType]:
        z = basket.current_zscore

        if abs(z) > self.config.stop_loss_zscore:
            if basket.in_position:
                # Require 3 consecutive ticks above stop threshold to confirm
                basket.stop_loss_ticks += 1
                if basket.stop_loss_ticks >= 3:
                    basket.stop_loss_ticks = 0
                    return SignalType.STOP_LOSS
                return None
            return None
        else:
            basket.stop_loss_ticks = 0  # reset counter when z drops below

        long_entry_z = self.config.entry_zscore
        short_entry_z = self.config.short_entry_zscore if self.config.short_entry_zscore is not None else self.config.entry_zscore

        # Lower min-spread for pairs with direct pool (cheaper execution)
        min_spread = self.config.min_spread_bps
        try:
            from direct_swap import has_direct_pool
            from constants import SOL_MINT
            if any(has_direct_pool(m, SOL_MINT) for m in basket.mints if m != SOL_MINT):
                min_spread = getattr(self.config, 'direct_min_spread_bps', 8)
        except ImportError:
            pass

        if z < -long_entry_z:
            if not basket.in_position:
                if min_spread > 0:
                    spread_dev_bps = abs(z) * basket.spread_std * 10000
                    if spread_dev_bps < min_spread:
                        return None
                return SignalType.ENTRY_LONG
        elif z > short_entry_z:
            if not basket.in_position:
                if min_spread > 0:
                    spread_dev_bps = abs(z) * basket.spread_std * 10000
                    if spread_dev_bps < min_spread:
                        return None
                return SignalType.ENTRY_SHORT
        elif basket.in_position:
            cooldown_secs = self.config.entry_cooldown_slots / 2.5
            if (time.time() - basket.position_entry_time) >= cooldown_secs:
                # Exit if z is within exit band
                if abs(z) < self._compute_exit_threshold(basket):
                    return SignalType.EXIT
                # Exit if z crossed through the mean (overshot)
                # Short entered at +z, exit if z went negative (and vice versa)
                if basket.position_entry_zscore > 0 and z < 0:
                    return SignalType.EXIT
                if basket.position_entry_zscore < 0 and z > 0:
                    return SignalType.EXIT
        return None

    def get_basket_states(self) -> Dict[str, BasketState]:
        return self.baskets

    # Backward compat alias
    def get_pair_states(self) -> Dict[str, BasketState]:
        return self.baskets
