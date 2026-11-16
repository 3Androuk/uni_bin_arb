from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


ZERO = Decimal("0")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Direction(str, Enum):
    BUY_UNISWAP_SELL_BINANCE = "buy_uniswap_sell_binance"
    BUY_BINANCE_SELL_UNISWAP = "buy_binance_sell_uniswap"
    NO_TRADE = "no_trade"


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: Sequence[Tuple[Decimal, Decimal]]
    asks: Sequence[Tuple[Decimal, Decimal]]
    last_update_id: int
    received_at: datetime

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0][0]

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0][0]

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")


@dataclass(frozen=True)
class SwapQuote:
    side: str
    quantity_eth: Decimal
    quote_usdc: Decimal
    average_price: Decimal
    gas_estimate: int
    sqrt_price_after: int
    ticks_crossed: int = 0
    block_number: Optional[int] = None


@dataclass(frozen=True)
class GasCost:
    gas_units: int
    max_fee_per_gas_wei: int
    cost_eth: Decimal
    cost_usdc: Decimal


@dataclass
class Inventory:
    uniswap_usdc: Decimal
    uniswap_weth: Decimal
    uniswap_native_eth: Decimal
    binance_usdc: Decimal
    binance_weth: Decimal


@dataclass(frozen=True)
class Candidate:
    direction: Direction
    quantity_eth: Decimal
    gross_buy_usdc: Decimal
    gross_sell_usdc: Decimal
    exchange_fee_usdc: Decimal
    gas_cost_usdc: Decimal
    gas_cost_eth: Decimal
    rebalancing_cost_usdc: Decimal
    net_profit_usdc: Decimal
    profit_bps: Decimal
    feasible: bool
    rejection_reason: Optional[str] = None
    uniswap_gas_estimate: int = 0

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["direction"] = self.direction.value
        return value


@dataclass(frozen=True)
class OptimizationResult:
    best: Candidate
    candidates: Sequence[Candidate]
    evaluated_at: datetime = field(default_factory=utc_now)

    @property
    def should_trade(self) -> bool:
        return self.best.direction != Direction.NO_TRADE


@dataclass(frozen=True)
class PoolState:
    block_number: int
    observed_at: datetime
    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee: int
    price_usdc_per_weth: Decimal
    block_timestamp: Optional[datetime] = None


@dataclass(frozen=True)
class PendingSwap:
    tx_hash: str
    router: str
    selector: str
    command_index: Optional[int]
    token_in: str
    token_out: str
    fee: int
    exact_input: bool
    amount_specified_raw: int
    amount_bound_raw: int
    path: Sequence[str]
    observed_at: datetime
    raw_transaction: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingScenario:
    tx_hash: str
    baseline_price: Decimal
    conditional_price: Decimal
    price_change_bps: Decimal
    valid: bool
    reason: Optional[str]
    limitations: Sequence[str]
    conditional_best: Optional[Candidate] = None


def no_trade_candidate(reason: str = "no candidate met thresholds") -> Candidate:
    return Candidate(
        direction=Direction.NO_TRADE,
        quantity_eth=ZERO,
        gross_buy_usdc=ZERO,
        gross_sell_usdc=ZERO,
        exchange_fee_usdc=ZERO,
        gas_cost_usdc=ZERO,
        gas_cost_eth=ZERO,
        rebalancing_cost_usdc=ZERO,
        net_profit_usdc=ZERO,
        profit_bps=ZERO,
        feasible=False,
        rejection_reason=reason,
    )

