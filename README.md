# Uniswap V3–Binance arbitrage research

A read-only, paper-trading research system for the Ethereum mainnet Uniswap V3 USDC/WETH 0.05% pool and Binance `ETHUSDC` spot market. It measures instantaneous CEX–DEX opportunities; it does **not** place Binance orders, sign Ethereum transactions, or claim that quoted profits are achievable.

## What is implemented

- Bidirectional adaptive optimization:
  - buy exact WETH on Uniswap through QuoterV2, sell into Binance bids;
  - buy WETH through Binance asks, sell exact WETH through Uniswap QuoterV2.
- Uniswap fees, concentrated-liquidity price impact and tick crossings via QuoterV2.
- Binance order-book depth and account-specific taker fees through the optional signed, read-only `tradeFee` endpoint, with a configured fallback.
- EIP-1559 gas estimates using current base/priority fees, QuoterV2 gas, a configurable multiplier and router overhead.
- Venue inventories, native ETH gas balance, minimum profit, profit-bps and explicit rebalancing assumptions.
- A persistent SQLite monitor that stores all observations and all candidates, including no-trade periods.
- Latency paper evaluation: selected candidates are repriced after configured delays and recorded as survived/failed opportunities.
- Alchemy pending-transaction collection for configured router addresses, direct SwapRouter decoding and Universal Router V3 command/path decoding.
- Conditional pending-swap scenarios using a local, single-active-liquidity-range V3 model with limitations attached to every result.

The live monitor starts with seven geometrically spaced quantities in each direction, then performs two local refinement rounds around each direction's best result. This normally requires about 26 QuoterV2 calls instead of 60 calls for the former fixed 30-size grid. Set `search_mode = "exhaustive"` to restore the full configured grid for comparison.

The public Binance market-data endpoint follows Binance's [official market-data-only documentation](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md). Pending subscriptions follow [Alchemy's `alchemy_pendingTransactions` documentation](https://www.alchemy.com/docs/reference/alchemy-pendingtransactions), and Universal Router command decoding follows [Uniswap's command reference](https://developers.uniswap.org/docs/protocols/universal-router/concepts/commands).

## Quick start

Python 3.9 or newer is supported.

```bash
cd /Users/tigrou/Github/uni_bin_arb
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
cp config.example.toml config.toml
```

Put an Ethereum mainnet HTTP endpoint in `.env`. Add an Alchemy WebSocket endpoint only if you intend to run the pending collector. Binance credentials are optional and are used solely to read the account's taker fee; without them, `fallback_taker_fee_bps` is used.

```bash
# Offline unit tests
python -m pytest

# One live, stored observation
python -m scr --config config.toml once

# Poll until interrupted; every sample and candidate goes to SQLite
python -m scr --config config.toml monitor

# Paper trading: simulate fills and update a session inventory ledger
python -m scr --config config.toml paper

# A bounded smoke run
python -m scr --config config.toml monitor --iterations 2

# Dataset counts
python -m scr --config config.toml summary

# Optional, high-volume experimental collector
python -m scr --config config.toml pending
```

`python scr/app/main.py ...` is also supported. Installing the repository with `python -m pip install -e .` additionally creates the `uni-bin-arb` command.

## Configuration and cost model

`config.example.toml` documents every input. For a WETH quantity `q`:

```text
buy Uniswap / sell Binance:
  net = Binance bid-depth proceeds - Binance taker fee
        - QuoterV2 exact-output USDC cost - dynamic gas - rebalance cost

buy Binance / sell Uniswap:
  net = QuoterV2 exact-input USDC proceeds
        - Binance ask-depth cost - Binance taker fee - dynamic gas - rebalance cost
```

QuoterV2 already includes the Uniswap pool fee and deterministic price impact; the optimizer does not subtract that fee twice. It separately charges gas, Binance fees and the configured rebalancing assumption.

The CEX model conservatively charges the taker commission in quote currency. Accounts configured to pay fees in BNB or receive rebates need a custom fee model; the stored fee and itemized P&L make that assumption auditable.

Inventory constraints assume pre-positioned assets so the two legs can be evaluated without waiting for a cross-venue transfer. Rebalancing is represented as a cost, not as an implemented transfer. Paper fills are marks, not executions.

The `paper` command starts from the configured inventories, applies every fresh selected candidate as an immediate hypothetical fill, updates both venue balances and native gas inventory, and stores the result. Each invocation starts a new in-memory portfolio session; the trades remain available in SQLite under their session ID.

The supplied configuration polls every five seconds, requests 100 Binance depth levels, and requires at least $0.10 estimated net profit. The separate basis-point threshold is disabled, so `$0.10` is the effective profit floor at every size.

The default quantity search range is 0.05–10 ETH. Change `min_quantity_eth` and `max_quantity_eth` in `config.toml` if a different inventory range is required.

## Stored data

The SQLite database contains:

- `observations`: timestamps, block, pool price, best CEX prices, freshness and fee;
- `candidates`: every direction/quantity and its itemized P&L;
- `paper_signals` and `latency_evaluations`: original vs delayed marks;
- `paper_trades`: simulated fills, cumulative session P&L and post-fill inventories;
- `pending_transactions`: raw and decoded provider-visible transactions;
- `pending_scenarios`: counterfactual prices, decisions and limitations.

SQLite uses WAL mode so notebooks can read while the monitor writes. `notebooks/01_profitability_analysis.ipynb` is a starter analysis.

## Pending-transaction limitations

This part is intentionally labelled experimental:

1. There is no global public mempool. Alchemy documents that its stream only includes transactions visible to its infrastructure; private transactions and bundles are absent.
2. A seen transaction may be replaced, cancelled, reordered, revert, miss the next block, or execute against changed state.
3. Router-address filtering misses swaps sent through aggregators or unlisted/new router deployments.
4. The decoder covers common direct V3 SwapRouter calls and Universal Router V3 exact-in/out commands. Unsupported commands are stored but not simulated.
5. The conditional model assumes the swap stays inside the currently active Uniswap V3 liquidity range. It does not load initialized ticks, run a stateful EVM fork, or execute arbitrary calldata.
6. A conditional post-swap discrepancy can be removed by builders, searchers or competing arbitrage before any hypothetical strategy can act.

For research-grade stateful counterfactuals, replay selected transactions on an archival local EVM fork at a pinned block and enumerate orderings. Even that would remain conditional—not a forecast of builder ordering.

## Project layout

```text
scr/
  data/
    binance.py          public depth + optional read-only fee lookup
    uniswap.py          pool state + QuoterV2 calls
  arb/
    optimizer.py        bidirectional sizing and constraints
    gas.py              live EIP-1559 cost model
  app/
    cli.py              command-line application
    config.py           TOML/environment configuration
    models.py           shared data models
    monitor.py          persistent observations + delayed repricing
    pending.py          collection, decoding and conditional simulation
    storage.py          SQLite research schema
    paper.py            optional hypothetical inventory ledger
tests/                  offline deterministic tests
notebooks/              reproducible analysis starters
```

## Safety

The codebase has no private-key loader, signer, `eth_sendRawTransaction`, Binance order endpoint or withdrawal endpoint. Treat RPC/API keys as secrets, keep `.env` out of version control, use read-only Binance keys with IP restrictions, and set provider usage limits before running a broad pending stream.

