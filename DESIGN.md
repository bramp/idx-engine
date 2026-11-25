# Design Doc: Direct Indexing Simulator, Statistical Backtester & Web/CLI Workbench

**Status:** Draft for Implementation

**Primary Language:** Python (`3.12+`, `polars` / `pandas`, `pydantic`, `duckdb` / `sqlite3`, `typer` / `fastapi`)

**UI Layer:** Web UI (React + TypeScript) + CLI (`idx-fetch`, `idx-sim`, `idx-ui`)

**Goal:** Build a deterministic, lot-ledger-backed simulation engine, statistical path/sweep evaluator, and interactive data/simulation workbench to test direct indexing against low-cost S&P 500 ETFs across historical windows, rolling slices, and block-bootstrap paths—without building morning/live execution automation until the simulation evidence justifies it.

---

## 1. Scope & Guiding Rules

### In Scope

* **Shared Python Library (`idx_engine`):** Deterministic simulation loop, tax-lot ledger (`HIFO`/`FIFO`/`specific_lot`), 30-day wash-sale tracking & cost-basis adjustments, corporate actions (`split`, `dividend`, `spin_off`, `ticker_change`), whole-share vs. fractional share sizing, and tax/liquidation accounting.
* **Data Fetcher & Local Store (`idx-fetch`):** Pulls, normalizes, versions, and caches historical prices, dividends, corporate actions, and index constituent weights.
* **Simulation & Statistical Sweep Engine (`idx-sim`):** Runs historical backtests, rolling-window slices, and block-bootstrap Monte Carlo paths across strategy configurations and parameter grids.
* **Web UI & API (`idx-ui` / FastAPI + React):** Inspect cached market/index data, configure and launch simulations/sweeps, visualize return/outperformance distributions and trade-offs, and drill into closed tax lots and wash sales.

### Out of Scope (Deferred)

* **Morning / live order execution:** Deferred until simulation and statistical evaluation demonstrate clear after-tax value over simpler alternatives.

### Rules for Rigorous Analysis

Every simulation run, parameter sweep, and comparison must follow these non-negotiable analytical rules:

1. **Paired Baseline Comparisons:** Always evaluate a direct indexing strategy against a matching low-cost S&P 500 ETF baseline (`etf_baseline`) on the **exact same path, date range, starting capital, and cash-flow schedule**. Never compare a direct-indexing path against an unaligned benchmark.
2. **After-Tax & Liquidation-Adjusted Truth:** Never judge tax-loss harvesting (TLH) on gross pre-tax return or harvested losses alone. Report **realized tax drag**, **loss carryforwards**, and **hypothetical end-of-horizon liquidation tax** on remaining unrealized gains so tax deferral is not mistaken for permanent tax elimination.
3. **Distribution Over Point Estimates:** When using rolling windows or block-bootstrap paths, report **distributions** (median / P50, interquartile range P25–P75, downside P10, and win rate vs. ETF), not just a single mean or best-case run.
4. **No Survivorship Bias Without Explicit Labeling:** Point-in-time constituent membership and weights are preferred for multi-year backtests. If a dataset falls back to current constituents, flag `survivorship_biased=True` on the dataset and block "validated" claims until point-in-time data is used.
5. **Preserve Cross-Sectional & Temporal Structure in Resampling:** When generating synthetic paths, sample **contiguous blocks** across all constituents together so volatility clustering, correlations, and single-stock dispersion stay intact.
6. **Avoid Parameter Overfitting (Plateaus Over Spikes):** Treat parameter sweeps as sensitivity maps. Prefer parameter regions ("plateaus") that remain robust across neighboring TLH thresholds, drift bands, and sample sizes—and validate frozen parameters out-of-sample or across walk-forward splits.
7. **Full Friction & Constraint Accounting:** Account for expense ratios, whole-share rounding vs. fractional precision, uninvested cash drag, tracking error (in bps), and trade/lot volume (a proxy for execution friction and tax-filing burden).
8. **Immutable Inputs & Deterministic Replay:** Pin dataset versions and derive deterministic `run_id`s from `(dataset_version, path_spec, strategy_cfg, tax_cfg, cashflow_cfg)`. Never mutate an existing dataset in place without minting a new version.

---

## 2. Architecture & Design Decisions

### 2.1 Common Library + CLI + Web UI

* **Single `idx_engine` package:** CLI commands (`idx-fetch`, `idx-sim`, `idx-ui`), FastAPI routes, and background worker tasks all call `idx_engine` directly.
* **Explicit clock injection:** Simulation steps and path generators take explicit dates/horizons rather than reading wall-clock system time inside engine loops.
* **Sync reads, async heavy compute:** Dataset/run/ledger queries return synchronously; multi-path simulations and grid sweeps run as background jobs (`202 Accepted` + `job_id` status/progress polling).

### 2.2 Market Data & Index Constituents (`idx-fetch`)

* **Parquet + DuckDB storage:** Store historical bars, dividends, corporate actions, and constituent weights in partitioned **Parquet** files; store run metadata, path-level summaries, distribution stats, closed lots, and wash-sale records in **DuckDB** (or SQLite).
* **Corporate actions & cost basis:** Maintain split-adjusted prices for return calculations alongside explicit corporate action records (`split`, `dividend`, `spin_off`, `ticker_change`) so lot share counts and cost bases adjust cleanly.

### 2.3 Tax-Lot Ledger, Wash Sales & Tax Engine (`ledger/`)

* **Lot-level tracking:** Each acquisition creates a lot (`lot_id`, `ticker`, `sector`, `acquired_date`, `quantity`, `cost_basis_total`, `wash_sale_disallowed`).
* **Lot selection:** Support `HIFO` (default for harvesting losses/minimizing gains), `FIFO`, and `specific_lot`.
* **30-day wash-sale resolution:** Detect loss sales where matching acquisitions (same ticker, plus optional configured substitute rules) occur within `[sale_date - 30 days, sale_date + 30 days]`. Because subsequent purchases within 30 days can affect an earlier loss sale, run a chronological/windowed **wash-sale resolution pass** that disallows/defers the loss and rolls basis into the replacement lot.
* **Tax rates & carryforwards:** Separate short-term ($\le$ 365 days) and long-term ($>$ 365 days) realized gains/losses and dividend taxes; apply federal + state tax rates (e.g., California taxing long-term capital gains as ordinary income), the `$3,000` annual ordinary-income loss offset with carryforwards, and end-of-horizon liquidation tax.

### 2.4 Path Generation (`sim/paths.py`)

Support three path modes feeding the exact same `sim/engine.py` loop:

* `historical`: Replay the historical window directly.
* `rolling_window`: Slice the historical series into overlapping windows of `horizon_days` with stride `step_days`.
* `block_bootstrap`: Sample contiguous blocks of `block_size_days` (e.g., 21 or 63 trading days) with replacement across all constituents simultaneously, assigning synthetic business dates for `num_paths` Monte Carlo runs under a fixed `seed`.

### 2.5 Strategy, Sampling & Whole-Share Constraints (`strategy/`)

* **Comparable strategies:** `etf_baseline` vs. `direct_index` (full S&P 500 or top-$K$stratified sample) with configurable TLH loss % and min$ thresholds, sector substitute routing, and scheduled or drift-based rebalancing.
* **Fractional vs. whole shares:** Toggle `allow_fractional=True/False` to test exact fractional weights against integer share rounding (`floor` + greedy largest-remainder allocation), explicitly logging uninvested cash and tracking error.

### 2.6 Web UI & API (`idx-ui` + FastAPI + React)

* **Data Explorer:** Browse datasets, check date ranges, missing bars, and `survivorship_biased` warnings, and inspect constituent weights and price/dividend series.
* **Simulator & Sweep Workbench:** Configure `PathSpec`, strategy parameters, whole-share constraints, and tax rules; run single backtests or parameter sweeps; inspect **fan/percentile wealth bands**, **paired outperformance vs. ETF ($\Delta W_p$)**, **win rate**, **tracking error (bps)**, and **sweep trade-off plots/tables**.
* **Tax Ledger & Path Drill-Down:** Inspect a selected path (e.g., median, P10, or highest tracking error), filter closed lots and wash-sale adjustments by ticker/year/ST-LT status, and export CSV/JSON.

---

## 3. Code Layout

```text
direct_index_sim/
├── pyproject.toml
├── README.md
├── config/
│   ├── baseline_etf.toml
│   ├── full_tlh_bootstrap.toml
│   ├── top100_whole_shares.toml
│   └── sweep_tlh_and_sample.toml
├── data/                        # gitignored local cache
│   ├── raw/
│   ├── parquet/
│   └── app.duckdb
├── src/
│   └── idx_engine/
│       ├── __init__.py
│       ├── cli.py               # Entrypoints: idx-fetch, idx-sim, idx-ui
│       ├── config.py            # Pydantic configs: Dataset, PathSpec, Strategy, Tax, Sweep
│       ├── models.py            # TaxLot, Fill, CorporateAction, PathSummary, SweepRow
│       ├── fetch/
│       │   ├── __init__.py
│       │   ├── prices.py        # Historical OHLCV + dividends + splits fetcher
│       │   ├── index_weights.py # Point-in-time or tagged constituent weight loader
│       │   └── store.py         # Parquet + DuckDB dataset & run persistence
│       ├── ledger/
│       │   ├── __init__.py
│       │   ├── lots.py          # Lot creation, HIFO/FIFO selection, corporate actions
│       │   ├── wash_sale.py     # 30-day window detection & basis rollover pass
│       │   └── taxes.py         # ST/LT taxes, $3k offset, carryforward, liquidation tax
│       ├── strategy/
│       │   ├── __init__.py
│       │   ├── base.py          # Strategy protocol
│       │   ├── etf_baseline.py  # S&P 500 ETF baseline strategy
│       │   ├── direct_index.py  # Full/sampled S&P 500 + TLH + sector substitute logic
│       │   └── sizing.py        # Fractional vs. integer share sizing & cash allocation
│       └── sim/
│           ├── __init__.py
│           ├── paths.py         # historical, rolling_window, block_bootstrap generators
│           ├── engine.py        # Single-path simulation loop
│           ├── metrics.py       # Paired ETF diffs, P10/P25/P50/P75/P90, TE bps, turnover
│           └── sweep.py         # Multi-parameter grid/scenario runner
├── api/                         # FastAPI service wrapping idx_engine
│   ├── pyproject.toml
│   └── app/
│       ├── main.py
│       ├── jobs.py              # Async job runner for multi-path runs & sweeps
│       ├── schemas.py
│       └── routes/
│           ├── datasets.py
│           ├── runs.py
│           ├── sweeps.py
│           └── ledger.py
├── web/                         # React + TypeScript + Vite UI
│   ├── package.json
│   └── src/
│       ├── api/client.ts
│       ├── components/
│       │   ├── ParameterForm.tsx
│       │   ├── PercentileFanChart.tsx
│       │   ├── PairedDeltaChart.tsx
│       │   ├── SweepComparisonTable.tsx
│       │   └── TaxLotTable.tsx
│       └── pages/
│           ├── DataExplorerPage.tsx
│           ├── SimulatorPage.tsx
│           └── TaxLedgerPage.tsx
└── tests/
    ├── test_lots_hifo.py
    ├── test_wash_sale.py
    ├── test_whole_shares.py
    ├── test_paths_bootstrap.py
    ├── test_paired_metrics.py
    └── test_api_runs.py

```

---

## 4. Core Interfaces & Contracts

```python
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol
import polars as pl

PathMode = Literal["historical", "rolling_window", "block_bootstrap"]
LotPolicy = Literal["HIFO", "FIFO"]

@dataclass(frozen=True)
class PathSpec:
    mode: PathMode
    horizon_days: int = 252 * 10
    step_days: int = 21
    block_size_days: int = 21
    num_paths: int = 500
    seed: int = 42

@dataclass(frozen=True)
class SimulationPath:
    path_id: str
    mode: PathMode
    trading_dates: list[date]
    returns_df: pl.DataFrame
    weights_df: pl.DataFrame
    source_start: date
    source_end: date

@dataclass(frozen=True)
class TaxLot:
    lot_id: str
    ticker: str
    sector: str
    acquired_date: date
    quantity: Decimal
    cost_basis_total: Decimal
    wash_sale_disallowed: Decimal = Decimal("0")

    @property
    def unit_cost(self) -> Decimal:
        return self.cost_basis_total / self.quantity if self.quantity > 0 else Decimal("0")

@dataclass(frozen=True)
class PathOutcome:
    path_id: str
    strategy_name: str
    ending_market_value: Decimal
    ending_cash: Decimal
    after_tax_realized_wealth: Decimal
    after_tax_liquidation_wealth: Decimal
    after_tax_cagr: float
    annualized_tracking_error_bps: float
    annual_turnover: float
    harvested_gross_losses: Decimal
    disallowed_wash_sale_losses: Decimal
    closed_lot_count: int

@dataclass(frozen=True)
class DistributionSummary:
    run_id: str
    dataset_id: str
    strategy_name: str
    benchmark_name: str
    path_count: int
    win_rate_vs_benchmark_liquidation: float
    median_paired_wealth_delta: Decimal
    p10_paired_wealth_delta: Decimal
    p25_paired_wealth_delta: Decimal
    p75_paired_wealth_delta: Decimal
    median_after_tax_cagr: float
    median_tracking_error_bps: float
    p90_tracking_error_bps: float
    median_closed_lots_per_year: float

```

---

## 5. Phased Implementation Plan

1. **Phase 1 — Tax-Lot Ledger, Wash Sales & Unit Tests (`ledger/` + `tests/`):** Implement `TaxLot`, `HIFO`/`FIFO` sales, corporate action adjustments, and 30-day wash-sale resolution, including post-sale repurchases and liquidation tax math.
2. **Phase 2 — Data Store, Path Generators & Paired ETF Baseline (`fetch/`, `paths.py`, `etf_baseline.py`, `engine.py`, `metrics.py`):** Cache historical data, implement `historical` / `rolling_window` / `block_bootstrap` generators, and verify paired ETF vs. direct-index outcome distributions.
3. **Phase 3 — Direct Indexing, Whole Shares & Parameter Sweeps (`direct_index.py`, `sizing.py`, `sweep.py`, `cli.py`):** Add TLH triggers, sector substitutes, integer vs. fractional share modes, and multi-parameter sweeps via `idx-sim`.
4. **Phase 4 — FastAPI Backend & Web UI (`api/`, `web/`, `idx-ui`):** Build dataset inspection, async run/sweep endpoints, percentile fan and paired-delta charts, sweep comparison views, and closed-lot/wash-sale tables.

---

Would you like to write the **Phase 1 ledger & wash-sale code + tests**, the **Phase 2 path generator & paired metrics**, or the **FastAPI + DuckDB schema** next?
