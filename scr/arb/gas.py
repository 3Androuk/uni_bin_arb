from decimal import Decimal, ROUND_CEILING
from threading import Lock
from typing import Optional

from ..app.config import GasConfig
from ..app.models import GasCost


WEI_PER_ETH = Decimal(10) ** 18
WEI_PER_GWEI = Decimal(10) ** 9


class DynamicGasModel:
    def __init__(self, web3, config: GasConfig):
        self.web3 = web3
        self.config = config
        self._fee_cache = {}
        self._lock = Lock()

    def suggested_max_fee_per_gas(self, block_number: Optional[int] = None) -> int:
        cache_key = block_number if block_number is not None else "latest"
        with self._lock:
            if cache_key in self._fee_cache:
                return self._fee_cache[cache_key]
            block = self.web3.eth.get_block(cache_key)
            base_fee_value = block.get("baseFeePerGas")
            base_fee = int(base_fee_value if base_fee_value is not None else self.web3.eth.gas_price)
            try:
                priority = int(self.web3.eth.max_priority_fee)
            except Exception:
                priority = int(self.config.fallback_priority_fee_gwei * WEI_PER_GWEI)
            value = int(Decimal(base_fee) * self.config.base_fee_multiplier) + priority
            self._fee_cache[cache_key] = value
            if len(self._fee_cache) > 32:
                self._fee_cache.pop(next(iter(self._fee_cache)))
            return value

    def estimate(self, quoter_gas: int, eth_price_usdc: Decimal, block_number: Optional[int] = None) -> GasCost:
        gas_units = int((
            Decimal(quoter_gas) * self.config.quote_gas_multiplier
            + Decimal(self.config.router_overhead_gas)
        ).to_integral_value(rounding=ROUND_CEILING))
        max_fee = self.suggested_max_fee_per_gas(block_number)
        cost_eth = Decimal(gas_units * max_fee) / WEI_PER_ETH
        return GasCost(gas_units, max_fee, cost_eth, cost_eth * eth_price_usdc)


class FixedGasModel:
    """Deterministic model for tests and sensitivity analysis."""

    def __init__(self, cost_usdc: Decimal, cost_eth: Decimal = Decimal("0.002")):
        self.cost_usdc = Decimal(str(cost_usdc))
        self.cost_eth = Decimal(str(cost_eth))

    def estimate(self, quoter_gas: int, eth_price_usdc: Decimal, block_number=None) -> GasCost:
        return GasCost(int(quoter_gas), 0, self.cost_eth, self.cost_usdc)

