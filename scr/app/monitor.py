import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .models import Direction
from ..arb.optimizer import build_quantity_grid


LOGGER = logging.getLogger(__name__)


class MarketMonitor:
    def __init__(self, config, binance, uniswap, optimizer, store):
        self.config = config
        self.binance = binance
        self.uniswap = uniswap
        self.optimizer = optimizer
        self.store = store
        self.quantities = build_quantity_grid(
            config.strategy.min_quantity_eth, config.strategy.max_quantity_eth,
            config.strategy.quantity_steps, config.strategy.grid,
        )

    def sample_once(self):
        book = self.binance.get_order_book()
        block = self.uniswap.current_block()
        pool = self.uniswap.get_pool_state(block)
        fee_rate = self.binance.get_taker_fee_rate()
        pool_time = pool.block_timestamp or pool.observed_at
        market_age_ms = int(abs((book.received_at - pool_time).total_seconds()) * 1000)
        stale = market_age_ms > int(self.config.monitor.max_market_age_seconds * 1000)
        if self.config.strategy.search_mode == "adaptive":
            result = self.optimizer.optimize_adaptive(block, book, fee_rate)
        else:
            result = self.optimizer.optimize(self.quantities, block, book, fee_rate)
        observation_id = self.store.record_observation(
            pool, book, fee_rate, result, market_age_ms, stale,
            metadata={"last_update_id": book.last_update_id},
        )

        self._evaluate_due(observation_id, book, block, fee_rate, result.evaluated_at)
        if result.should_trade and not stale:
            self.store.schedule_paper_signals(
                observation_id, result.best, result.evaluated_at,
                self.config.monitor.latency_delays_ms,
            )
            # Evaluate zero-delay signals against the same observed state.
            self._evaluate_due(observation_id, book, block, fee_rate, result.evaluated_at)
        return observation_id, pool, book, result, stale

    def _evaluate_due(self, observation_id, book, block, fee_rate, evaluated_at):
        for signal in self.store.due_signals(evaluated_at):
            try:
                candidate = self.optimizer.evaluate(
                    Direction(signal["direction"]), Decimal(signal["quantity_eth"]),
                    block, book, fee_rate,
                )
                self.store.record_latency(signal, observation_id, evaluated_at, candidate)
            except Exception as exc:
                self.store.record_latency(signal, observation_id, evaluated_at, None, str(exc))

    def run(self, iterations: Optional[int] = None):
        count = 0
        while iterations is None or count < iterations:
            started = time.monotonic()
            try:
                yield self.sample_once()
            except KeyboardInterrupt:
                return
            except Exception:
                LOGGER.exception("monitor sample failed")
            count += 1
            remaining = self.config.monitor.poll_interval_seconds - (time.monotonic() - started)
            if remaining > 0 and (iterations is None or count < iterations):
                time.sleep(remaining)

