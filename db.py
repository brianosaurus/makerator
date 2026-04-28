"""
SQLite persistence for statalayer.
Own DB for positions/signals + read-only access to scanner's cointegration results.
"""

import json
import sqlite3
import logging
import time
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = 'statalayer.db'):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                pair_key TEXT NOT NULL,
                basket_size INTEGER NOT NULL DEFAULT 2,
                mints_json TEXT NOT NULL DEFAULT '[]',
                hedge_ratios_json TEXT NOT NULL DEFAULT '[]',
                zscore REAL NOT NULL,
                signal_type TEXT NOT NULL,
                spread REAL,
                spread_mean REAL,
                spread_std REAL,
                acted_on INTEGER NOT NULL DEFAULT 0,
                reason_not_acted TEXT,
                spread_velocity REAL,
                zscore_velocity REAL,
                spread_volatility REAL,
                half_life_secs REAL,
                eg_p_value REAL,
                buffer_count INTEGER,
                price_momentum_json TEXT,
                num_open_positions INTEGER,
                total_exposure_pct REAL,
                drawdown_pct REAL,
                portfolio_return REAL,
                recent_win_rate REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                pair_key TEXT NOT NULL,
                basket_size INTEGER NOT NULL DEFAULT 2,
                mints_json TEXT NOT NULL DEFAULT '[]',
                direction TEXT NOT NULL,
                hedge_ratios_json TEXT NOT NULL DEFAULT '[]',
                entry_time INTEGER NOT NULL,
                entry_slot INTEGER NOT NULL,
                entry_zscore REAL NOT NULL,
                entry_prices_json TEXT NOT NULL DEFAULT '[]',
                quantities_json TEXT NOT NULL DEFAULT '[]',
                quantities_raw_json TEXT NOT NULL DEFAULT '[]',
                entry_values_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                exit_time INTEGER,
                exit_slot INTEGER,
                exit_zscore REAL,
                exit_prices_json TEXT,
                realized_pnl REAL DEFAULT 0.0,
                is_paper INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                features_json TEXT
            );

            CREATE TABLE IF NOT EXISTS execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                leg TEXT NOT NULL,
                side TEXT NOT NULL,
                token_mint TEXT NOT NULL,
                amount_raw INTEGER NOT NULL,
                price REAL NOT NULL,
                dex TEXT,
                pool_address TEXT,
                signature TEXT,
                slot INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                slippage_bps REAL,
                fee_lamports INTEGER DEFAULT 0,
                is_paper INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (position_id) REFERENCES positions(id)
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                total_value REAL NOT NULL,
                num_open_positions INTEGER NOT NULL,
                total_exposure REAL NOT NULL,
                total_realized_pnl REAL NOT NULL,
                total_unrealized_pnl REAL NOT NULL,
                drawdown_pct REAL NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS config_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS discovered_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_key TEXT NOT NULL UNIQUE,
                basket_size INTEGER NOT NULL DEFAULT 2,
                mints_json TEXT NOT NULL DEFAULT '[]',
                symbols_json TEXT NOT NULL DEFAULT '[]',
                hedge_ratios_json TEXT NOT NULL DEFAULT '[]',
                half_life REAL NOT NULL,
                eg_p_value REAL NOT NULL,
                eg_test_statistic REAL NOT NULL,
                spread_mean REAL NOT NULL,
                spread_std REAL NOT NULL,
                num_observations INTEGER NOT NULL,
                analyzed_at REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS price_candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                basket_key TEXT NOT NULL,
                timestamp REAL NOT NULL,
                log_prices_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submit_timestamp REAL NOT NULL,
                slot INTEGER,
                signature TEXT NOT NULL,
                confirmation_slot INTEGER,
                confirmation_timestamp REAL
            );

            CREATE TABLE IF NOT EXISTS sol_balance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                action TEXT NOT NULL,
                sol_before REAL NOT NULL,
                sol_after REAL NOT NULL,
                expected_sol_after REAL,
                diff REAL,
                pair_key TEXT,
                timestamp INTEGER NOT NULL,
                FOREIGN KEY (position_id) REFERENCES positions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_sol_balance_pos ON sol_balance_log(position_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_sig ON transactions(signature);
            CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals(pair_key);
            CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(timestamp);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_positions_pair ON positions(pair_key);
            CREATE INDEX IF NOT EXISTS idx_exec_position ON execution_log(position_id);
            CREATE INDEX IF NOT EXISTS idx_candles_pair_time ON price_candles(basket_key, timestamp);

            CREATE TABLE IF NOT EXISTS exit_reconciliation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL,
                pair_key TEXT NOT NULL,
                sol_before REAL NOT NULL,
                slot_before INTEGER NOT NULL DEFAULT 0,
                expected_pnl REAL NOT NULL,
                signatures_json TEXT NOT NULL,
                sol_after REAL,
                slot_after INTEGER,
                finalized_at REAL,
                created_at REAL NOT NULL,
                FOREIGN KEY (position_id) REFERENCES positions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_exit_recon_pos ON exit_reconciliation(position_id);

            CREATE TABLE IF NOT EXISTS rl_experiences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_key TEXT NOT NULL,
                observation_json TEXT NOT NULL,
                action INTEGER NOT NULL,
                log_prob REAL NOT NULL,
                value REAL NOT NULL,
                context TEXT NOT NULL DEFAULT 'entry',
                reward REAL,
                done INTEGER NOT NULL DEFAULT 0,
                timestamp REAL NOT NULL
            );
        """)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Add columns to existing tables if missing."""
        # entry SOL tracking on positions
        for col, typ in [('entry_sol_before', 'REAL'),
                         ('entry_sol_after', 'REAL')]:
            try:
                self.conn.execute(
                    f"ALTER TABLE positions ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # already exists
        # RL processing flag on exit_reconciliation
        try:
            self.conn.execute(
                "ALTER TABLE exit_reconciliation ADD COLUMN rl_reward_assigned INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Rich ML features on signals
        for col, typ in [('spread_velocity', 'REAL'),
                         ('zscore_velocity', 'REAL'),
                         ('spread_volatility', 'REAL'),
                         ('half_life_secs', 'REAL'),
                         ('eg_p_value', 'REAL'),
                         ('buffer_count', 'INTEGER'),
                         ('price_momentum_json', 'TEXT'),
                         ('num_open_positions', 'INTEGER'),
                         ('total_exposure_pct', 'REAL'),
                         ('drawdown_pct', 'REAL'),
                         ('portfolio_return', 'REAL'),
                         ('recent_win_rate', 'REAL')]:
            try:
                self.conn.execute(
                    f"ALTER TABLE signals ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        # signal_id FK on positions
        try:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN signal_id INTEGER")
        except sqlite3.OperationalError:
            pass
        # ML features blob on positions
        try:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN features_json TEXT")
        except sqlite3.OperationalError:
            pass
        # SOL tracking on execution_log
        for col, typ in [('phase', 'TEXT DEFAULT "exit"'), ('sol_amount', 'REAL DEFAULT 0.0')]:
            try:
                self.conn.execute(
                    f"ALTER TABLE execution_log ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    # --- Signal methods ---

    def save_signal(self, signal, acted_on: bool, reason: str = '',
                    portfolio_ctx: dict = None) -> int:
        ctx = portfolio_ctx or {}
        cursor = self.conn.execute(
            """INSERT INTO signals
               (timestamp, slot, pair_key, basket_size, mints_json, hedge_ratios_json,
                zscore, signal_type, spread, spread_mean, spread_std,
                acted_on, reason_not_acted,
                spread_velocity, zscore_velocity, spread_volatility,
                half_life_secs, eg_p_value, buffer_count, price_momentum_json,
                num_open_positions, total_exposure_pct, drawdown_pct,
                portfolio_return, recent_win_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal.timestamp, signal.slot, signal.pair_key,
             signal.basket_size, json.dumps(signal.mints), json.dumps(signal.hedge_ratios),
             signal.zscore, signal.signal_type.value,
             signal.spread, signal.spread_mean, signal.spread_std,
             int(acted_on), reason,
             signal.spread_velocity, signal.zscore_velocity, signal.spread_volatility,
             signal.half_life_secs, signal.eg_p_value, signal.buffer_count,
             json.dumps(signal.price_momentum),
             ctx.get('num_open_positions'), ctx.get('total_exposure_pct'),
             ctx.get('drawdown_pct'), ctx.get('portfolio_return'),
             ctx.get('recent_win_rate')),
        )
        self.conn.commit()
        return cursor.lastrowid

    # --- Position methods ---

    def save_position(self, position, signal_id: int = None) -> int:
        features = json.dumps(position.features, default=str) if hasattr(position, 'features') and position.features else None
        cursor = self.conn.execute(
            """INSERT INTO positions
               (signal_id, pair_key, basket_size, mints_json, direction, hedge_ratios_json,
                entry_time, entry_slot, entry_zscore,
                entry_prices_json, quantities_json, quantities_raw_json, entry_values_json,
                status, is_paper, features_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, position.pair_key, position.basket_size, json.dumps(position.mints),
             position.direction, json.dumps(position.hedge_ratios),
             position.entry_time, position.entry_slot, position.entry_zscore,
             json.dumps(position.entry_prices), json.dumps(position.quantities),
             json.dumps(position.quantities_raw), json.dumps(position.entry_values),
             position.status.value, int(position.is_paper), features),
        )
        self.conn.commit()
        position.id = cursor.lastrowid
        return cursor.lastrowid

    def update_position(self, position) -> None:
        features = json.dumps(position.features, default=str) if hasattr(position, 'features') and position.features else None
        self.conn.execute(
            """UPDATE positions SET
               status = ?, exit_time = ?, exit_slot = ?, exit_zscore = ?,
               exit_prices_json = ?, realized_pnl = ?, features_json = ?,
               updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (position.status.value, position.exit_time, position.exit_slot,
             position.exit_zscore, json.dumps(position.exit_prices) if position.exit_prices else None,
             position.realized_pnl, features, position.id),
        )
        self.conn.commit()

    def get_open_positions(self) -> list:
        return self.conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time ASC"
        ).fetchall()

    def get_position_columns(self) -> list:
        cursor = self.conn.execute("PRAGMA table_info(positions)")
        return [row[1] for row in cursor.fetchall()]

    # --- Execution log ---

    def save_execution(self, position_id: int, leg: str, side: str,
                       token_mint: str, amount_raw: int, price: float,
                       dex: str, pool_address: str, signature: str,
                       slot: int, timestamp: int, slippage_bps: float,
                       fee_lamports: int, is_paper: bool,
                       phase: str = "exit", sol_amount: float = 0.0) -> None:
        self.conn.execute(
            """INSERT INTO execution_log
               (position_id, leg, side, token_mint, amount_raw, price,
                dex, pool_address, signature, slot, timestamp,
                slippage_bps, fee_lamports, is_paper, phase, sol_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (position_id, leg, side, token_mint, amount_raw, price,
             dex, pool_address, signature, slot, timestamp,
             slippage_bps, fee_lamports, int(is_paper), phase, sol_amount),
        )
        self.conn.commit()

    # --- Per-token edge estimation ---

    def get_per_token_edge_bps(self, min_trades: int = 5) -> Dict[str, float]:
        """Compute per-token edge in bps from closed positions.

        For each token, finds all closed positions containing that token,
        computes average (realized_pnl / entry_value) in bps.
        Only returns tokens with at least min_trades closed positions.
        """
        rows = self.conn.execute("""
            SELECT mints_json, entry_values_json, realized_pnl
            FROM positions
            WHERE status IN ('closed', 'stopped_out')
              AND realized_pnl IS NOT NULL
        """).fetchall()

        # Accumulate per-token: total edge bps weighted by entry value
        from collections import defaultdict
        token_pnl = defaultdict(float)     # mint -> sum of pnl attributed
        token_value = defaultdict(float)   # mint -> sum of entry value
        token_count = defaultdict(int)     # mint -> num positions

        for mints_json, values_json, pnl in rows:
            mints = json.loads(mints_json)
            values = json.loads(values_json)
            total_value = sum(values)
            if total_value <= 0:
                continue
            # Attribute PnL proportionally to each token's entry value
            for mint, val in zip(mints, values):
                if val <= 0:
                    continue
                share = val / total_value
                token_pnl[mint] += pnl * share
                token_value[mint] += val
                token_count[mint] += 1

        result = {}
        for mint in token_pnl:
            if token_count[mint] >= min_trades and token_value[mint] > 0:
                edge_bps = (token_pnl[mint] / token_value[mint]) * 10000
                result[mint] = edge_bps
        return result

    # --- Portfolio snapshots ---

    def save_snapshot(self, total_value: float, num_positions: int,
                      total_exposure: float, realized_pnl: float,
                      unrealized_pnl: float, drawdown_pct: float) -> None:
        self.conn.execute(
            """INSERT INTO portfolio_snapshots
               (timestamp, total_value, num_open_positions, total_exposure,
                total_realized_pnl, total_unrealized_pnl, drawdown_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(time.time()), total_value, num_positions, total_exposure,
             realized_pnl, unrealized_pnl, drawdown_pct),
        )
        self.conn.commit()

    # --- Config state (crash recovery) ---

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO config_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
               updated_at = CURRENT_TIMESTAMP""",
            (key, value),
        )
        self.conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM config_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    # --- Scanner DB reader (read-only) ---

    @staticmethod
    def read_cointegrated_baskets(scanner_db_path: str) -> List[Dict]:
        """Read cointegrated baskets from scanner's DB (read-only).
        Supports 2, 3, and 4-token baskets."""
        try:
            conn = sqlite3.connect(f"file:{scanner_db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            logger.error(f"Cannot open scanner DB: {scanner_db_path}")
            return []

        try:
            rows = conn.execute("""
                SELECT basket_key, basket_size, mints_json, symbols_json,
                       hedge_ratios_json, spread_mean, spread_std, half_life,
                       eg_p_value, eg_is_cointegrated, johansen_is_cointegrated,
                       num_observations, analyzed_at, correlation
                FROM cointegration_results
                WHERE (eg_is_cointegrated = 1 OR johansen_is_cointegrated = 1)
                ORDER BY CASE WHEN eg_p_value IS NOT NULL THEN eg_p_value ELSE 1.0 END ASC
            """).fetchall()

            baskets = []
            for row in rows:
                baskets.append({
                    'basket_key': row[0],
                    'basket_size': row[1],
                    'mints': json.loads(row[2]),
                    'symbols': json.loads(row[3]),
                    'hedge_ratios': json.loads(row[4]),
                    'spread_mean': row[5],
                    'spread_std': row[6],
                    'half_life': row[7],
                    'eg_p_value': row[8],
                    'eg_is_cointegrated': bool(row[9]) if row[9] is not None else False,
                    'johansen_is_cointegrated': bool(row[10]) if row[10] is not None else False,
                    'num_observations': row[11],
                    'analyzed_at': row[12],
                    'correlation': row[13],
                })
            return baskets
        except Exception as e:
            logger.error(f"Error reading scanner DB: {e}")
            return []
        finally:
            conn.close()

    # Backward compat alias
    @staticmethod
    def read_cointegrated_pairs(scanner_db_path: str) -> List[Dict]:
        return Database.read_cointegrated_baskets(scanner_db_path)

    # --- Discovered pairs persistence ---

    def save_discovered_pair(self, result) -> None:
        """UPSERT a discovered cointegration result."""
        from signals import make_pair_key
        pair_key = make_pair_key(result.token_a_mint, result.token_b_mint)
        mints = sorted([result.token_a_mint, result.token_b_mint])
        symbols = [result.token_a_symbol, result.token_b_symbol]
        # Sort symbols to match mint order
        if result.token_a_mint == mints[0]:
            sorted_symbols = [result.token_a_symbol, result.token_b_symbol]
        else:
            sorted_symbols = [result.token_b_symbol, result.token_a_symbol]
        hedge_ratios = [1.0, -result.hedge_ratio] if result.token_a_mint == mints[0] else [-result.hedge_ratio, 1.0]

        self.conn.execute("""
            INSERT INTO discovered_pairs
                (pair_key, basket_size, mints_json, symbols_json, hedge_ratios_json,
                 half_life, eg_p_value, eg_test_statistic,
                 spread_mean, spread_std, num_observations, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair_key) DO UPDATE SET
                hedge_ratios_json=excluded.hedge_ratios_json, half_life=excluded.half_life,
                eg_p_value=excluded.eg_p_value, eg_test_statistic=excluded.eg_test_statistic,
                spread_mean=excluded.spread_mean, spread_std=excluded.spread_std,
                num_observations=excluded.num_observations, analyzed_at=excluded.analyzed_at
        """, (pair_key, 2, json.dumps(mints), json.dumps(sorted_symbols),
              json.dumps(hedge_ratios),
              result.half_life, result.eg_p_value,
              result.eg_test_statistic, result.spread_mean, result.spread_std,
              result.num_observations, result.analyzed_at))
        try:
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def load_discovered_pairs(self) -> List[Dict]:
        """Load all discovered pairs from DB (for crash recovery)."""
        rows = self.conn.execute("""
            SELECT pair_key, basket_size, mints_json, symbols_json, hedge_ratios_json,
                   half_life, eg_p_value, eg_test_statistic,
                   spread_mean, spread_std, num_observations, analyzed_at
            FROM discovered_pairs
        """).fetchall()
        pairs = []
        for row in rows:
            mints = json.loads(row[2])
            symbols = json.loads(row[3])
            hedge_ratios = json.loads(row[4])
            # Convert back to CointResult-compatible dict for load_discovered_pairs
            # Inline discovery only produces 2-token pairs
            pairs.append({
                'pair_key': row[0],
                'token_a_mint': mints[0] if len(mints) > 0 else '',
                'token_b_mint': mints[1] if len(mints) > 1 else '',
                'token_a_symbol': symbols[0] if len(symbols) > 0 else '',
                'token_b_symbol': symbols[1] if len(symbols) > 1 else '',
                'hedge_ratio': abs(hedge_ratios[1]) if len(hedge_ratios) > 1 else 1.0,
                'half_life': row[5],
                'eg_p_value': row[6],
                'eg_test_statistic': row[7],
                'spread_mean': row[8],
                'spread_std': row[9],
                'num_observations': row[10],
                'analyzed_at': row[11],
            })
        return pairs

    def remove_discovered_pair(self, pair_key: str) -> None:
        self.conn.execute("DELETE FROM discovered_pairs WHERE pair_key = ?", (pair_key,))
        self.conn.commit()

    # --- Price candle persistence (warmup avoidance) ---

    _candle_count: Dict[str, int] = {}

    def save_candle(self, basket_key: str, timestamp: float,
                    log_prices: List[float]) -> None:
        self.conn.execute(
            "INSERT INTO price_candles (basket_key, timestamp, log_prices_json) "
            "VALUES (?, ?, ?)",
            (basket_key, timestamp, json.dumps(log_prices)),
        )
        self.conn.commit()
        # Auto-prune every 50 inserts per basket to prevent DB bloat
        self._candle_count[basket_key] = self._candle_count.get(basket_key, 0) + 1
        if self._candle_count[basket_key] >= 50:
            self.trim_candles(basket_key, keep=120)
            self._candle_count[basket_key] = 0

    def load_candles(self, basket_key: str, limit: int = 100) -> List[tuple]:
        """Load most recent candles for a basket, ordered oldest-first.
        Returns list of (timestamp, log_prices_list) tuples."""
        rows = self.conn.execute(
            "SELECT timestamp, log_prices_json FROM price_candles "
            "WHERE basket_key = ? ORDER BY timestamp DESC LIMIT ?",
            (basket_key, limit),
        ).fetchall()[::-1]  # reverse to oldest-first
        return [(row[0], json.loads(row[1])) for row in rows]

    def trim_candles(self, basket_key: str, keep: int = 100) -> None:
        """Delete old candles beyond the keep limit."""
        self.conn.execute(
            "DELETE FROM price_candles WHERE basket_key = ? AND id NOT IN "
            "(SELECT id FROM price_candles WHERE basket_key = ? ORDER BY timestamp DESC LIMIT ?)",
            (basket_key, basket_key, keep),
        )
        self.conn.commit()

    def get_stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        open_pos = self.conn.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()[0]
        closed = self.conn.execute("SELECT COUNT(*) FROM positions WHERE status != 'open'").fetchone()[0]
        total_pnl = self.conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE status != 'open'").fetchone()[0]
        signals = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        return {
            'total_positions': total,
            'open_positions': open_pos,
            'closed_positions': closed,
            'total_realized_pnl': total_pnl,
            'total_signals': signals,
        }

    # --- Transaction timing ---

    def save_transaction(self, submit_timestamp: float, slot: int,
                         signature: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO transactions (submit_timestamp, slot, signature) VALUES (?, ?, ?)",
            (submit_timestamp, slot, signature),
        )
        self.conn.commit()
        return cursor.lastrowid

    def confirm_transaction(self, signature: str, confirmation_slot: int,
                            confirmation_timestamp: float) -> None:
        self.conn.execute(
            "UPDATE transactions SET confirmation_slot = ?, confirmation_timestamp = ? WHERE signature = ?",
            (confirmation_slot, confirmation_timestamp, signature),
        )
        self.conn.commit()

    def save_sol_balance(self, position_id: int, action: str,
                         sol_before: float, sol_after: float,
                         expected_sol_after: float, pair_key: str) -> None:
        self.conn.execute(
            """INSERT INTO sol_balance_log
               (position_id, action, sol_before, sol_after, expected_sol_after,
                diff, pair_key, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (position_id, action, sol_before, sol_after, expected_sol_after,
             sol_after - expected_sol_after if expected_sol_after is not None else None,
             pair_key, int(time.time())),
        )
        self.conn.commit()

    # --- Exit reconciliation ---

    def save_exit_reconciliation(self, position_id: int, pair_key: str,
                                 sol_before: float, slot_before: int,
                                 expected_pnl: float,
                                 signatures: list) -> int:
        cursor = self.conn.execute(
            """INSERT INTO exit_reconciliation
               (position_id, pair_key, sol_before, slot_before, expected_pnl,
                signatures_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (position_id, pair_key, sol_before, slot_before, expected_pnl,
             json.dumps(signatures), time.time()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_position_entry_sol(self, position_id: int,
                                    sol_before: float, sol_after: float):
        """Store actual SOL balance before/after entry execution."""
        self.conn.execute(
            "UPDATE positions SET entry_sol_before = ?, entry_sol_after = ? WHERE id = ?",
            (sol_before, sol_after, position_id),
        )
        self.conn.commit()

    def get_unprocessed_reconciliations(self) -> list:
        """Return finalized exit reconciliations not yet fed to RL."""
        rows = self.conn.execute("""
            SELECT r.id, r.position_id, r.pair_key, r.sol_before, r.sol_after,
                   r.expected_pnl, r.finalized_at,
                   p.entry_sol_before, p.entry_sol_after,
                   p.entry_time, p.exit_time
            FROM exit_reconciliation r
            JOIN positions p ON p.id = r.position_id
            WHERE r.sol_after IS NOT NULL
              AND r.rl_reward_assigned = 0
        """).fetchall()
        return [{'rec_id': r[0], 'position_id': r[1], 'pair_key': r[2],
                 'exit_sol_before': r[3], 'exit_sol_after': r[4],
                 'expected_pnl': r[5], 'finalized_at': r[6],
                 'entry_sol_before': r[7], 'entry_sol_after': r[8],
                 'entry_time': r[9], 'exit_time': r[10]}
                for r in rows]

    def get_position_realized_pnl(self, position_id: int):
        """Return positions.realized_pnl (SOL-native) for a position, or None."""
        row = self.conn.execute(
            "SELECT realized_pnl FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        return None if row is None or row[0] is None else float(row[0])

    def mark_reconciliation_rl_processed(self, rec_id: int):
        """Mark a reconciliation record as processed by RL."""
        self.conn.execute(
            "UPDATE exit_reconciliation SET rl_reward_assigned = 1 WHERE id = ?",
            (rec_id,),
        )
        self.conn.commit()

    # --- RL experience persistence ---

    def save_rl_experience(self, pair_key: str, observation_json: str,
                           action: int, log_prob: float, value: float,
                           context: str = 'entry') -> None:
        """Save or update a pending RL experience for crash recovery."""
        self.conn.execute(
            """INSERT INTO rl_experiences
               (pair_key, observation_json, action, log_prob, value, context,
                done, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(id) DO UPDATE SET
                observation_json=excluded.observation_json,
                action=excluded.action, log_prob=excluded.log_prob,
                value=excluded.value, timestamp=excluded.timestamp""",
            (pair_key, observation_json, action, log_prob, value, context,
             time.time()),
        )
        self.conn.commit()

    def load_rl_experiences(self) -> List[Dict]:
        """Load pending (undone) RL experiences."""
        rows = self.conn.execute(
            "SELECT pair_key, observation_json, action, log_prob, value, context "
            "FROM rl_experiences WHERE done = 0"
        ).fetchall()
        return [{'pair_key': r[0], 'observation_json': r[1], 'action': r[2],
                 'log_prob': r[3], 'value': r[4], 'context': r[5]}
                for r in rows]

    def clear_rl_experiences(self) -> None:
        """Clear all RL experiences (after training)."""
        self.conn.execute("DELETE FROM rl_experiences")
        self.conn.commit()

    def close(self):
        self.conn.close()
