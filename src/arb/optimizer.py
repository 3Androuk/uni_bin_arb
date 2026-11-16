from decimal import Decimal, ROUND_DOWN
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List

from ..app.config import StrategyConfig
from ..data.binance import calculate_buy_cost, calculate_sell_proceeds
from ..app.models import (
    Candidate, Direction, Inventory, OptimizationResult, OrderBook, no_trade_candidate,
)


ONE = Decimal("1")


def build_quantity_grid(minimum: Decimal, maximum: Decimal, steps: int, kind: str = "geometric") -> List[Decimal]:
    minimum, maximum = Decimal(str(minimum)), Decimal(str(maximum))
    if minimum <= 0 or maximum < minimum or steps < 1:
        raise ValueError("invalid quantity grid")
    quantum = Decimal("0.000000000000000001")
    if steps == 1 or minimum == maximum:
        return [minimum.quantize(quantum, rounding=ROUND_DOWN)]
    if kind == "linear":
        gap = (maximum - minimum) / Decimal(steps - 1)
        values = [(minimum + gap * i).quantize(quantum, rounding=ROUND_DOWN) for i in range(steps)]
        values[-1] = maximum.quantize(quantum, rounding=ROUND_DOWN)
        return list(dict.fromkeys(values))
    if kind != "geometric":
        raise ValueError("grid must be linear or geometric")
    ratio = Decimal(str(float(maximum / minimum) ** (1.0 / (steps - 1))))
    values = [(minimum * (ratio ** i)).quantize(quantum, rounding=ROUND_DOWN) for i in range(steps)]
    values[-1] = maximum.quantize(quantum, rounding=ROUND_DOWN)
    return list(dict.fromkeys(values))


class BidirectionalOptimizer:
    def __init__(self, uniswap, gas_model, strategy: StrategyConfig, inventory: Inventory):
        self.uniswap = uniswap
        self.gas_model = gas_model
        self.strategy = strategy
        self.inventory = inventory

    def _rebalance_cost(self, notional: Decimal) -> Decimal:
        return self.strategy.rebalancing_fixed_usdc + notional * self.strategy.rebalancing_bps / Decimal("10000")

    def evaluate(self, direction: Direction, quantity: Decimal, block_number: int,
                 book: OrderBook, taker_fee_rate: Decimal) -> Candidate:
        quantity = Decimal(str(quantity))
        if direction == Direction.BUY_UNISWAP_SELL_BINANCE:
            uni = self.uniswap.quote_buy_eth(quantity, block_number)
        elif direction == Direction.BUY_BINANCE_SELL_UNISWAP:
            uni = self.uniswap.quote_sell_eth(quantity, block_number)
        else:
            raise ValueError("cannot evaluate no_trade direction")
        return self._candidate_from_quote(
            direction, quantity, uni, block_number, book, taker_fee_rate
        )

    def _candidate_from_quote(self, direction, quantity, uni, block_number,
                              book, taker_fee_rate):
        if direction == Direction.BUY_UNISWAP_SELL_BINANCE:
            buy = uni.quote_usdc
            sell = calculate_sell_proceeds(book, quantity)
            exchange_fee = sell * taker_fee_rate
            inventory_reason = None
            if buy > self.inventory.uniswap_usdc:
                inventory_reason = "insufficient Uniswap USDC inventory"
            elif quantity > self.inventory.binance_weth:
                inventory_reason = "insufficient Binance WETH inventory"
        elif direction == Direction.BUY_BINANCE_SELL_UNISWAP:
            buy = calculate_buy_cost(book, quantity)
            sell = uni.quote_usdc
            exchange_fee = buy * taker_fee_rate
            inventory_reason = None
            if buy + exchange_fee > self.inventory.binance_usdc:
                inventory_reason = "insufficient Binance USDC inventory"
            elif quantity > self.inventory.uniswap_weth:
                inventory_reason = "insufficient Uniswap WETH inventory"
        gas = self.gas_model.estimate(uni.gas_estimate, book.mid, block_number)
        if inventory_reason is None and gas.cost_eth > self.inventory.uniswap_native_eth:
            inventory_reason = "insufficient native ETH for gas"
        rebalance = self._rebalance_cost((buy + sell) / Decimal("2"))
        profit = sell - buy - exchange_fee - gas.cost_usdc - rebalance
        profit_bps = profit / buy * Decimal("10000") if buy > 0 else Decimal("-Infinity")
        return Candidate(
            direction=direction, quantity_eth=quantity, gross_buy_usdc=buy, gross_sell_usdc=sell,
            exchange_fee_usdc=exchange_fee, gas_cost_usdc=gas.cost_usdc, gas_cost_eth=gas.cost_eth,
            rebalancing_cost_usdc=rebalance, net_profit_usdc=profit, profit_bps=profit_bps,
            feasible=inventory_reason is None, rejection_reason=inventory_reason,
            uniswap_gas_estimate=uni.gas_estimate,
        )

    def _evaluate_jobs(self, jobs, block_number, book, taker_fee_rate):
        jobs = list(jobs)
        if hasattr(self.uniswap, "quote_many"):
            try:
                quotes = self.uniswap.quote_many(jobs, block_number)
                candidates = []
                for (direction, quantity), quote in zip(jobs, quotes):
                    try:
                        candidates.append(self._candidate_from_quote(
                            direction, Decimal(str(quantity)), quote, block_number,
                            book, taker_fee_rate,
                        ))
                    except (ValueError, ArithmeticError):
                        continue
                return candidates
            except Exception:
                # Providers differ in JSON-RPC batch support. Preserve the
                # concurrent single-call path as a compatibility fallback.
                pass

        def run(job):
            direction, quantity = job
            try:
                return self.evaluate(direction, quantity, block_number, book, taker_fee_rate)
            except (ValueError, ArithmeticError):
                return None

        with ThreadPoolExecutor(max_workers=self.strategy.quote_workers) as executor:
            return [candidate for candidate in executor.map(run, jobs) if candidate is not None]

    def _select(self, candidates):
        eligible = [
            item for item in candidates
            if item.feasible
            and item.net_profit_usdc >= self.strategy.minimum_profit_usdc
            and item.profit_bps >= self.strategy.minimum_profit_bps
        ]
        best = max(eligible, key=lambda item: item.net_profit_usdc) if eligible else no_trade_candidate()
        return OptimizationResult(best=best, candidates=candidates)

    def optimize(self, quantities: Iterable[Decimal], block_number: int,
                 book: OrderBook, taker_fee_rate: Decimal) -> OptimizationResult:
        """Exhaustively evaluate every supplied quantity in both directions."""
        jobs = [
            (direction, quantity)
            for quantity in quantities
            for direction in (
                Direction.BUY_UNISWAP_SELL_BINANCE,
                Direction.BUY_BINANCE_SELL_UNISWAP,
            )
        ]
        return self._select(self._evaluate_jobs(jobs, block_number, book, taker_fee_rate))

    def optimize_adaptive(self, block_number: int, book: OrderBook,
                          taker_fee_rate: Decimal) -> OptimizationResult:
        """Coarse scan followed by local refinement around each direction's best size.

        This avoids assuming a perfectly smooth closed-form objective while using far
        fewer remote QuoterV2 calls than a dense exhaustive grid.
        """
        coarse = build_quantity_grid(
            self.strategy.min_quantity_eth,
            self.strategy.max_quantity_eth,
            self.strategy.coarse_quantity_steps,
            self.strategy.grid,
        )
        directions = (
            Direction.BUY_UNISWAP_SELL_BINANCE,
            Direction.BUY_BINANCE_SELL_UNISWAP,
        )
        cache = {}
        seen = set()

        def evaluate_new(jobs):
            unique = []
            for direction, quantity in jobs:
                key = (direction, Decimal(str(quantity)))
                if key not in seen:
                    seen.add(key)
                    unique.append(key)
            for candidate in self._evaluate_jobs(unique, block_number, book, taker_fee_rate):
                cache[(candidate.direction, candidate.quantity_eth)] = candidate

        evaluate_new((direction, quantity) for quantity in coarse for direction in directions)

        for _ in range(self.strategy.refinement_rounds):
            refinement_jobs = []
            for direction in directions:
                directional = sorted(
                    (candidate for candidate in cache.values()
                     if candidate.direction == direction and candidate.feasible),
                    key=lambda candidate: candidate.quantity_eth,
                )
                if not directional:
                    continue
                best = max(directional, key=lambda candidate: candidate.net_profit_usdc)
                index = directional.index(best)
                lower = directional[max(0, index - 1)].quantity_eth
                upper = directional[min(len(directional) - 1, index + 1)].quantity_eth
                if lower == upper:
                    continue
                refined = build_quantity_grid(
                    lower, upper, self.strategy.refinement_steps, "linear"
                )
                refinement_jobs.extend((direction, quantity) for quantity in refined)
            evaluate_new(refinement_jobs)

        candidates = sorted(
            cache.values(), key=lambda candidate: (candidate.quantity_eth, candidate.direction.value)
        )
        return self._select(candidates)
