import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import Candidate, OptimizationResult, OrderBook, PendingScenario, PendingSwap, PoolState


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError("cannot serialize {}".format(type(value).__name__))


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_timestamp TEXT,
  book_received_at TEXT NOT NULL,
  market_age_ms INTEGER NOT NULL,
  binance_best_bid TEXT NOT NULL,
  binance_best_ask TEXT NOT NULL,
  uniswap_price TEXT NOT NULL,
  binance_taker_fee TEXT NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observation_id INTEGER NOT NULL REFERENCES observations(id),
  direction TEXT NOT NULL,
  quantity_eth TEXT NOT NULL,
  gross_buy_usdc TEXT NOT NULL,
  gross_sell_usdc TEXT NOT NULL,
  exchange_fee_usdc TEXT NOT NULL,
  gas_cost_usdc TEXT NOT NULL,
  gas_cost_eth TEXT NOT NULL,
  rebalancing_cost_usdc TEXT NOT NULL,
  net_profit_usdc TEXT NOT NULL,
  profit_bps TEXT NOT NULL,
  feasible INTEGER NOT NULL,
  rejection_reason TEXT,
  selected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paper_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observation_id INTEGER NOT NULL REFERENCES observations(id),
  created_at TEXT NOT NULL,
  due_at TEXT NOT NULL,
  delay_ms INTEGER NOT NULL,
  direction TEXT NOT NULL,
  quantity_eth TEXT NOT NULL,
  expected_profit_usdc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS latency_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER NOT NULL UNIQUE REFERENCES paper_signals(id),
  evaluated_observation_id INTEGER REFERENCES observations(id),
  evaluated_at TEXT NOT NULL,
  actual_delay_ms INTEGER NOT NULL,
  repriced_profit_usdc TEXT,
  repriced_profit_bps TEXT,
  survived INTEGER NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  observation_id INTEGER NOT NULL REFERENCES observations(id),
  executed_at TEXT NOT NULL,
  direction TEXT NOT NULL,
  quantity_eth TEXT NOT NULL,
  estimated_profit_usdc TEXT NOT NULL,
  cumulative_pnl_usdc TEXT NOT NULL,
  inventory_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_transactions (
  tx_hash TEXT PRIMARY KEY,
  observed_at TEXT NOT NULL,
  router TEXT NOT NULL,
  selector TEXT NOT NULL,
  decoded INTEGER NOT NULL,
  swaps_json TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS pending_scenarios (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tx_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  baseline_price TEXT NOT NULL,
  conditional_price TEXT NOT NULL,
  price_change_bps TEXT NOT NULL,
  valid INTEGER NOT NULL,
  reason TEXT,
  limitations_json TEXT NOT NULL,
  candidate_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_observation ON candidates(observation_id);
CREATE INDEX IF NOT EXISTS idx_signals_due ON paper_signals(status, due_at);
CREATE INDEX IF NOT EXISTS idx_paper_trades_session ON paper_trades(session_id, id);
"""


class ResearchStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def record_observation(self, pool: PoolState, book: OrderBook, fee_rate: Decimal,
                           result: OptimizationResult, market_age_ms: int, stale: bool = False,
                           metadata: Optional[Dict[str, Any]] = None) -> int:
        cursor = self.connection.execute(
            """INSERT INTO observations (
              observed_at, block_number, block_timestamp, book_received_at, market_age_ms,
              binance_best_bid, binance_best_ask, uniswap_price, binance_taker_fee, stale, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.evaluated_at.isoformat(), pool.block_number,
                pool.block_timestamp.isoformat() if pool.block_timestamp else None,
                book.received_at.isoformat(), market_age_ms, str(book.best_bid), str(book.best_ask),
                str(pool.price_usdc_per_weth), str(fee_rate), int(stale),
                json.dumps(metadata or {}, default=_json_default),
            ),
        )
        observation_id = int(cursor.lastrowid)
        for candidate in result.candidates:
            self.connection.execute(
                """INSERT INTO candidates (
                  observation_id, direction, quantity_eth, gross_buy_usdc, gross_sell_usdc,
                  exchange_fee_usdc, gas_cost_usdc, gas_cost_eth, rebalancing_cost_usdc,
                  net_profit_usdc, profit_bps, feasible, rejection_reason, selected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation_id, candidate.direction.value, str(candidate.quantity_eth),
                    str(candidate.gross_buy_usdc), str(candidate.gross_sell_usdc),
                    str(candidate.exchange_fee_usdc), str(candidate.gas_cost_usdc),
                    str(candidate.gas_cost_eth), str(candidate.rebalancing_cost_usdc),
                    str(candidate.net_profit_usdc), str(candidate.profit_bps), int(candidate.feasible),
                    candidate.rejection_reason, int(candidate == result.best),
                ),
            )
        self.connection.commit()
        return observation_id

    def schedule_paper_signals(self, observation_id: int, candidate: Candidate,
                               created_at: datetime, delays_ms: Iterable[int]) -> None:
        for delay in delays_ms:
            due = created_at + timedelta(milliseconds=int(delay))
            self.connection.execute(
                """INSERT INTO paper_signals (
                   observation_id, created_at, due_at, delay_ms, direction, quantity_eth,
                   expected_profit_usdc, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (observation_id, created_at.isoformat(), due.isoformat(), int(delay),
                 candidate.direction.value, str(candidate.quantity_eth), str(candidate.net_profit_usdc)),
            )
        self.connection.commit()

    def due_signals(self, now: datetime) -> List[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM paper_signals WHERE status='pending' AND due_at <= ? ORDER BY due_at",
            (now.isoformat(),),
        ))

    def record_latency(self, signal, observation_id: int, evaluated_at: datetime,
                       candidate: Optional[Candidate], error: Optional[str] = None) -> None:
        created = datetime.fromisoformat(signal["created_at"])
        actual_delay = int((evaluated_at - created).total_seconds() * 1000)
        survived = bool(candidate and candidate.feasible and candidate.net_profit_usdc > 0)
        self.connection.execute(
            """INSERT INTO latency_evaluations (
              signal_id, evaluated_observation_id, evaluated_at, actual_delay_ms,
              repriced_profit_usdc, repriced_profit_bps, survived, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal["id"], observation_id, evaluated_at.isoformat(), actual_delay,
             str(candidate.net_profit_usdc) if candidate else None,
             str(candidate.profit_bps) if candidate else None, int(survived), error),
        )
        self.connection.execute("UPDATE paper_signals SET status='evaluated' WHERE id=?", (signal["id"],))
        self.connection.commit()

    def record_paper_trade(self, session_id: str, observation_id: int,
                           candidate: Candidate, cumulative_pnl: Decimal,
                           inventory: Dict[str, Any]) -> int:
        cursor = self.connection.execute(
            """INSERT INTO paper_trades (
              session_id, observation_id, executed_at, direction, quantity_eth,
              estimated_profit_usdc, cumulative_pnl_usdc, inventory_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, observation_id, datetime.now(timezone.utc).isoformat(),
             candidate.direction.value, str(candidate.quantity_eth),
             str(candidate.net_profit_usdc), str(cumulative_pnl),
             json.dumps(inventory, default=_json_default)),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_pending(self, tx: Dict[str, Any], swaps: List[PendingSwap], error: Optional[str] = None) -> None:
        tx_hash = tx.get("hash", "")
        observed_at = swaps[0].observed_at if swaps else datetime.now(timezone.utc)
        self.connection.execute(
            """INSERT OR REPLACE INTO pending_transactions (
              tx_hash, observed_at, router, selector, decoded, swaps_json, raw_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tx_hash, observed_at.isoformat(), tx.get("to") or "", (tx.get("input") or "")[:10],
             int(bool(swaps)), json.dumps([swap.__dict__ for swap in swaps], default=_json_default),
             json.dumps(tx, default=_json_default), error),
        )
        self.connection.commit()

    def record_scenario(self, scenario: PendingScenario) -> None:
        self.connection.execute(
            """INSERT INTO pending_scenarios (
              tx_hash, created_at, baseline_price, conditional_price, price_change_bps,
              valid, reason, limitations_json, candidate_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (scenario.tx_hash, datetime.now(timezone.utc).isoformat(), str(scenario.baseline_price),
             str(scenario.conditional_price), str(scenario.price_change_bps), int(scenario.valid),
             scenario.reason, json.dumps(list(scenario.limitations)),
             json.dumps(scenario.conditional_best.to_dict(), default=_json_default)
             if scenario.conditional_best else None),
        )
        self.connection.commit()

    def summary(self) -> Dict[str, Any]:
        def scalar(query):
            return self.connection.execute(query).fetchone()[0]
        return {
            "observations": scalar("SELECT COUNT(*) FROM observations"),
            "profitable_observations": scalar("SELECT COUNT(DISTINCT observation_id) FROM candidates WHERE selected=1"),
            "paper_signals": scalar("SELECT COUNT(*) FROM paper_signals"),
            "paper_trades": scalar("SELECT COUNT(*) FROM paper_trades"),
            "latency_evaluations": scalar("SELECT COUNT(*) FROM latency_evaluations"),
            "pending_transactions": scalar("SELECT COUNT(*) FROM pending_transactions"),
            "pending_scenarios": scalar("SELECT COUNT(*) FROM pending_scenarios"),
        }

