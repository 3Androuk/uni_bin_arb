from datetime import datetime, timezone
from decimal import Decimal

from eth_abi import encode

from src.app.models import PendingSwap
from src.app.pending import (
    EXECUTE_WITH_DEADLINE, ActiveLiquidityQuoter, PendingTransactionDecoder, decode_v3_path,
)


USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"


def path(token_in, fee, token_out):
    return bytes.fromhex(token_in[2:]) + fee.to_bytes(3, "big") + bytes.fromhex(token_out[2:])


def test_decodes_universal_router_v3_exact_input():
    route = path(USDC, 500, WETH)
    command_input = encode(
        ["address", "uint256", "uint256", "bytes", "bool"],
        ["0x0000000000000000000000000000000000000001", 2_000_000_000, 900_000_000_000_000_000, route, True],
    )
    payload = encode(["bytes", "bytes[]", "uint256"], [b"\x00", [command_input], 2_000_000_000])
    tx = {"hash": "0xabc", "to": "0x0000000000000000000000000000000000000002",
          "input": "0x" + (EXECUTE_WITH_DEADLINE + payload).hex()}
    swaps = PendingTransactionDecoder().decode(tx)
    assert len(swaps) == 1
    assert swaps[0].exact_input
    assert swaps[0].fee == 500
    assert swaps[0].token_in.lower() == USDC.lower()
    assert swaps[0].token_out.lower() == WETH.lower()


def test_exact_output_path_is_reversed_to_trade_direction():
    tokens, fees = decode_v3_path(path(WETH, 500, USDC), exact_input=False)
    assert tokens[0].lower() == USDC.lower()
    assert tokens[-1].lower() == WETH.lower()
    assert fees == [500]


def test_active_liquidity_price_moves_in_expected_directions():
    sqrt_x96 = int((Decimal("0.0005").sqrt()) * Decimal(2 ** 96) / Decimal(10 ** 6))
    # Use a realistic on-chain-scale sqrt directly: sqrt(raw token1/token0).
    sqrt_x96 = int((Decimal("500000000").sqrt()) * Decimal(2 ** 96))
    base = ActiveLiquidityQuoter(sqrt_x96, 10 ** 18, 500)
    buy_weth = PendingSwap("0x1", "0xrouter", "0x", 0, USDC, WETH, 500, True,
                           1_000_000_000, 0, [USDC, WETH], datetime.now(timezone.utc))
    sell_weth = PendingSwap("0x2", "0xrouter", "0x", 0, WETH, USDC, 500, True,
                            10 ** 18, 0, [WETH, USDC], datetime.now(timezone.utc))
    assert base.apply(buy_weth, USDC, WETH).price > base.price
    assert base.apply(sell_weth, USDC, WETH).price < base.price

