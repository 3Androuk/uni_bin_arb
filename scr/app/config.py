import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

try:
    import tomllib
except ImportError:  # Python 3.9/3.10
    import tomli as tomllib

from dotenv import load_dotenv

from .models import Inventory


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class EthereumConfig:
    rpc_url: str
    ws_url: str = ""
    chain_id: int = 1
    confirmation_blocks: int = 0


@dataclass(frozen=True)
class UniswapConfig:
    pool_address: str
    quoter_v2_address: str
    token0_address: str
    token1_address: str
    token0_symbol: str = "USDC"
    token1_symbol: str = "WETH"
    token0_decimals: int = 6
    token1_decimals: int = 18
    pool_fee: int = 500


@dataclass(frozen=True)
class BinanceConfig:
    symbol: str = "ETHUSDC"
    rest_base_url: str = "https://data-api.binance.vision"
    account_base_url: str = "https://api.binance.com"
    depth_limit: int = 100
    fallback_taker_fee_bps: Decimal = Decimal("10")
    api_key_env: str = "BINANCE_API_KEY"
    api_secret_env: str = "BINANCE_API_SECRET"


@dataclass(frozen=True)
class GasConfig:
    quote_gas_multiplier: Decimal = Decimal("1.20")
    router_overhead_gas: int = 45000
    base_fee_multiplier: Decimal = Decimal("1.20")
    fallback_priority_fee_gwei: Decimal = Decimal("1.5")


@dataclass(frozen=True)
class StrategyConfig:
    min_quantity_eth: Decimal = Decimal("0.05")
    max_quantity_eth: Decimal = Decimal("10")
    quantity_steps: int = 30
    grid: str = "geometric"
    quote_workers: int = 8
    search_mode: str = "adaptive"
    coarse_quantity_steps: int = 7
    refinement_steps: int = 5
    refinement_rounds: int = 2
    minimum_profit_usdc: Decimal = Decimal("0.10")
    minimum_profit_bps: Decimal = Decimal("0")
    rebalancing_bps: Decimal = Decimal("2")
    rebalancing_fixed_usdc: Decimal = Decimal("0")


@dataclass(frozen=True)
class MonitorConfig:
    database_path: str = "data/observations.sqlite3"
    poll_interval_seconds: float = 5.0
    max_market_age_seconds: float = 20.0
    latency_delays_ms: List[int] = field(default_factory=lambda: [0, 1000, 5000])


@dataclass(frozen=True)
class PendingConfig:
    enabled: bool = False
    router_addresses: List[str] = field(default_factory=list)
    simulate_matching_swaps: bool = True
    max_scenario_notional_usdc: Decimal = Decimal("1000000")


@dataclass(frozen=True)
class AppConfig:
    ethereum: EthereumConfig
    uniswap: UniswapConfig
    binance: BinanceConfig
    gas: GasConfig
    strategy: StrategyConfig
    inventory: Inventory
    monitor: MonitorConfig
    pending: PendingConfig
    project_root: Path


def _decimal_fields(section: Dict[str, Any], names: List[str]) -> Dict[str, Any]:
    result = dict(section)
    for name in names:
        if name in result:
            result[name] = Decimal(str(result[name]))
    return result


def load_config(path: str) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", override=False)
    with config_path.open("rb") as handle:
        raw = _expand(tomllib.load(handle))

    eth = EthereumConfig(**raw["ethereum"])
    if not eth.rpc_url:
        raise ValueError("ethereum.rpc_url is empty; set ETHEREUM_RPC_URL")

    binance = BinanceConfig(**_decimal_fields(raw.get("binance", {}), ["fallback_taker_fee_bps"]))
    gas = GasConfig(**_decimal_fields(raw.get("gas", {}), [
        "quote_gas_multiplier", "base_fee_multiplier", "fallback_priority_fee_gwei"
    ]))
    strategy = StrategyConfig(**_decimal_fields(raw.get("strategy", {}), [
        "min_quantity_eth", "max_quantity_eth", "minimum_profit_usdc",
        "minimum_profit_bps", "rebalancing_bps", "rebalancing_fixed_usdc",
    ]))
    inventory_values = _decimal_fields(raw["inventory"], list(raw["inventory"].keys()))
    pending = PendingConfig(**_decimal_fields(raw.get("pending", {}), ["max_scenario_notional_usdc"]))
    monitor = MonitorConfig(**raw.get("monitor", {}))

    if strategy.min_quantity_eth <= 0 or strategy.max_quantity_eth < strategy.min_quantity_eth:
        raise ValueError("invalid strategy quantity range")
    if strategy.quantity_steps < 1:
        raise ValueError("strategy.quantity_steps must be positive")
    if strategy.quote_workers < 1:
        raise ValueError("strategy.quote_workers must be positive")
    if strategy.search_mode not in ("adaptive", "exhaustive"):
        raise ValueError("strategy.search_mode must be adaptive or exhaustive")
    if strategy.coarse_quantity_steps < 3:
        raise ValueError("strategy.coarse_quantity_steps must be at least 3")
    if strategy.refinement_steps < 3:
        raise ValueError("strategy.refinement_steps must be at least 3")
    if strategy.refinement_rounds < 0:
        raise ValueError("strategy.refinement_rounds cannot be negative")

    db_path = Path(monitor.database_path)
    if not db_path.is_absolute():
        monitor = MonitorConfig(
            database_path=str((config_path.parent / db_path).resolve()),
            poll_interval_seconds=monitor.poll_interval_seconds,
            max_market_age_seconds=monitor.max_market_age_seconds,
            latency_delays_ms=monitor.latency_delays_ms,
        )

    return AppConfig(
        ethereum=eth,
        uniswap=UniswapConfig(**raw["uniswap"]),
        binance=binance,
        gas=gas,
        strategy=strategy,
        inventory=Inventory(**inventory_values),
        monitor=monitor,
        pending=pending,
        project_root=config_path.parent,
    )
