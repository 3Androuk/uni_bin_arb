"""Build the repository's analysis notebooks without requiring nbformat."""

import json
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = ROOT / "notebooks"


def markdown(text):
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": dedent(text).strip().splitlines(keepends=True),
    }


def code(text):
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": dedent(text).strip().splitlines(keepends=True),
    }


def notebook(cells):
    for index, cell in enumerate(cells):
        cell.setdefault("id", f"cell-{index:03d}")
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3 (ipykernel)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.9"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


COMMON_SETUP = r"""
from pathlib import Path
import json
import os
import sqlite3

import matplotlib.pyplot as plt
import pandas as pd

pd.set_option("display.max_columns", 100)
pd.set_option("display.float_format", lambda value: f"{value:,.4f}")
plt.style.use("seaborn-v0_8-whitegrid")


def find_repo_root(start=Path.cwd()):
    # Work whether Jupyter starts in the repository root or notebooks/.
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "scr").exists():
            return candidate
    raise FileNotFoundError("Could not locate the repository root from the current directory")


ROOT = find_repo_root()
DB_PATH = Path(os.environ.get("ARB_DB_PATH", ROOT / "data" / "observations.sqlite3"))
if not DB_PATH.exists():
    raise FileNotFoundError(
        f"No research database at {DB_PATH}. Run `python -m scr --config config.toml monitor` first, "
        "or set ARB_DB_PATH to another observations.sqlite3 file."
    )

# Read-only URI prevents a notebook from accidentally changing the research dataset.
connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
tables = pd.read_sql_query(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    connection,
)["name"].tolist()
print(f"Database: {DB_PATH}")
print("Tables:", ", ".join(tables))
"""


PROFITABILITY = notebook([
    markdown(r"""
    # Profitability, sizing and paper-P&L analysis

    This notebook answers four practical questions:

    1. How often did the monitor find a feasible trade?
    2. Which direction and ETH size produced the best estimated net P&L?
    3. Where did the gross price edge go after Binance fees, Ethereum gas and rebalancing?
    4. What did the simulated inventory ledger record as cumulative paper P&L?

    All values are research marks from stored observations—not guaranteed fills or realized profit. The monitor uses Uniswap QuoterV2 and Binance depth when it creates these rows; this notebook does not re-quote historical markets.
    """),
    code(COMMON_SETUP),
    markdown("## Load and validate the dataset"),
    code(r"""
observations = pd.read_sql_query("SELECT * FROM observations ORDER BY id", connection)
candidates = pd.read_sql_query("SELECT * FROM candidates ORDER BY observation_id, id", connection)
paper_trades = pd.read_sql_query("SELECT * FROM paper_trades ORDER BY session_id, id", connection)

numeric_observation_columns = [
    "market_age_ms", "binance_best_bid", "binance_best_ask", "uniswap_price", "binance_taker_fee"
]
numeric_candidate_columns = [
    "quantity_eth", "gross_buy_usdc", "gross_sell_usdc", "exchange_fee_usdc",
    "gas_cost_usdc", "gas_cost_eth", "rebalancing_cost_usdc", "net_profit_usdc", "profit_bps",
]
for column in numeric_observation_columns:
    observations[column] = pd.to_numeric(observations[column], errors="coerce")
for column in numeric_candidate_columns:
    candidates[column] = pd.to_numeric(candidates[column], errors="coerce")

observations["observed_at"] = pd.to_datetime(observations["observed_at"], utc=True, errors="coerce")
observations["cex_mid"] = (observations["binance_best_bid"] + observations["binance_best_ask"]) / 2
observations["cex_spread_usdc"] = observations["binance_best_ask"] - observations["binance_best_bid"]
observations["cex_spread_bps"] = observations["cex_spread_usdc"] / observations["cex_mid"] * 10_000
observations["dex_minus_cex_usdc"] = observations["uniswap_price"] - observations["cex_mid"]
observations["dex_minus_cex_bps"] = observations["dex_minus_cex_usdc"] / observations["cex_mid"] * 10_000

selected = candidates[candidates["selected"].eq(1)].copy()
selected = selected.merge(
    observations[["id", "observed_at", "cex_mid", "dex_minus_cex_usdc", "dex_minus_cex_bps", "stale"]],
    left_on="observation_id", right_on="id", how="left", suffixes=("", "_observation"),
)

summary = pd.Series({
    "observations": len(observations),
    "candidate quotes": len(candidates),
    "selected opportunities": len(selected),
    "paper trades": len(paper_trades),
    "first observation": observations["observed_at"].min(),
    "last observation": observations["observed_at"].max(),
    "stale observations": int(observations["stale"].sum()) if len(observations) else 0,
    "observations with errors": int(observations["error"].notna().sum()) if len(observations) else 0,
})
summary.to_frame("value")
"""),
    markdown(r"""
    A `selected` row means it was the optimizer's best candidate and cleared its configured constraints. It is still only an opportunity mark. Check stale/error counts before interpreting profitability.
    """),
    markdown("## Market basis and observation quality"),
    code(r"""
if observations.empty:
    print("No observations yet. Run the monitor before analysing profitability.")
else:
    display(observations[[
        "market_age_ms", "cex_spread_usdc", "cex_spread_bps",
        "dex_minus_cex_usdc", "dex_minus_cex_bps",
    ]].describe(percentiles=[.05, .25, .5, .75, .95]).T)

    axes = observations.plot(
        x="observed_at", y=["cex_mid", "uniswap_price"], figsize=(12, 4),
        title="Observed ETH prices: Binance mid vs Uniswap spot",
    )
    axes.set_ylabel("USDC per ETH")
    plt.show()

    axes = observations.plot(
        x="observed_at", y="dex_minus_cex_usdc", figsize=(12, 3),
        title="Raw Uniswap minus Binance-mid basis (before costs)", legend=False,
    )
    axes.axhline(0, color="black", linewidth=1)
    axes.set_ylabel("USDC per ETH")
    plt.show()
"""),
    markdown("## Selected opportunity frequency, direction and size"),
    code(r"""
if selected.empty:
    print("No selected opportunities. This can be a valid result when fees and gas exceed the raw spread.")
else:
    elapsed_hours = max(
        (observations["observed_at"].max() - observations["observed_at"].min()).total_seconds() / 3600,
        1 / 3600,
    )
    opportunity_summary = pd.Series({
        "selected opportunities": len(selected),
        "share of observations": len(selected) / max(len(observations), 1),
        "opportunities per observed hour": len(selected) / elapsed_hours,
        "median size ETH": selected["quantity_eth"].median(),
        "median net profit USDC": selected["net_profit_usdc"].median(),
        "total marked profit USDC (not additive fills)": selected["net_profit_usdc"].sum(),
    })
    display(opportunity_summary.to_frame("value"))
    display(selected.groupby("direction").agg(
        opportunities=("id", "size"),
        median_size_eth=("quantity_eth", "median"),
        median_profit_usdc=("net_profit_usdc", "median"),
        p95_profit_usdc=("net_profit_usdc", lambda values: values.quantile(.95)),
    ))

    fig, axes = plt.subplots(1, 2, figsize=(13, 4))
    for direction, group in selected.groupby("direction"):
        axes[0].scatter(group["quantity_eth"], group["net_profit_usdc"], s=18, alpha=.6, label=direction)
        axes[1].plot(group["observed_at"], group["net_profit_usdc"], ".", alpha=.6, label=direction)
    axes[0].set(xlabel="Selected quantity (ETH)", ylabel="Estimated net profit (USDC)", title="Profit vs optimal size")
    axes[1].set(xlabel="Time", ylabel="Estimated net profit (USDC)", title="Selected opportunities over time")
    axes[0].legend(); axes[1].legend(); fig.autofmt_xdate(); plt.show()
"""),
    markdown(r"""
    The sum of selected marks is **not** a trading return: adjacent observations can describe the same short-lived opportunity. Use `paper_trades` for the system's simulated fill ledger and the latency notebook for opportunity survival.
    """),
    markdown("## Cost decomposition and break-even spread"),
    code(r"""
analysis = selected.copy()
if analysis.empty:
    print("Cost decomposition requires at least one selected opportunity.")
else:
    analysis["gross_edge_usdc"] = analysis["gross_sell_usdc"] - analysis["gross_buy_usdc"]
    analysis["total_cost_usdc"] = (
        analysis["exchange_fee_usdc"] + analysis["gas_cost_usdc"] + analysis["rebalancing_cost_usdc"]
    )
    analysis["break_even_edge_per_eth_usdc"] = analysis["total_cost_usdc"] / analysis["quantity_eth"]
    analysis["gross_edge_per_eth_usdc"] = analysis["gross_edge_usdc"] / analysis["quantity_eth"]
    analysis["gas_per_eth_usdc"] = analysis["gas_cost_usdc"] / analysis["quantity_eth"]
    analysis["exchange_fee_per_eth_usdc"] = analysis["exchange_fee_usdc"] / analysis["quantity_eth"]

    display(analysis[[
        "gross_edge_usdc", "exchange_fee_usdc", "gas_cost_usdc", "rebalancing_cost_usdc",
        "net_profit_usdc", "break_even_edge_per_eth_usdc",
    ]].describe(percentiles=[.1, .5, .9]).T)

    cost_means = analysis[["exchange_fee_usdc", "gas_cost_usdc", "rebalancing_cost_usdc"]].mean()
    cost_means.plot(kind="bar", figsize=(8, 3), title="Mean cost per selected opportunity")
    plt.ylabel("USDC"); plt.xticks(rotation=20); plt.show()

    axes = analysis.plot.scatter(
        x="quantity_eth", y="break_even_edge_per_eth_usdc", c="gas_cost_usdc",
        colormap="viridis", figsize=(9, 4), title="Required price edge per ETH after modeled costs",
    )
    axes.set(xlabel="ETH quantity", ylabel="Break-even CEX–DEX difference (USDC/ETH)")
    plt.show()
"""),
    markdown("## Adaptive optimizer diagnostics"),
    code(r"""
feasible = candidates[candidates["feasible"].eq(1)].copy()
if feasible.empty:
    print("No feasible candidate rows to inspect.")
else:
    size_profile = feasible.groupby(["direction", "quantity_eth"], as_index=False).agg(
        samples=("id", "size"), median_profit=("net_profit_usdc", "median"),
        profitable_share=("net_profit_usdc", lambda values: values.gt(0).mean()),
    )
    for direction, group in size_profile.groupby("direction"):
        plt.plot(group["quantity_eth"], group["median_profit"], marker=".", label=direction)
    plt.axhline(0, color="black", linewidth=1)
    plt.xscale("log")
    plt.xlabel("Quoted quantity (ETH, log scale)")
    plt.ylabel("Median net profit (USDC)")
    plt.title("Quoted sizing surface across stored observations")
    plt.legend(); plt.show()

    display(size_profile.sort_values(["direction", "median_profit"], ascending=[True, False]).groupby("direction").head(10))
"""),
    markdown("## Paper-trading ledger"),
    code(r"""
if paper_trades.empty:
    print("No paper trades recorded. Run `python -m scr --config config.toml paper` to populate this section.")
else:
    for column in ["quantity_eth", "estimated_profit_usdc", "cumulative_pnl_usdc"]:
        paper_trades[column] = pd.to_numeric(paper_trades[column], errors="coerce")
    paper_trades["executed_at"] = pd.to_datetime(paper_trades["executed_at"], utc=True, errors="coerce")

    session_summary = paper_trades.groupby("session_id").agg(
        trades=("id", "size"),
        started_at=("executed_at", "min"),
        ended_at=("executed_at", "max"),
        final_cumulative_pnl=("cumulative_pnl_usdc", "last"),
        marked_trade_profit=("estimated_profit_usdc", "sum"),
    ).sort_values("ended_at", ascending=False)
    display(session_summary)

    for session_id, group in paper_trades.groupby("session_id"):
        plt.plot(group["executed_at"], group["cumulative_pnl_usdc"], marker=".", label=session_id[:8])
    plt.xlabel("Time"); plt.ylabel("Cumulative paper P&L (USDC)")
    plt.title("Paper P&L by session"); plt.legend(title="Session"); plt.show()

    latest_inventory = json.loads(paper_trades.iloc[-1]["inventory_json"])
    display(pd.Series(latest_inventory, name="latest stored inventory"))
"""),
    markdown(r"""
    ## Interpretation checklist

    - Prefer net profit and break-even edge over the raw CEX–DEX price difference.
    - Treat repeated adjacent marks as one market episode, not independent trades.
    - Exclude stale/error observations before drawing conclusions.
    - Check latency survival before assuming a detected mark could have been captured.
    - Paper P&L uses hypothetical immediate fills and configured costs; it is not exchange-account P&L.
    """),
])


LATENCY = notebook([
    markdown(r"""
    # Latency survival and two-market movement analysis

    This notebook measures what happens between detection and a delayed reprice. It separates the movement of the Binance leg from the movement of the Uniswap leg, which is essential: an opportunity survives when the **relative** move remains favorable, not merely when ETH rises or falls.

    A survived signal is still a paper result. It does not model transaction propagation, builder ordering, partial CEX fills or failed/reverted on-chain execution.
    """),
    code(COMMON_SETUP),
    markdown("## Join signals, delayed evaluations and both market snapshots"),
    code(r"""
query = '''
SELECT
    s.id AS signal_id, s.observation_id AS original_observation_id,
    s.created_at, s.due_at, s.delay_ms AS requested_delay_ms, s.direction, s.quantity_eth,
    s.expected_profit_usdc, s.status,
    e.evaluated_observation_id, e.evaluated_at, e.actual_delay_ms,
    e.repriced_profit_usdc, e.repriced_profit_bps, e.survived, e.error,
    original.binance_best_bid AS original_bid,
    original.binance_best_ask AS original_ask,
    original.uniswap_price AS original_dex,
    evaluated.binance_best_bid AS evaluated_bid,
    evaluated.binance_best_ask AS evaluated_ask,
    evaluated.uniswap_price AS evaluated_dex
FROM paper_signals s
LEFT JOIN latency_evaluations e ON e.signal_id = s.id
LEFT JOIN observations original ON original.id = s.observation_id
LEFT JOIN observations evaluated ON evaluated.id = e.evaluated_observation_id
ORDER BY s.id
'''
latency = pd.read_sql_query(query, connection)

numeric_columns = [
    "requested_delay_ms", "quantity_eth", "expected_profit_usdc", "actual_delay_ms",
    "repriced_profit_usdc", "repriced_profit_bps", "survived", "original_bid", "original_ask",
    "original_dex", "evaluated_bid", "evaluated_ask", "evaluated_dex",
]
for column in numeric_columns:
    latency[column] = pd.to_numeric(latency[column], errors="coerce")
for column in ["created_at", "due_at", "evaluated_at"]:
    latency[column] = pd.to_datetime(latency[column], utc=True, errors="coerce")

evaluated = latency[latency["evaluated_at"].notna()].copy()
coverage = pd.Series({
    "scheduled signals": len(latency),
    "evaluated signals": len(evaluated),
    "pending/unevaluated": len(latency) - len(evaluated),
    "evaluation coverage": len(evaluated) / max(len(latency), 1),
    "evaluation errors": int(evaluated["error"].notna().sum()) if len(evaluated) else 0,
})
coverage.to_frame("value")
"""),
    markdown("## Requested versus actual delay"),
    code(r"""
if evaluated.empty:
    print("No delayed evaluations yet. Keep the monitor running long enough to reach configured delay horizons.")
else:
    evaluated["delay_overshoot_ms"] = evaluated["actual_delay_ms"] - evaluated["requested_delay_ms"]
    display(evaluated.groupby("requested_delay_ms").agg(
        samples=("signal_id", "size"),
        median_actual_ms=("actual_delay_ms", "median"),
        p95_actual_ms=("actual_delay_ms", lambda values: values.quantile(.95)),
        median_overshoot_ms=("delay_overshoot_ms", "median"),
    ))

    axes = evaluated.plot.scatter(
        x="requested_delay_ms", y="actual_delay_ms", alpha=.5, figsize=(7, 5),
        title="Requested delay vs actual observation-based reprice delay",
    )
    limit = max(evaluated["requested_delay_ms"].max(), evaluated["actual_delay_ms"].max())
    axes.plot([0, limit], [0, limit], "k--", linewidth=1, label="on time")
    axes.legend(); plt.show()
"""),
    markdown(r"""
    The monitor can only evaluate on a later observation. With a five-second polling interval, a requested 250 ms or 1 s horizon will generally be measured at the next poll, not at the exact requested timestamp. `actual_delay_ms` is therefore the correct x-axis for timing conclusions.
    """),
    markdown("## Survival curve and profit decay"),
    code(r"""
if not evaluated.empty:
    evaluated["profit_change_usdc"] = evaluated["repriced_profit_usdc"] - evaluated["expected_profit_usdc"]
    by_horizon = evaluated.groupby("requested_delay_ms").agg(
        samples=("signal_id", "size"),
        survival_rate=("survived", "mean"),
        median_expected_profit=("expected_profit_usdc", "median"),
        median_repriced_profit=("repriced_profit_usdc", "median"),
        median_profit_change=("profit_change_usdc", "median"),
        p10_repriced_profit=("repriced_profit_usdc", lambda values: values.quantile(.10)),
    )
    display(by_horizon)

    fig, axes = plt.subplots(1, 2, figsize=(13, 4))
    axes[0].plot(by_horizon.index, by_horizon["survival_rate"], marker="o")
    axes[0].set(xlabel="Requested delay (ms)", ylabel="Survival probability", ylim=(-.05, 1.05), title="Opportunity survival")
    axes[1].plot(by_horizon.index, by_horizon["median_expected_profit"], marker="o", label="At detection")
    axes[1].plot(by_horizon.index, by_horizon["median_repriced_profit"], marker="o", label="After delay")
    axes[1].axhline(0, color="black", linewidth=1)
    axes[1].set(xlabel="Requested delay (ms)", ylabel="Median profit (USDC)", title="Profit decay")
    axes[1].legend(); plt.show()
"""),
    markdown("## Attribute the change to Binance and Uniswap"),
    code(r"""
if not evaluated.empty:
    evaluated["cex_leg_move_bps"] = 0.0
    evaluated["dex_leg_move_bps"] = 0.0
    buy_dex = evaluated["direction"].eq("buy_uniswap_sell_binance")
    buy_cex = evaluated["direction"].eq("buy_binance_sell_uniswap")

    # Positive means the sale venue improved or the purchase venue became cheaper.
    evaluated.loc[buy_dex, "cex_leg_move_bps"] = (
        evaluated.loc[buy_dex, "evaluated_bid"] / evaluated.loc[buy_dex, "original_bid"] - 1
    ) * 10_000
    evaluated.loc[buy_dex, "dex_leg_move_bps"] = -(
        evaluated.loc[buy_dex, "evaluated_dex"] / evaluated.loc[buy_dex, "original_dex"] - 1
    ) * 10_000

    evaluated.loc[buy_cex, "cex_leg_move_bps"] = -(
        evaluated.loc[buy_cex, "evaluated_ask"] / evaluated.loc[buy_cex, "original_ask"] - 1
    ) * 10_000
    evaluated.loc[buy_cex, "dex_leg_move_bps"] = (
        evaluated.loc[buy_cex, "evaluated_dex"] / evaluated.loc[buy_cex, "original_dex"] - 1
    ) * 10_000
    evaluated["relative_leg_move_bps"] = evaluated["cex_leg_move_bps"] + evaluated["dex_leg_move_bps"]

    display(evaluated.groupby(["requested_delay_ms", "survived"])[[
        "cex_leg_move_bps", "dex_leg_move_bps", "relative_leg_move_bps", "profit_change_usdc"
    ]].median())

    colors = evaluated["survived"].map({1: "tab:green", 0: "tab:red"})
    plt.figure(figsize=(7, 6))
    plt.scatter(evaluated["cex_leg_move_bps"], evaluated["dex_leg_move_bps"], c=colors, alpha=.55)
    plt.axhline(0, color="black", linewidth=.8); plt.axvline(0, color="black", linewidth=.8)
    plt.xlabel("Favorable Binance-leg move (bps)")
    plt.ylabel("Favorable Uniswap-leg move (bps)")
    plt.title("Both market movements determine opportunity survival")
    plt.show()
"""),
    markdown(r"""
    In the movement chart, upper/right movements help the original trade and lower/left movements hurt it. This direction-aware transformation avoids the mistake of calling a general ETH price rise favorable or unfavorable without considering which venue was the buy leg and which was the sell leg.
    """),
    markdown("## Direction, size and delay risk"),
    code(r"""
if not evaluated.empty:
    display(evaluated.groupby(["direction", "requested_delay_ms"]).agg(
        samples=("signal_id", "size"),
        survival_rate=("survived", "mean"),
        median_quantity_eth=("quantity_eth", "median"),
        median_profit_change=("profit_change_usdc", "median"),
    ))

    evaluated["size_bucket"] = pd.qcut(evaluated["quantity_eth"], q=min(4, evaluated["quantity_eth"].nunique()), duplicates="drop")
    display(evaluated.groupby("size_bucket", observed=True).agg(
        samples=("signal_id", "size"), survival_rate=("survived", "mean"),
        median_expected_profit=("expected_profit_usdc", "median"),
        median_repriced_profit=("repriced_profit_usdc", "median"),
    ))
"""),
    markdown(r"""
    ## Interpretation checklist

    - Use actual delay, not only requested delay.
    - Require enough samples per horizon before comparing survival rates.
    - Inspect Binance and Uniswap movements separately; correlated ETH movement can hide a changing cross-venue basis.
    - A delayed positive quote does not prove atomic execution or fillability.
    - The monitor's polling cadence limits the resolution of this evaluation.
    """),
])


PENDING = notebook([
    markdown(r"""
    # Pending-transaction decoding and conditional scenario research

    This notebook now has two useful modes:

    1. **Collected-data mode** analyses decoded pending transactions and the conditional scenarios written by `python -m scr --config config.toml pending`.
    2. **Sensitivity mode** runs even when the pending tables are empty. It asks how hypothetical Uniswap price shocks would change the observed CEX–DEX basis. This is a diagnostic—not a claim that a particular pending swap will execute.

    The collector sees only provider-visible public order flow. Private bundles, inclusion, ordering, replacement, cancellation and reverts remain unknown.
    """),
    code(COMMON_SETUP),
    markdown("## Collection and decoding funnel"),
    code(r"""
pending_transactions = pd.read_sql_query(
    "SELECT * FROM pending_transactions ORDER BY observed_at", connection
)
scenarios = pd.read_sql_query(
    "SELECT * FROM pending_scenarios ORDER BY created_at", connection
)
observations = pd.read_sql_query(
    "SELECT id, observed_at, binance_best_bid, binance_best_ask, uniswap_price FROM observations ORDER BY id",
    connection,
)

for frame, column in [(pending_transactions, "observed_at"), (scenarios, "created_at"), (observations, "observed_at")]:
    if column in frame:
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
for column in ["binance_best_bid", "binance_best_ask", "uniswap_price"]:
    observations[column] = pd.to_numeric(observations[column], errors="coerce")
for column in ["baseline_price", "conditional_price", "price_change_bps", "valid"]:
    if column in scenarios:
        scenarios[column] = pd.to_numeric(scenarios[column], errors="coerce")

funnel = pd.Series({
    "pending transactions seen": len(pending_transactions),
    "transactions decoded": int(pending_transactions["decoded"].sum()) if len(pending_transactions) else 0,
    "decode errors": int(pending_transactions["error"].notna().sum()) if len(pending_transactions) else 0,
    "conditional scenarios": len(scenarios),
    "valid scenarios": int(scenarios["valid"].sum()) if len(scenarios) else 0,
})
display(funnel.to_frame("count"))

if pending_transactions.empty:
    print("No pending transactions have been stored yet.")
    print("To collect them, configure ALCHEMY_WSS_URL in .env and run:")
    print("  python -m scr --config config.toml pending")
"""),
    markdown("## Inspect decoded swaps"),
    code(r"""
decoded_swaps = []
for row in pending_transactions.itertuples(index=False):
    try:
        swaps = json.loads(row.swaps_json or "[]")
    except json.JSONDecodeError:
        swaps = []
    for swap in swaps:
        decoded_swaps.append({
            "tx_hash": row.tx_hash,
            "observed_at": row.observed_at,
            "router": row.router,
            "selector": row.selector,
            **swap,
        })
decoded_swaps = pd.DataFrame(decoded_swaps)

if decoded_swaps.empty:
    print("No supported SwapRouter/Universal Router V3 swaps decoded yet.")
else:
    decoded_swaps["route"] = decoded_swaps.apply(
        lambda row: f"{str(row['token_in'])[:8]}… → {str(row['token_out'])[:8]}…", axis=1
    )
    display(decoded_swaps.groupby(["exact_input", "fee", "route"]).size().rename("swaps").sort_values(ascending=False).head(20))
    display(decoded_swaps[[
        "observed_at", "tx_hash", "selector", "command_index", "exact_input", "fee",
        "amount_specified_raw", "amount_bound_raw", "route",
    ]].tail(20))
"""),
    markdown(r"""
    Raw amounts are intentionally not converted here unless the token and its decimals are known. Treating every path token as USDC or WETH would create misleading notionals.
    """),
    markdown("## Conditional scenario validity and price impact"),
    code(r"""
if scenarios.empty:
    print("No conditional scenarios yet; continue to the sensitivity experiment below.")
else:
    scenarios["abs_price_change_bps"] = scenarios["price_change_bps"].abs()
    display(scenarios.groupby(["valid", "reason"], dropna=False).size().rename("scenarios").sort_values(ascending=False))

    valid = scenarios[scenarios["valid"].eq(1)].copy()
    if valid.empty:
        print("Scenarios were recorded, but none passed the simulator's direct-pool/fee/notional checks.")
    else:
        display(valid["price_change_bps"].describe(percentiles=[.01, .05, .5, .95, .99]).to_frame())
        valid.plot(
            x="created_at", y="price_change_bps", kind="scatter", figsize=(12, 4),
            title="Conditional Uniswap price change if each observed swap executed next",
        )
        plt.axhline(0, color="black", linewidth=1); plt.ylabel("Price change (bps)"); plt.show()
        display(valid.nlargest(20, "abs_price_change_bps")[[
            "created_at", "tx_hash", "baseline_price", "conditional_price", "price_change_bps"
        ]])
"""),
    markdown("## Conditional arbitrage candidates"),
    code(r"""
conditional_candidates = []
for row in scenarios.itertuples(index=False):
    if not row.candidate_json:
        continue
    try:
        candidate = json.loads(row.candidate_json)
    except json.JSONDecodeError:
        continue
    conditional_candidates.append({
        "tx_hash": row.tx_hash,
        "created_at": row.created_at,
        "price_change_bps": float(row.price_change_bps),
        **candidate,
    })
conditional_candidates = pd.DataFrame(conditional_candidates)

if conditional_candidates.empty:
    print("No post-scenario candidate was profitable and feasible, or no live scenarios have been collected.")
else:
    for column in [
        "quantity_eth", "gross_buy_usdc", "gross_sell_usdc", "exchange_fee_usdc",
        "gas_cost_usdc", "rebalancing_cost_usdc", "net_profit_usdc", "profit_bps",
    ]:
        conditional_candidates[column] = pd.to_numeric(conditional_candidates[column], errors="coerce")
    display(conditional_candidates.groupby("direction").agg(
        candidates=("tx_hash", "size"),
        median_quantity_eth=("quantity_eth", "median"),
        median_profit_usdc=("net_profit_usdc", "median"),
        max_profit_usdc=("net_profit_usdc", "max"),
    ))
    axes = conditional_candidates.plot.scatter(
        x="price_change_bps", y="net_profit_usdc", c="quantity_eth", colormap="viridis",
        figsize=(9, 5), title="Conditional price impact vs modeled arbitrage profit",
    )
    axes.axhline(0, color="black", linewidth=1); plt.show()
"""),
    markdown("## Sensitivity experiment when the pending tables are empty"),
    code(r"""
# This experiment is deliberately independent of pending_transactions, so the notebook produces
# a useful result before the collector has observed its first supported swap.
if observations.empty:
    print("No market observations are available for sensitivity analysis. Run the monitor first.")
else:
    market = observations.dropna().tail(min(500, len(observations))).copy()
    market["cex_mid"] = (market["binance_best_bid"] + market["binance_best_ask"]) / 2
    shock_grid_bps = list(range(-100, 101, 5))
    rows = []
    for shock_bps in shock_grid_bps:
        shocked_dex = market["uniswap_price"] * (1 + shock_bps / 10_000)
        shocked_basis_bps = (shocked_dex / market["cex_mid"] - 1) * 10_000
        rows.append({
            "hypothetical_dex_shock_bps": shock_bps,
            "median_post_shock_basis_bps": shocked_basis_bps.median(),
            "p05_basis_bps": shocked_basis_bps.quantile(.05),
            "p95_basis_bps": shocked_basis_bps.quantile(.95),
            "share_abs_basis_above_10bps": shocked_basis_bps.abs().gt(10).mean(),
        })
    sensitivity = pd.DataFrame(rows)
    display(sensitivity.iloc[::4].reset_index(drop=True))

    plt.figure(figsize=(10, 5))
    plt.plot(sensitivity["hypothetical_dex_shock_bps"], sensitivity["median_post_shock_basis_bps"], label="Median")
    plt.fill_between(
        sensitivity["hypothetical_dex_shock_bps"], sensitivity["p05_basis_bps"],
        sensitivity["p95_basis_bps"], alpha=.2, label="5th–95th percentile across observations",
    )
    plt.axhline(0, color="black", linewidth=1)
    plt.xlabel("Hypothetical pending-swap effect on Uniswap (bps)")
    plt.ylabel("Post-shock Uniswap minus Binance-mid basis (bps)")
    plt.title("How a conditional DEX move would alter the observed cross-venue basis")
    plt.legend(); plt.show()
"""),
    markdown(r"""
    The sensitivity chart is **not** a swap simulator and does not include size-dependent QuoterV2 execution, gas, fees or CEX depth. It simply verifies the direction and scale by which a hypothetical DEX-only shock would alter the cross-venue basis. Actual conditional candidates are produced by the application and analysed in the preceding section.
    """),
    markdown("## Limitations recorded with the scenarios"),
    code(r"""
limitations = []
for raw in scenarios.get("limitations_json", pd.Series(dtype=str)).dropna():
    try:
        limitations.extend(json.loads(raw))
    except json.JSONDecodeError:
        pass

if limitations:
    display(pd.Series(limitations).value_counts().rename_axis("limitation").to_frame("scenario rows"))
else:
    display(pd.DataFrame({"limitations that apply before collection": [
        "The provider-visible public mempool is incomplete and excludes private order flow.",
        "Inclusion, success, replacement, cancellation and transaction ordering are unknown.",
        "The local V3 scenario assumes the swap stays in the current active liquidity range.",
        "Competing searchers, builders and MEV bundles are not simulated.",
        "A conditional post-swap quote is not a prediction or executable signal.",
    ]}))
"""),
    markdown(r"""
    ## What would make this predictive rather than merely conditional?

    A serious CEX-price prediction study needs labelled pairs of pending-flow features and future Binance returns, strict event-time alignment, a train/validation/test split by time, and baselines such as current CEX momentum and contemporaneous DEX–CEX basis. It must measure incremental out-of-sample value after latency and costs. The current scenario system is useful for generating hypotheses and conditional features, but it cannot establish causality or forecastability by itself.
    """),
])


OUTPUTS = {
    "01_profitability_analysis.ipynb": PROFITABILITY,
    "02_latency_survival.ipynb": LATENCY,
    "03_pending_scenarios.ipynb": PENDING,
}


def main():
    NOTEBOOKS.mkdir(parents=True, exist_ok=True)
    for name, payload in OUTPUTS.items():
        path = NOTEBOOKS / name
        path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({len(payload['cells'])} cells)")


if __name__ == "__main__":
    main()
