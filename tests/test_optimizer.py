from datetime import datetime, timezone
from decimal import Decimal

from src.arb.gas import FixedGasModel
from src.arb.optimizer import BidirectionalOptimizer, build_quantity_grid
from src.app.config import StrategyConfig
from src.app.models import Direction, Inventory, OrderBook, SwapQuote


class FakeUniswap:
    def __init__(self, buy_price, sell_price):
        self.buy_price = Decimal(str(buy_price))
        self.sell_price = Decimal(str(sell_price))

    def quote_buy_eth(self, quantity, block_number=None):
        q = Decimal(str(quantity)); cost = q * self.buy_price
        return SwapQuote("buy", q, cost, self.buy_price, 100000, 1, block_number=block_number)

    def quote_sell_eth(self, quantity, block_number=None):
        q = Decimal(str(quantity)); revenue = q * self.sell_price
        return SwapQuote("sell", q, revenue, self.sell_price, 100000, 1, block_number=block_number)


class CurvedUniswap:
    """Produces a concave buy-Uniswap profit curve with its maximum at 5 ETH."""

    def __init__(self):
        self.calls = 0

    def quote_buy_eth(self, quantity, block_number=None):
        self.calls += 1
        q = Decimal(str(quantity))
        cost = Decimal("1900") * q + Decimal("10") * q * q
        return SwapQuote("buy", q, cost, cost / q, 100000, 1, block_number=block_number)

    def quote_sell_eth(self, quantity, block_number=None):
        self.calls += 1
        q = Decimal(str(quantity))
        revenue = Decimal("1800") * q
        return SwapQuote("sell", q, revenue, revenue / q, 100000, 1, block_number=block_number)


def make_book():
    return OrderBook("ETHUSDC", [(Decimal("2000"), Decimal("20"))],
                     [(Decimal("2005"), Decimal("20"))], 1, datetime.now(timezone.utc))


def inventory(**changes):
    values = dict(uniswap_usdc=Decimal("100000"), uniswap_weth=Decimal("50"),
                  uniswap_native_eth=Decimal("1"), binance_usdc=Decimal("100000"),
                  binance_weth=Decimal("50"))
    values.update(changes)
    return Inventory(**values)


def strategy():
    return StrategyConfig(minimum_profit_usdc=Decimal("1"), minimum_profit_bps=Decimal("0"),
                          rebalancing_bps=Decimal("0"), rebalancing_fixed_usdc=Decimal("0"))


def test_selects_uniswap_to_binance_direction():
    optimizer = BidirectionalOptimizer(FakeUniswap(1900, 1800), FixedGasModel(5), strategy(), inventory())
    result = optimizer.optimize([Decimal("1"), Decimal("2")], 10, make_book(), Decimal("0.001"))
    assert result.best.direction == Direction.BUY_UNISWAP_SELL_BINANCE
    assert result.best.quantity_eth == Decimal("2")
    assert result.best.net_profit_usdc == Decimal("191.000")


def test_selects_binance_to_uniswap_direction():
    optimizer = BidirectionalOptimizer(FakeUniswap(2200, 2110), FixedGasModel(5), strategy(), inventory())
    result = optimizer.optimize([Decimal("1")], 10, make_book(), Decimal("0.001"))
    assert result.best.direction == Direction.BUY_BINANCE_SELL_UNISWAP
    assert result.best.net_profit_usdc == Decimal("97.995")


def test_inventory_can_force_no_trade():
    optimizer = BidirectionalOptimizer(
        FakeUniswap(1900, 1800), FixedGasModel(5), strategy(),
        inventory(binance_weth=Decimal("0"), uniswap_weth=Decimal("0")),
    )
    result = optimizer.optimize([Decimal("1")], 10, make_book(), Decimal("0.001"))
    assert result.best.direction == Direction.NO_TRADE
    assert all(not candidate.feasible for candidate in result.candidates)


def test_quantity_grid_has_exact_bounds():
    values = build_quantity_grid(Decimal("0.1"), Decimal("10"), 5, "geometric")
    assert values[0] == Decimal("0.100000000000000000")
    assert values[-1] == Decimal("10.000000000000000000")
    assert len(values) == 5
    assert all(-value.as_tuple().exponent <= 18 for value in values)


def test_adaptive_search_refines_around_optimum_with_fewer_quotes():
    uniswap = CurvedUniswap()
    adaptive_strategy = StrategyConfig(
        min_quantity_eth=Decimal("0.1"), max_quantity_eth=Decimal("10"),
        coarse_quantity_steps=7, refinement_steps=5, refinement_rounds=2,
        minimum_profit_usdc=Decimal("0"), minimum_profit_bps=Decimal("0"),
        rebalancing_bps=Decimal("0"), rebalancing_fixed_usdc=Decimal("0"),
    )
    optimizer = BidirectionalOptimizer(
        uniswap, FixedGasModel(0, 0), adaptive_strategy, inventory()
    )
    result = optimizer.optimize_adaptive(10, make_book(), Decimal("0"))

    assert result.best.direction == Direction.BUY_UNISWAP_SELL_BINANCE
    assert abs(result.best.quantity_eth - Decimal("5")) < Decimal("0.25")
    assert uniswap.calls <= 30
    assert len(result.candidates) == uniswap.calls

