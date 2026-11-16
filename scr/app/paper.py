from dataclasses import replace
from decimal import Decimal

from .models import Candidate, Direction, Inventory


class PaperPortfolio:
    """Optional inventory ledger for hypothetical fills; it never touches a venue."""

    def __init__(self, inventory: Inventory):
        self.inventory = replace(inventory)
        self.realized_pnl_usdc = Decimal("0")

    def apply(self, candidate: Candidate) -> None:
        if not candidate.feasible or candidate.direction == Direction.NO_TRADE:
            raise ValueError("cannot paper-fill an infeasible candidate")
        q = candidate.quantity_eth
        if candidate.direction == Direction.BUY_UNISWAP_SELL_BINANCE:
            self.inventory.uniswap_usdc -= candidate.gross_buy_usdc
            self.inventory.uniswap_weth += q
            self.inventory.binance_weth -= q
            self.inventory.binance_usdc += candidate.gross_sell_usdc - candidate.exchange_fee_usdc
        else:
            self.inventory.binance_usdc -= candidate.gross_buy_usdc + candidate.exchange_fee_usdc
            self.inventory.binance_weth += q
            self.inventory.uniswap_weth -= q
            self.inventory.uniswap_usdc += candidate.gross_sell_usdc
        self.inventory.uniswap_native_eth -= candidate.gas_cost_eth
        self.realized_pnl_usdc += candidate.net_profit_usdc
