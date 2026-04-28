"""
Configuration for statalayer statistical arbitrage bot.
"""

import os
import logging
from dataclasses import dataclass
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

try:
    load_dotenv()
except Exception:
    pass


@dataclass
class Config:
    # gRPC
    rpc_url: str = os.getenv('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')
    grpc_endpoint: str = os.getenv('GRPC_ENDPOINT', 'api.mainnet-beta.solana.com:443')
    grpc_token: str = os.getenv('GRPC_TOKEN', '')

    # Wallet (for live trading)
    wallet_keypair_path: str = os.getenv('WALLET_KEYPAIR_PATH', '')

    # Scanner DB (read cointegrated pairs from here)
    scanner_db_path: str = os.getenv('SCANNER_DB_PATH', '../arbitrage_tracker/arb_tracker.db')

    # Signal thresholds
    entry_zscore: float = float(os.getenv('ENTRY_ZSCORE', '2.5'))
    exit_zscore: float = float(os.getenv('EXIT_ZSCORE', '0.5'))
    stop_loss_zscore: float = float(os.getenv('STOP_LOSS_ZSCORE', '4.0'))
    max_entry_zscore: float = float(os.getenv('MAX_ENTRY_ZSCORE', '3.0'))
    min_spread_bps: float = float(os.getenv('MIN_SPREAD_BPS', '0'))  # min abs spread deviation in bps to enter
    max_basket_size: int = int(os.getenv('MAX_BASKET_SIZE', '99'))  # max tokens per basket (2=pairs only)
    regime_caution_threshold: float = float(os.getenv('REGIME_CAUTION', '0.4'))
    regime_danger_threshold: float = float(os.getenv('REGIME_DANGER', '0.7'))
    regime_ema_alpha: float = float(os.getenv('REGIME_EMA_ALPHA', '0.1'))
    regime_caution_size_mult: float = float(os.getenv('REGIME_CAUTION_SIZE', '0.5'))
    regime_caution_entry_z_mult: float = float(os.getenv('REGIME_CAUTION_Z', '1.3'))
    allowed_direction: str = os.getenv('ALLOWED_DIRECTION', 'both')  # both, long, short

    # Position sizing
    sizing_method: str = os.getenv('SIZING_METHOD', 'fixed_fraction')
    fixed_fraction: float = float(os.getenv('FIXED_FRACTION', '0.05'))
    max_position_usd: float = float(os.getenv('MAX_POSITION_USD', '1000'))
    max_exposure_ratio: float = float(os.getenv('MAX_EXPOSURE_RATIO', '1.0'))
    max_positions: int = int(os.getenv('MAX_POSITIONS', '10'))
    max_positions_per_hour: int = int(os.getenv('MAX_POSITIONS_PER_HOUR', '5'))

    # Risk
    max_drawdown_pct: float = float(os.getenv('MAX_DRAWDOWN_PCT', '0.10'))
    max_position_loss_pct: float = float(os.getenv('MAX_POSITION_LOSS_PCT', '0.15'))
    max_position_age_half_lives: float = float(os.getenv('MAX_POSITION_AGE_HALF_LIVES', '5.0'))
    pair_staleness_hours: int = int(os.getenv('PAIR_STALENESS_HOURS', '24'))
    min_half_life: float = float(os.getenv('MIN_HALF_LIFE', '200'))
    max_half_life_ratio: float = float(os.getenv('MAX_HALF_LIFE_RATIO', '0.5'))
    max_half_life_secs: float = float(os.getenv('MAX_HALF_LIFE_SECS', '1800'))  # 30min default
    max_positions_per_token: int = int(os.getenv('MAX_POSITIONS_PER_TOKEN', '3'))
    min_correlation: float = float(os.getenv('MIN_CORRELATION', '0.0'))  # 0 disables the gate

    # Execution
    slippage_bps: int = int(os.getenv('SLIPPAGE_BPS', '50'))
    priority_fee_lamports: int = int(os.getenv('PRIORITY_FEE', '10000'))
    use_lunar_lander: bool = os.getenv('USE_LUNAR_LANDER', 'false').lower() == 'true'
    lunar_lander_tip_lamports: int = int(os.getenv('LUNAR_LANDER_TIP', '1000000'))  # 0.001 SOL minimum
    lunar_lander_endpoint: str = os.getenv('LUNAR_LANDER_ENDPOINT', 'http://fra.lunar-lander.hellomoon.io')
    lunar_lander_api_key: str = os.getenv('LUNAR_LANDER_API_KEY', '')

    # Paper realism (make paper trading mirror live)
    paper_leg_failure_pct: float = float(os.getenv('PAPER_LEG_FAILURE_PCT', '0.07'))  # 7% chance per leg fails
    paper_latency_mean_s: float = float(os.getenv('PAPER_LATENCY_MEAN_S', '15'))     # mean execution latency
    paper_latency_std_s: float = float(os.getenv('PAPER_LATENCY_STD_S', '8'))        # stddev of latency
    paper_qty_jitter_pct: float = float(os.getenv('PAPER_QTY_JITTER_PCT', '0.01'))   # 1% fill quantity jitter

    # Price feed
    price_poll_interval: float = float(os.getenv('PRICE_POLL_INTERVAL', '6'))

    # Mode
    paper_trade: bool = os.getenv('PAPER_TRADE', 'true').lower() == 'true'
    lookback_window: int = int(os.getenv('LOOKBACK_WINDOW', '100'))
    signal_resample_secs: float = float(os.getenv('SIGNAL_RESAMPLE_SECS', '300'))  # 5-min candles
    entry_cooldown_slots: int = int(os.getenv('ENTRY_COOLDOWN_SLOTS', '750'))
    initial_capital: float = float(os.getenv('INITIAL_CAPITAL', '1000'))

    # Inline cointegration discovery
    coint_scan_interval: float = float(os.getenv('COINT_SCAN_INTERVAL', '300'))
    coint_resample_secs: float = float(os.getenv('COINT_RESAMPLE_SECS', '30'))
    coint_min_observations: int = int(os.getenv('COINT_MIN_OBSERVATIONS', '50'))
    coint_p_threshold: float = float(os.getenv('COINT_P_THRESHOLD', '0.05'))
    coint_history_capacity: int = int(os.getenv('COINT_HISTORY_CAPACITY', '10000'))
    coint_warmup_minutes: float = float(os.getenv('COINT_WARMUP_MINUTES', '30'))
    use_scanner_db: bool = os.getenv('USE_SCANNER_DB', 'true').lower() == 'true'

    # Token whitelist: if set, only trade baskets where ALL mints are in this set
    token_whitelist_mints: set = None  # populated from --token-whitelist CLI arg

    # Per-direction overrides (None = use the shared value above)
    short_entry_zscore: float = None
    short_max_entry_zscore: float = None
    short_min_correlation: float = None
    long_min_correlation: float = None
    long_exclude_mints: set = None  # mints to exclude from long entries only
    short_exclude_mints: set = None  # mints to exclude from short entries only
    blocked_hours_utc: set = None  # UTC hours to pause entries (e.g. {14, 15, 20})

    # Direct swap pair overrides (unified with main gate — wallet data shows no cost advantage)
    direct_min_spread_bps: float = 15.0
    direct_max_entry_zscore: float = 20.0
    direct_short_max_entry_zscore: float = 20.0

    # Per-trade absolute SOL stop-loss (0 = disabled)
    max_loss_sol: float = float(os.getenv('MAX_LOSS_SOL', '0'))

    # Reinforcement learning
    rl_model_path: str = os.getenv('RL_MODEL_PATH', 'rl_model')
    rl_hidden_dim: int = int(os.getenv('RL_HIDDEN_DIM', '64'))
    rl_learning_rate: float = float(os.getenv('RL_LEARNING_RATE', '0.0003'))

    # ─── Makerator-specific (Phase 1+) ───────────────────────────────
    # Capital target — locked at 10 SOL per Phase 0 decisions.
    target_capital_sol: float = float(os.getenv('TARGET_CAPITAL_SOL', '10.0'))

    # Maker DB (fresh, not shared with statalyzer).
    makerator_db: str = os.getenv('MAKERATOR_DB', 'makerator.db')

    # Quoting: spread vs fair value at which to place passive orders.
    quote_spread_bps: float = float(os.getenv('QUOTE_SPREAD_BPS', '5'))

    # Order management: how long an order lives before TTL cancel.
    order_ttl_seconds: float = float(os.getenv('ORDER_TTL_SECONDS', '60'))

    # Requote: cancel-and-replace when fair value moves more than this.
    requote_threshold_bps: float = float(os.getenv('REQUOTE_THRESHOLD_BPS', '2'))

    # Per-pair order book depth limit.
    max_orders_per_pair: int = int(os.getenv('MAX_ORDERS_PER_PAIR', '2'))

    # Inventory skew gate — abort new quotes if any LST is more than this
    # far from its target weight (in SOL terms).
    max_inventory_skew_sol: float = float(os.getenv('MAX_INVENTORY_SKEW_SOL', '2.0'))
