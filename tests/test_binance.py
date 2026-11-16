from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.data.binance import calculate_buy_cost, calculate_sell_proceeds, walk_order_book
from src.app.models import OrderBook


def book():
    return OrderBook(
        "ETHUSDC",
        bids=[(Decimal("2000"), Decimal("1")), (Decimal("1990"), Decimal("2"))],
        asks=[(Decimal("2010"), Decimal("1")), (Decimal("2020"), Decimal("2"))],
        last_update_id=1,
        received_at=datetime.now(timezone.utc),
    )


def test_walks_both_sides_across_levels():
    assert calculate_sell_proceeds(book(), Decimal("2")) == Decimal("3990")
    assert calculate_buy_cost(book(), Decimal("2")) == Decimal("4030")


def test_rejects_insufficient_depth_and_invalid_quantity():
    with pytest.raises(ValueError, match="insufficient"):
        walk_order_book(book().bids, Decimal("4"))
    with pytest.raises(ValueError, match="positive"):
        walk_order_book(book().bids, Decimal("0"))

