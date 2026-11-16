import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import websockets
from eth_abi import decode
from eth_utils import keccak

from .models import PendingScenario, PendingSwap, PoolState, SwapQuote
from ..arb.optimizer import BidirectionalOptimizer
from ..data.uniswap import price_usdc_per_weth


LOGGER = logging.getLogger(__name__)
getcontext().prec = 70
Q96 = Decimal(2 ** 96)


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


DIRECT_SINGLE = {
    selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"):
        (True, ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"], True),
    selector("exactOutputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"):
        (False, ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"], True),
    selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"):
        (True, ["(address,address,uint24,address,uint256,uint256,uint160)"], False),
    selector("exactOutputSingle((address,address,uint24,address,uint256,uint256,uint160))"):
        (False, ["(address,address,uint24,address,uint256,uint256,uint160)"], False),
}
EXECUTE_WITH_DEADLINE = selector("execute(bytes,bytes[],uint256)")
EXECUTE = selector("execute(bytes,bytes[])")


def _hex_bytes(value: str) -> bytes:
    value = value[2:] if value.startswith("0x") else value
    return bytes.fromhex(value)


def decode_v3_path(path: bytes, exact_input: bool) -> Tuple[List[str], List[int]]:
    if len(path) < 43 or (len(path) - 20) % 23:
        raise ValueError("invalid V3 path length")
    tokens = ["0x" + path[:20].hex()]
    fees = []
    offset = 20
    while offset < len(path):
        fees.append(int.from_bytes(path[offset:offset + 3], "big"))
        tokens.append("0x" + path[offset + 3:offset + 23].hex())
        offset += 23
    if not exact_input:
        tokens.reverse()
        fees.reverse()
    return tokens, fees


class PendingTransactionDecoder:
    """Decode common direct SwapRouter and Universal Router V3 swap calls."""

    def decode(self, tx: Dict[str, Any]) -> List[PendingSwap]:
        calldata = _hex_bytes(tx.get("input") or tx.get("data") or "")
        if len(calldata) < 4:
            return []
        observed = datetime.now(timezone.utc)
        if calldata[:4] in DIRECT_SINGLE:
            return [self._decode_single(tx, calldata, observed)]
        if calldata[:4] in (EXECUTE, EXECUTE_WITH_DEADLINE):
            return self._decode_universal(tx, calldata, observed)
        return []

    def _decode_single(self, tx, calldata: bytes, observed: datetime) -> PendingSwap:
        exact_input, types, has_deadline = DIRECT_SINGLE[calldata[:4]]
        (params,) = decode(types, calldata[4:])
        token_in, token_out, fee = params[0], params[1], int(params[2])
        if has_deadline:
            amount_a, amount_b = int(params[5]), int(params[6])
        else:
            amount_a, amount_b = int(params[4]), int(params[5])
        return PendingSwap(
            tx_hash=tx.get("hash", ""), router=tx.get("to", ""),
            selector="0x" + calldata[:4].hex(), command_index=None,
            token_in=token_in, token_out=token_out, fee=fee, exact_input=exact_input,
            amount_specified_raw=amount_a, amount_bound_raw=amount_b,
            path=[token_in, token_out], observed_at=observed, raw_transaction=tx,
        )

    def _decode_universal(self, tx, calldata: bytes, observed: datetime) -> List[PendingSwap]:
        if calldata[:4] == EXECUTE_WITH_DEADLINE:
            commands, inputs, _ = decode(["bytes", "bytes[]", "uint256"], calldata[4:])
        else:
            commands, inputs = decode(["bytes", "bytes[]"], calldata[4:])
        swaps = []
        for index, (command_byte, command_input) in enumerate(zip(commands, inputs)):
            command = command_byte & 0x7F
            if command not in (0x00, 0x01):
                continue
            exact_input = command == 0x00
            values = self._decode_universal_v3_input(command_input)
            amount_specified, amount_bound, path = int(values[1]), int(values[2]), values[3]
            tokens, fees = decode_v3_path(path, exact_input)
            swaps.append(PendingSwap(
                tx_hash=tx.get("hash", ""), router=tx.get("to", ""),
                selector="0x" + calldata[:4].hex(), command_index=index,
                token_in=tokens[0], token_out=tokens[-1], fee=fees[0] if len(fees) == 1 else -1,
                exact_input=exact_input, amount_specified_raw=amount_specified,
                amount_bound_raw=amount_bound, path=tokens, observed_at=observed,
                raw_transaction=tx,
            ))
        return swaps

    @staticmethod
    def _decode_universal_v3_input(command_input: bytes):
        # Universal Router deployments differ: recent versions append per-hop price limits.
        for types in (
            ["address", "uint256", "uint256", "bytes", "bool", "uint256[]"],
            ["address", "uint256", "uint256", "bytes", "bool"],
        ):
            try:
                return decode(types, command_input)
            except Exception:
                continue
        raise ValueError("unsupported Universal Router V3 input encoding")


class ActiveLiquidityQuoter:
    """Local single-active-range V3 approximation used only for conditional scenarios."""

    def __init__(self, sqrt_price_x96: int, liquidity: int, fee: int,
                 token0_decimals: int = 6, token1_decimals: int = 18):
        if sqrt_price_x96 <= 0 or liquidity <= 0:
            raise ValueError("pool price and liquidity must be positive")
        self.sqrt_price = Decimal(sqrt_price_x96) / Q96
        self.liquidity = Decimal(liquidity)
        self.fee_rate = Decimal(fee) / Decimal(1_000_000)
        self.d0, self.d1 = token0_decimals, token1_decimals

    @property
    def sqrt_price_x96(self) -> int:
        return int(self.sqrt_price * Q96)

    @property
    def price(self) -> Decimal:
        return price_usdc_per_weth(self.sqrt_price_x96, self.d0, self.d1)

    def _zero_for_one_exact_in(self, raw_amount0: Decimal) -> Tuple[Decimal, Decimal]:
        net = raw_amount0 * (Decimal("1") - self.fee_rate)
        after = self.liquidity * self.sqrt_price / (self.liquidity + net * self.sqrt_price)
        output1 = self.liquidity * (self.sqrt_price - after)
        return after, output1

    def _one_for_zero_exact_in(self, raw_amount1: Decimal) -> Tuple[Decimal, Decimal]:
        net = raw_amount1 * (Decimal("1") - self.fee_rate)
        after = self.sqrt_price + net / self.liquidity
        output0 = self.liquidity * (Decimal("1") / self.sqrt_price - Decimal("1") / after)
        return after, output0

    def _zero_for_one_exact_out(self, raw_amount1: Decimal) -> Tuple[Decimal, Decimal]:
        after = self.sqrt_price - raw_amount1 / self.liquidity
        if after <= 0:
            raise ValueError("swap exceeds active liquidity approximation")
        net_input0 = self.liquidity * (Decimal("1") / after - Decimal("1") / self.sqrt_price)
        return after, net_input0 / (Decimal("1") - self.fee_rate)

    def _one_for_zero_exact_out(self, raw_amount0: Decimal) -> Tuple[Decimal, Decimal]:
        denominator = Decimal("1") / self.sqrt_price - raw_amount0 / self.liquidity
        if denominator <= 0:
            raise ValueError("swap exceeds active liquidity approximation")
        after = Decimal("1") / denominator
        net_input1 = self.liquidity * (after - self.sqrt_price)
        return after, net_input1 / (Decimal("1") - self.fee_rate)

    def apply(self, swap: PendingSwap, token0: str, token1: str) -> "ActiveLiquidityQuoter":
        t_in, t_out = swap.token_in.lower(), swap.token_out.lower()
        if t_in == token0.lower() and t_out == token1.lower():
            if swap.exact_input:
                after, calculated = self._zero_for_one_exact_in(Decimal(swap.amount_specified_raw))
            else:
                after, calculated = self._zero_for_one_exact_out(Decimal(swap.amount_specified_raw))
        elif t_in == token1.lower() and t_out == token0.lower():
            if swap.exact_input:
                after, calculated = self._one_for_zero_exact_in(Decimal(swap.amount_specified_raw))
            else:
                after, calculated = self._one_for_zero_exact_out(Decimal(swap.amount_specified_raw))
        else:
            raise ValueError("pending swap does not directly match configured pool")
        if swap.amount_bound_raw:
            if swap.exact_input and calculated < Decimal(swap.amount_bound_raw):
                raise ValueError("conditional swap violates amountOutMinimum")
            if not swap.exact_input and calculated > Decimal(swap.amount_bound_raw):
                raise ValueError("conditional swap violates amountInMaximum")
        clone = ActiveLiquidityQuoter(
            self.sqrt_price_x96, int(self.liquidity), int(self.fee_rate * 1_000_000), self.d0, self.d1
        )
        clone.sqrt_price = after
        return clone

    def quote_buy_eth(self, quantity_eth: Decimal, block_number=None) -> SwapQuote:
        q = Decimal(str(quantity_eth))
        raw_out = q * (Decimal(10) ** self.d1)
        after, raw_in = self._zero_for_one_exact_out(raw_out)
        cost = raw_in / (Decimal(10) ** self.d0)
        return SwapQuote("buy", q, cost, cost / q, 180000, int(after * Q96), 0, block_number)

    def quote_sell_eth(self, quantity_eth: Decimal, block_number=None) -> SwapQuote:
        q = Decimal(str(quantity_eth))
        raw_in = q * (Decimal(10) ** self.d1)
        after, raw_out = self._one_for_zero_exact_in(raw_in)
        revenue = raw_out / (Decimal(10) ** self.d0)
        return SwapQuote("sell", q, revenue, revenue / q, 180000, int(after * Q96), 0, block_number)


SCENARIO_LIMITATIONS = (
    "The public/provider mempool is incomplete and excludes private order flow.",
    "Inclusion, success, replacement, cancellation and transaction ordering are unknown.",
    "The local V3 calculation assumes the swap remains within the current active liquidity range.",
    "It does not execute arbitrary router logic or model competing arbitrage/MEV bundles.",
    "Conditional profit is a research counterfactual, not an executable quote or trading signal.",
)


class PendingScenarioSimulator:
    def __init__(self, app_config, gas_model):
        self.config = app_config
        self.gas_model = gas_model

    def simulate(self, swap: PendingSwap, pool: PoolState, book, taker_fee_rate: Decimal,
                 quantities: Sequence[Decimal]) -> PendingScenario:
        uni = self.config.uniswap
        baseline = ActiveLiquidityQuoter(
            pool.sqrt_price_x96, pool.liquidity, pool.fee,
            uni.token0_decimals, uni.token1_decimals,
        )
        if len(swap.path) != 2 or swap.fee != uni.pool_fee:
            return PendingScenario(swap.tx_hash, baseline.price, baseline.price, Decimal("0"), False,
                                   "only direct swaps through the configured fee tier are simulated",
                                   SCENARIO_LIMITATIONS)
        specified_token = swap.token_in if swap.exact_input else swap.token_out
        if specified_token.lower() == uni.token0_address.lower():
            specified_notional = Decimal(swap.amount_specified_raw) / (Decimal(10) ** uni.token0_decimals)
        elif specified_token.lower() == uni.token1_address.lower():
            specified_notional = (
                Decimal(swap.amount_specified_raw) / (Decimal(10) ** uni.token1_decimals)
                * baseline.price
            )
        else:
            specified_notional = Decimal("Infinity")
        if specified_notional > self.config.pending.max_scenario_notional_usdc:
            return PendingScenario(
                swap.tx_hash, baseline.price, baseline.price, Decimal("0"), False,
                "specified amount exceeds pending.max_scenario_notional_usdc", SCENARIO_LIMITATIONS,
            )
        try:
            conditional = baseline.apply(swap, uni.token0_address, uni.token1_address)
            change = (conditional.price / baseline.price - Decimal("1")) * Decimal("10000")
            local_optimizer = BidirectionalOptimizer(
                conditional, self.gas_model, self.config.strategy, self.config.inventory
            )
            result = local_optimizer.optimize(quantities, pool.block_number, book, taker_fee_rate)
            return PendingScenario(
                swap.tx_hash, baseline.price, conditional.price, change, True, None,
                SCENARIO_LIMITATIONS, result.best,
            )
        except Exception as exc:
            return PendingScenario(swap.tx_hash, baseline.price, baseline.price, Decimal("0"), False,
                                   str(exc), SCENARIO_LIMITATIONS)


class AlchemyPendingCollector:
    def __init__(self, ws_url: str, routers: Sequence[str], decoder: PendingTransactionDecoder,
                 store, on_swaps: Optional[Callable] = None):
        self.ws_url = ws_url
        self.routers = list(routers)
        self.decoder = decoder
        self.store = store
        self.on_swaps = on_swaps

    async def run(self):
        delay = 1
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as socket:
                    request = {"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": [
                        "alchemy_pendingTransactions",
                        {"toAddress": self.routers, "hashesOnly": False},
                    ]}
                    await socket.send(json.dumps(request))
                    acknowledgement = json.loads(await socket.recv())
                    if "error" in acknowledgement:
                        raise RuntimeError(str(acknowledgement["error"]))
                    delay = 1
                    async for raw in socket:
                        message = json.loads(raw)
                        tx = message.get("params", {}).get("result")
                        if not isinstance(tx, dict):
                            continue
                        try:
                            swaps = self.decoder.decode(tx)
                            self.store.record_pending(tx, swaps)
                            if swaps and self.on_swaps:
                                outcome = self.on_swaps(tx, swaps)
                                if asyncio.iscoroutine(outcome):
                                    await outcome
                        except Exception as exc:
                            self.store.record_pending(tx, [], str(exc))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("pending transaction stream disconnected; reconnecting")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
