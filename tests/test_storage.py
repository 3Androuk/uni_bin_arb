from datetime import datetime, timezone
from decimal import Decimal

from src.app.models import (
    Candidate, Direction, OptimizationResult, OrderBook, PoolState,
)
from src.app.storage import ResearchStore


def test_records_observations_candidates_and_signals(tmp_path):
    store = ResearchStore(str(tmp_path / "research.sqlite3"))
    candidate = Candidate(Direction.BUY_UNISWAP_SELL_BINANCE, Decimal("1"), Decimal("1900"),
                          Decimal("2000"), Decimal("2"), Decimal("5"), Decimal("0.002"),
                          Decimal("1"), Decimal("92"), Decimal("484.2"), True)
    result = OptimizationResult(candidate, [candidate])
    now = datetime.now(timezone.utc)
    pool = PoolState(1, now, 1, 1, 1, 500, Decimal("1950"), now)
    book = OrderBook("ETHUSDC", [(Decimal("2000"), Decimal("2"))],
                     [(Decimal("2001"), Decimal("2"))], 1, now)
    observation_id = store.record_observation(pool, book, Decimal("0.001"), result, 0)
    store.schedule_paper_signals(observation_id, candidate, now, [0, 1000])
    trade_id = store.record_paper_trade(
        "session-1", observation_id, candidate, Decimal("92"),
        {"uniswap_usdc": Decimal("98100")},
    )
    assert trade_id > 0
    assert store.summary()["observations"] == 1
    assert store.summary()["profitable_observations"] == 1
    assert store.summary()["paper_trades"] == 1
    assert len(store.due_signals(now)) == 1
    store.close()
