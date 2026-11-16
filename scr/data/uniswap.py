from datetime import datetime, timezone
from decimal import Decimal, getcontext
from typing import Optional

from web3 import Web3

from ..app.config import EthereumConfig, UniswapConfig
from ..app.models import Direction, PoolState, SwapQuote


getcontext().prec = 70

POOL_ABI = [
    {"inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"},
        {"name": "unlocked", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "fee", "outputs": [{"name": "", "type": "uint24"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [{"name": "", "type": "uint128"}], "stateMutability": "view", "type": "function"},
]

_QUOTE_PARAMS = [
    {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
    {"name": "amount", "type": "uint256"}, {"name": "fee", "type": "uint24"},
    {"name": "sqrtPriceLimitX96", "type": "uint160"},
]
_QUOTE_OUTPUTS = [
    {"name": "amount", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
    {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"},
]
QUOTER_V2_ABI = [
    {"name": "quoteExactOutputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "params", "type": "tuple", "components": _QUOTE_PARAMS}], "outputs": _QUOTE_OUTPUTS},
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "params", "type": "tuple", "components": _QUOTE_PARAMS}], "outputs": _QUOTE_OUTPUTS},
]


def price_usdc_per_weth(sqrt_price_x96: int, token0_decimals: int = 6, token1_decimals: int = 18) -> Decimal:
    if sqrt_price_x96 <= 0:
        raise ValueError("sqrt_price_x96 must be positive")
    return (
        Decimal(2 ** 192) / Decimal(sqrt_price_x96) ** 2
        * Decimal(10) ** (token1_decimals - token0_decimals)
    )


class UniswapV3ReadOnlyClient:
    """Pool reads and QuoterV2 eth_call requests; never signs or sends a transaction."""

    def __init__(self, ethereum: EthereumConfig, config: UniswapConfig):
        self.ethereum = ethereum
        self.config = config
        self.w3 = Web3(Web3.HTTPProvider(ethereum.rpc_url, request_kwargs={"timeout": 30}))
        if not self.w3.is_connected():
            raise ConnectionError("could not connect to Ethereum RPC")
        if self.w3.eth.chain_id != ethereum.chain_id:
            raise ValueError("expected chain {}, got {}".format(ethereum.chain_id, self.w3.eth.chain_id))
        self.pool = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.pool_address), abi=POOL_ABI
        )
        self.quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.quoter_v2_address), abi=QUOTER_V2_ABI
        )
        self.token0 = Web3.to_checksum_address(config.token0_address)
        self.token1 = Web3.to_checksum_address(config.token1_address)

    def current_block(self) -> int:
        return max(0, self.w3.eth.block_number - self.ethereum.confirmation_blocks)

    def get_pool_state(self, block_number: Optional[int] = None) -> PoolState:
        block = self.current_block() if block_number is None else block_number
        slot0 = self.pool.functions.slot0().call(block_identifier=block)
        token0 = self.pool.functions.token0().call(block_identifier=block)
        token1 = self.pool.functions.token1().call(block_identifier=block)
        fee = self.pool.functions.fee().call(block_identifier=block)
        liquidity = self.pool.functions.liquidity().call(block_identifier=block)
        if Web3.to_checksum_address(token0) != self.token0 or Web3.to_checksum_address(token1) != self.token1:
            raise ValueError("configured tokens do not match pool token ordering")
        if fee != self.config.pool_fee:
            raise ValueError("configured fee does not match pool")
        block_data = self.w3.eth.get_block(block)
        block_time = datetime.fromtimestamp(block_data["timestamp"], tz=timezone.utc)
        return PoolState(
            block_number=block,
            observed_at=datetime.now(timezone.utc),
            sqrt_price_x96=int(slot0[0]), tick=int(slot0[1]), liquidity=int(liquidity), fee=int(fee),
            price_usdc_per_weth=price_usdc_per_weth(
                int(slot0[0]), self.config.token0_decimals, self.config.token1_decimals
            ),
            block_timestamp=block_time,
        )

    def _raw_weth(self, quantity_eth: Decimal) -> int:
        quantity = Decimal(str(quantity_eth))
        raw = quantity * (Decimal(10) ** self.config.token1_decimals)
        if quantity <= 0 or raw != raw.to_integral_value():
            raise ValueError("quantity must be positive and fit token decimals")
        return int(raw)

    def quote_buy_eth(self, quantity_eth: Decimal, block_number: Optional[int] = None) -> SwapQuote:
        quantity = Decimal(str(quantity_eth))
        params = (self.token0, self.token1, self._raw_weth(quantity), self.config.pool_fee, 0)
        result = self.quoter.functions.quoteExactOutputSingle(params).call(
            block_identifier=block_number if block_number is not None else "latest"
        )
        amount_in, sqrt_after, ticks, gas = result
        cost = Decimal(amount_in) / (Decimal(10) ** self.config.token0_decimals)
        return SwapQuote("buy", quantity, cost, cost / quantity, int(gas), int(sqrt_after), int(ticks), block_number)

    def quote_sell_eth(self, quantity_eth: Decimal, block_number: Optional[int] = None) -> SwapQuote:
        quantity = Decimal(str(quantity_eth))
        params = (self.token1, self.token0, self._raw_weth(quantity), self.config.pool_fee, 0)
        result = self.quoter.functions.quoteExactInputSingle(params).call(
            block_identifier=block_number if block_number is not None else "latest"
        )
        amount_out, sqrt_after, ticks, gas = result
        revenue = Decimal(amount_out) / (Decimal(10) ** self.config.token0_decimals)
        return SwapQuote("sell", quantity, revenue, revenue / quantity, int(gas), int(sqrt_after), int(ticks), block_number)

    def quote_many(self, jobs, block_number: int):
        """Execute a group of QuoterV2 calls in one JSON-RPC batch request."""
        prepared = []
        with self.w3.batch_requests() as batch:
            for direction, quantity_value in jobs:
                quantity = Decimal(str(quantity_value))
                if direction == Direction.BUY_UNISWAP_SELL_BINANCE:
                    params = (
                        self.token0, self.token1, self._raw_weth(quantity),
                        self.config.pool_fee, 0,
                    )
                    request = self.quoter.functions.quoteExactOutputSingle(params).call(
                        block_identifier=block_number
                    )
                    side = "buy"
                elif direction == Direction.BUY_BINANCE_SELL_UNISWAP:
                    params = (
                        self.token1, self.token0, self._raw_weth(quantity),
                        self.config.pool_fee, 0,
                    )
                    request = self.quoter.functions.quoteExactInputSingle(params).call(
                        block_identifier=block_number
                    )
                    side = "sell"
                else:
                    raise ValueError("cannot quote no_trade direction")
                batch.add(request)
                prepared.append((side, quantity))
            responses = batch.execute()

        quotes = []
        scale = Decimal(10) ** self.config.token0_decimals
        for (side, quantity), response in zip(prepared, responses):
            amount, sqrt_after, ticks, gas = response
            quote_usdc = Decimal(amount) / scale
            quotes.append(SwapQuote(
                side, quantity, quote_usdc, quote_usdc / quantity,
                int(gas), int(sqrt_after), int(ticks), block_number,
            ))
        return quotes

