"""
Inline cointegration discovery for statalyzer.
Accumulates per-token price histories from live swap data,
periodically runs Engle-Granger tests to discover cointegrated pairs.
"""

import logging
import time
from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class PriceBuffer:
    """Parallel circular buffers for (timestamp, log_price) pairs."""

    def __init__(self, capacity: int):
        self.timestamps = np.empty(capacity, dtype=np.float64)
        self.log_prices = np.empty(capacity, dtype=np.float64)
        self.capacity = capacity
        self.write_idx = 0
        self.count = 0

    def append(self, timestamp: float, log_price: float):
        self.timestamps[self.write_idx] = timestamp
        self.log_prices[self.write_idx] = log_price
        self.write_idx = (self.write_idx + 1) % self.capacity
        self.count = min(self.count + 1, self.capacity)

    def get_arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        """Return (timestamps, log_prices) in chronological order."""
        if self.count == 0:
            empty = np.array([], dtype=np.float64)
            return empty, empty
        if self.count < self.capacity:
            return self.timestamps[:self.count].copy(), self.log_prices[:self.count].copy()
        return (
            np.concatenate([self.timestamps[self.write_idx:], self.timestamps[:self.write_idx]]),
            np.concatenate([self.log_prices[self.write_idx:], self.log_prices[:self.write_idx]]),
        )


@dataclass
class TokenPriceHistory:
    """Per-token price accumulator for cointegration discovery."""
    mint: str
    symbol: str
    buffer: PriceBuffer = field(repr=False)
    last_update: float = 0.0


@dataclass
class CointResult:
    """Result of a single pair cointegration test."""
    token_a_mint: str
    token_b_mint: str
    token_a_symbol: str
    token_b_symbol: str
    hedge_ratio: float
    half_life: float  # in resampled periods
    eg_p_value: float
    eg_test_statistic: float
    eg_is_cointegrated: bool
    spread_mean: float
    spread_std: float
    num_observations: int
    analyzed_at: float


class CointegrationDiscovery:
    """Discovers cointegrated pairs from live price streams."""

    def __init__(self, config, well_known_tokens: dict, stablecoin_mints: set):
        self.config = config
        # Track non-stablecoin well-known tokens only
        self.trackable_mints: Dict[str, str] = {}  # mint -> symbol
        for mint, info in well_known_tokens.items():
            if mint not in stablecoin_mints:
                self.trackable_mints[mint] = info['symbol']

        self.token_histories: Dict[str, TokenPriceHistory] = {}
        self.discovered_pairs: Dict[str, CointResult] = {}  # pair_key -> result
        self.fail_counts: Dict[str, int] = {}  # pair_key -> consecutive failures
        self.last_scan_time: float = 0.0
        self.start_time: float = time.time()

        # Lazy-loaded statsmodels
        self._sm_loaded = False
        self._OLS = None
        self._add_constant = None
        self._adfuller = None

        logger.info(f"Cointegration discovery initialized: tracking {len(self.trackable_mints)} tokens, "
                    f"C({len(self.trackable_mints)},2)={len(self.trackable_mints)*(len(self.trackable_mints)-1)//2} candidate pairs")

    def _load_statsmodels(self):
        """Lazy import to avoid slow startup."""
        if self._sm_loaded:
            return
        from statsmodels.regression.linear_model import OLS
        from statsmodels.tools import add_constant
        from statsmodels.tsa.stattools import adfuller
        self._OLS = OLS
        self._add_constant = add_constant
        self._adfuller = adfuller
        self._sm_loaded = True

    def update_prices(self, token_prices: Dict[str, float], block_time: float):
        """Called every block. Accumulate prices for trackable tokens."""
        now = block_time if block_time > 0 else time.time()
        for mint, symbol in self.trackable_mints.items():
            price = token_prices.get(mint)
            if not price or price <= 0:
                continue
            hist = self.token_histories.get(mint)
            if not hist:
                hist = TokenPriceHistory(
                    mint=mint,
                    symbol=symbol,
                    buffer=PriceBuffer(self.config.coint_history_capacity),
                )
                self.token_histories[mint] = hist
            hist.buffer.append(now, np.log(price))
            hist.last_update = now

    def maybe_run_scan(self) -> List[CointResult]:
        """Called every block. Only actually scans every coint_scan_interval seconds."""
        now = time.time()
        # Wait for warmup
        if now - self.start_time < self.config.coint_warmup_minutes * 60:
            return []
        # Rate limit
        if now - self.last_scan_time < self.config.coint_scan_interval:
            return []
        self.last_scan_time = now
        return self._run_full_scan()

    def _resample(self, timestamps: np.ndarray, log_prices: np.ndarray
                  ) -> Tuple[np.ndarray, np.ndarray]:
        """Resample raw block-level data to fixed time bins using median.
        Returns (bin_times, median_log_prices)."""
        if len(timestamps) < 2:
            return np.array([]), np.array([])

        bin_size = self.config.coint_resample_secs
        t_start = timestamps[0]
        t_end = timestamps[-1]
        n_bins = int((t_end - t_start) / bin_size) + 1
        if n_bins < 2:
            return np.array([]), np.array([])

        # Assign each observation to a bin
        bin_indices = ((timestamps - t_start) / bin_size).astype(int)
        bin_indices = np.clip(bin_indices, 0, n_bins - 1)

        # Compute median per bin
        bin_times = []
        bin_prices = []
        for b in range(n_bins):
            mask = bin_indices == b
            if mask.any():
                bin_times.append(t_start + b * bin_size)
                bin_prices.append(np.median(log_prices[mask]))

        return np.array(bin_times), np.array(bin_prices)

    def _align_series(self, times_a: np.ndarray, prices_a: np.ndarray,
                      times_b: np.ndarray, prices_b: np.ndarray
                      ) -> Tuple[np.ndarray, np.ndarray]:
        """Inner-join two resampled series on shared time bins."""
        # Both use same bin_size so times should align on multiples
        set_b = set(times_b)
        mask_a = np.array([t in set_b for t in times_a])
        if not mask_a.any():
            return np.array([]), np.array([])

        set_a = set(times_a[mask_a])
        mask_b = np.array([t in set_a for t in times_b])

        return prices_a[mask_a], prices_b[mask_b]

    def _engle_granger(self, log_a: np.ndarray, log_b: np.ndarray
                       ) -> Tuple[float, float, float, np.ndarray]:
        """Engle-Granger two-step cointegration test.
        Returns: (adf_stat, p_value, hedge_ratio, spread)"""
        # Reject constant series — OLS needs variance in both regressand and regressor
        if np.ptp(log_a) == 0 or np.ptp(log_b) == 0:
            raise ValueError("constant series")

        X = self._add_constant(log_b)
        if X.ndim == 1 or X.shape[1] < 2:
            raise ValueError("add_constant produced no regressor column")
        model = self._OLS(log_a, X).fit()
        hedge_ratio = model.params[1]

        residuals = model.resid
        adf_result = self._adfuller(residuals, maxlag=None, autolag='AIC')
        adf_stat = adf_result[0]
        p_value = adf_result[1]

        spread = log_a - hedge_ratio * log_b
        return adf_stat, p_value, hedge_ratio, spread

    def _half_life(self, spread: np.ndarray) -> float:
        """AR(1) half-life of mean reversion.
        Returns half-life in resampled periods, or inf if not mean-reverting."""
        if len(spread) < 10:
            return float('inf')
        try:
            lag = spread[:-1]
            diff = np.diff(spread)
            X = self._add_constant(lag)
            model = self._OLS(diff, X).fit()
            phi = model.params[1]
            ar_coeff = 1.0 + phi
            if ar_coeff <= 0 or ar_coeff >= 1:
                return float('inf')
            half_life = -np.log(2) / np.log(ar_coeff)
            return half_life if half_life > 0 else float('inf')
        except Exception:
            return float('inf')

    def _run_full_scan(self) -> List[CointResult]:
        """Test all candidate pairs for cointegration."""
        self._load_statsmodels()

        # Get tokens with sufficient resampled data
        resampled: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        for mint, hist in self.token_histories.items():
            ts, lp = hist.buffer.get_arrays()
            if len(ts) < 100:
                continue
            bin_times, bin_prices = self._resample(ts, lp)
            if len(bin_times) >= self.config.coint_min_observations:
                resampled[mint] = (bin_times, bin_prices)

        if len(resampled) < 2:
            logger.info(f"Discovery scan: {len(resampled)} tokens with sufficient data, need ≥2")
            return []

        # Test all pairs
        results = []
        tested = 0
        cointegrated = 0
        now = time.time()

        for mint_a, mint_b in combinations(resampled.keys(), 2):
            times_a, prices_a = resampled[mint_a]
            times_b, prices_b = resampled[mint_b]

            aligned_a, aligned_b = self._align_series(times_a, prices_a, times_b, prices_b)
            if len(aligned_a) < self.config.coint_min_observations:
                continue

            tested += 1
            try:
                adf_stat, p_value, hedge_ratio, spread = self._engle_granger(aligned_a, aligned_b)
            except Exception as e:
                logger.debug(f"EG test failed for {mint_a[:8]}../{mint_b[:8]}..: {e}")
                continue

            is_coint = p_value < self.config.coint_p_threshold
            hl = self._half_life(spread) if is_coint else float('inf')

            # Filter: must be cointegrated with finite half-life
            pair_key = f"{min(mint_a,mint_b)}:{max(mint_a,mint_b)}"
            if is_coint and hl != float('inf') and hl > 0:
                cointegrated += 1
                sym_a = self.trackable_mints.get(mint_a, mint_a[:8])
                sym_b = self.trackable_mints.get(mint_b, mint_b[:8])
                result = CointResult(
                    token_a_mint=mint_a,
                    token_b_mint=mint_b,
                    token_a_symbol=sym_a,
                    token_b_symbol=sym_b,
                    hedge_ratio=hedge_ratio,
                    half_life=hl,
                    eg_p_value=p_value,
                    eg_test_statistic=adf_stat,
                    eg_is_cointegrated=True,
                    spread_mean=float(np.mean(spread)),
                    spread_std=float(np.std(spread)),
                    num_observations=len(aligned_a),
                    analyzed_at=now,
                )
                results.append(result)
                self.discovered_pairs[pair_key] = result
                self.fail_counts.pop(pair_key, None)
            else:
                # Track consecutive failures for pair removal
                self.fail_counts[pair_key] = self.fail_counts.get(pair_key, 0) + 1

        # Remove pairs that failed 3+ consecutive times
        removed = []
        for pair_key, count in list(self.fail_counts.items()):
            if count >= 3 and pair_key in self.discovered_pairs:
                del self.discovered_pairs[pair_key]
                removed.append(pair_key)
                del self.fail_counts[pair_key]

        logger.info(f"Discovery scan: {len(resampled)} tokens, {tested} pairs tested, "
                    f"{cointegrated} cointegrated, {len(removed)} removed")

        return results
