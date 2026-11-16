import hashlib
import hmac
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence, Tuple
from urllib.parse import urlencode

import requests

from ..app.config import BinanceConfig
from ..app.models import OrderBook


def walk_order_book(levels: Sequence[Tuple[Decimal, Decimal]], quantity: Decimal) -> Decimal:
    """Return quote-currency value for an exact base quantity."""
    quantity = Decimal(str(quantity))
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("quantity must be positive and finite")
    remaining = quantity
    total = Decimal("0")
    for price, available in levels:
        if price <= 0 or available <= 0:
            raise ValueError("order-book levels must be positive")
        filled = min(remaining, available)
        total += filled * price
        remaining -= filled
        if remaining == 0:
            return total
    raise ValueError("insufficient order-book depth: {} base units unfilled".format(remaining))


def calculate_buy_cost(order_book: OrderBook, quantity: Decimal) -> Decimal:
    return walk_order_book(order_book.asks, quantity)


def calculate_sell_proceeds(order_book: OrderBook, quantity: Decimal) -> Decimal:
    return walk_order_book(order_book.bids, quantity)


class BinanceReadOnlyClient:
    """Binance market/account-fee reads. It intentionally has no order methods."""

    def __init__(self, config: BinanceConfig, session=None):
        self.config = config
        self.session = session or requests.Session()

    def get_order_book(self) -> OrderBook:
        response = self.session.get(
            self.config.rest_base_url.rstrip("/") + "/api/v3/depth",
            params={"symbol": self.config.symbol, "limit": self.config.depth_limit},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        bids = sorted(
            [(Decimal(price), Decimal(qty)) for price, qty in payload["bids"]],
            key=lambda level: level[0], reverse=True,
        )
        asks = sorted(
            [(Decimal(price), Decimal(qty)) for price, qty in payload["asks"]],
            key=lambda level: level[0],
        )
        if not bids or not asks:
            raise ValueError("empty Binance order book")
        if bids[0][0] >= asks[0][0]:
            raise ValueError("locked or crossed Binance order book")
        return OrderBook(
            symbol=self.config.symbol,
            bids=bids,
            asks=asks,
            last_update_id=int(payload["lastUpdateId"]),
            received_at=datetime.now(timezone.utc),
        )

    def get_taker_fee_rate(self) -> Decimal:
        """Read the account-specific fee when credentials exist, else configured fallback."""
        api_key = os.getenv(self.config.api_key_env, "")
        secret = os.getenv(self.config.api_secret_env, "")
        if not api_key or not secret:
            return self.config.fallback_taker_fee_bps / Decimal("10000")

        params = {"symbol": self.config.symbol, "timestamp": int(time.time() * 1000)}
        query = urlencode(params)
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        response = self.session.get(
            self.config.account_base_url.rstrip("/") + "/sapi/v1/asset/tradeFee",
            params={**params, "signature": signature},
            headers={"X-MBX-APIKEY": api_key},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload:
            raise ValueError("Binance tradeFee response was empty")
        return Decimal(str(payload[0]["takerCommission"]))

