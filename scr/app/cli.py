import argparse
import asyncio
import json
import logging
import shutil
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .config import load_config
from ..data.binance import BinanceReadOnlyClient
from ..data.uniswap import UniswapV3ReadOnlyClient
from ..arb.gas import DynamicGasModel
from .monitor import MarketMonitor
from ..arb.optimizer import BidirectionalOptimizer, build_quantity_grid
from .pending import AlchemyPendingCollector, PendingScenarioSimulator, PendingTransactionDecoder
from .paper import PaperPortfolio
from .storage import ResearchStore


def _runtime(config_path):
    config = load_config(config_path)
    binance = BinanceReadOnlyClient(config.binance)
    uniswap = UniswapV3ReadOnlyClient(config.ethereum, config.uniswap)
    gas = DynamicGasModel(uniswap.w3, config.gas)
    optimizer = BidirectionalOptimizer(uniswap, gas, config.strategy, config.inventory)
    store = ResearchStore(config.monitor.database_path)
    return config, binance, uniswap, gas, optimizer, store


def _print_sample(sample):
    observation_id, pool, book, result, stale = sample
    best = result.best
    print(json.dumps({
        "observation_id": observation_id,
        "block_number": pool.block_number,
        "uniswap_price_usdc": str(pool.price_usdc_per_weth),
        "binance_best_bid": str(book.best_bid),
        "binance_best_ask": str(book.best_ask),
        "stale": stale,
        "decision": best.direction.value,
        "quantity_eth": str(best.quantity_eth),
        "estimated_profit_usdc": str(best.net_profit_usdc),
        "profit_bps": str(best.profit_bps),
    }, indent=2))


def cmd_init(args):
    destination = Path(args.destination).resolve()
    source = Path(__file__).resolve().parents[2] / "config.example.toml"
    if destination.exists() and not args.force:
        raise FileExistsError("{} already exists; use --force to replace it".format(destination))
    shutil.copyfile(str(source), str(destination))
    print("Created {}".format(destination))


def cmd_once(args):
    config, binance, uniswap, gas, optimizer, store = _runtime(args.config)
    try:
        _print_sample(MarketMonitor(config, binance, uniswap, optimizer, store).sample_once())
    finally:
        store.close()


def cmd_monitor(args):
    config, binance, uniswap, gas, optimizer, store = _runtime(args.config)
    try:
        for sample in MarketMonitor(config, binance, uniswap, optimizer, store).run(args.iterations):
            _print_sample(sample)
    finally:
        store.close()


def cmd_paper(args):
    config, binance, uniswap, gas, optimizer, store = _runtime(args.config)
    portfolio = PaperPortfolio(config.inventory)
    optimizer.inventory = portfolio.inventory
    session_id = uuid4().hex
    trades = 0
    print(json.dumps({
        "event": "paper_session_started",
        "session_id": session_id,
        "inventory": asdict(portfolio.inventory),
    }, default=str, indent=2))
    try:
        monitor = MarketMonitor(config, binance, uniswap, optimizer, store)
        for sample in monitor.run(args.iterations):
            _print_sample(sample)
            observation_id, _pool, _book, result, stale = sample
            if stale or not result.should_trade:
                continue
            portfolio.apply(result.best)
            inventory = asdict(portfolio.inventory)
            trade_id = store.record_paper_trade(
                session_id, observation_id, result.best,
                portfolio.realized_pnl_usdc, inventory,
            )
            trades += 1
            print(json.dumps({
                "event": "paper_fill",
                "paper_trade_id": trade_id,
                "session_id": session_id,
                "direction": result.best.direction.value,
                "quantity_eth": str(result.best.quantity_eth),
                "estimated_profit_usdc": str(result.best.net_profit_usdc),
                "cumulative_pnl_usdc": str(portfolio.realized_pnl_usdc),
                "inventory": inventory,
            }, default=str, indent=2))
            if args.max_trades is not None and trades >= args.max_trades:
                break
    finally:
        print(json.dumps({
            "event": "paper_session_ended",
            "session_id": session_id,
            "paper_trades": trades,
            "cumulative_pnl_usdc": str(portfolio.realized_pnl_usdc),
            "inventory": asdict(portfolio.inventory),
        }, default=str, indent=2))
        store.close()


def cmd_summary(args):
    config = load_config(args.config)
    store = ResearchStore(config.monitor.database_path)
    try:
        print(json.dumps(store.summary(), indent=2))
    finally:
        store.close()


def cmd_pending(args):
    config, binance, uniswap, gas, optimizer, store = _runtime(args.config)
    if not config.ethereum.ws_url:
        raise ValueError("ethereum.ws_url is empty; set ALCHEMY_WS_URL")
    if not config.pending.router_addresses:
        raise ValueError("pending.router_addresses is empty")
    quantities = build_quantity_grid(
        config.strategy.min_quantity_eth, config.strategy.max_quantity_eth,
        config.strategy.quantity_steps, config.strategy.grid,
    )
    simulator = PendingScenarioSimulator(config, gas)

    def process(_tx, swaps):
        if not config.pending.simulate_matching_swaps:
            return
        book = binance.get_order_book()
        fee = binance.get_taker_fee_rate()
        pool = uniswap.get_pool_state()
        for swap in swaps:
            scenario = simulator.simulate(swap, pool, book, fee, quantities)
            store.record_scenario(scenario)
            print(json.dumps({
                "tx_hash": scenario.tx_hash, "valid": scenario.valid,
                "baseline_price": str(scenario.baseline_price),
                "conditional_price": str(scenario.conditional_price),
                "price_change_bps": str(scenario.price_change_bps),
                "reason": scenario.reason,
            }))

    collector = AlchemyPendingCollector(
        config.ethereum.ws_url, config.pending.router_addresses,
        PendingTransactionDecoder(), store, process,
    )
    try:
        asyncio.run(collector.run())
    finally:
        store.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only Uniswap V3 / Binance arbitrage research")
    parser.add_argument("--config", default="config.toml", help="TOML configuration path")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-config", help="copy the sample configuration")
    init.add_argument("destination", nargs="?", default="config.toml")
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=cmd_init)
    once = sub.add_parser("once", help="record one market observation")
    once.set_defaults(handler=cmd_once)
    monitor = sub.add_parser("monitor", help="run the persistent polling monitor")
    monitor.add_argument("--iterations", type=int, default=None)
    monitor.set_defaults(handler=cmd_monitor)
    paper = sub.add_parser("paper", help="run simulated fills with a paper inventory ledger")
    paper.add_argument("--iterations", type=int, default=None)
    paper.add_argument("--max-trades", type=int, default=None)
    paper.set_defaults(handler=cmd_paper)
    pending = sub.add_parser("pending", help="collect/decode pending router transactions")
    pending.set_defaults(handler=cmd_pending)
    summary = sub.add_parser("summary", help="show stored research counts")
    summary.set_defaults(handler=cmd_summary)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        args.handler(args)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        parser.exit(1, "error: {}\n".format(exc))

