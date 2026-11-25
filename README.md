# idx-engine: Direct Indexing Simulator, Statistical Backtester & Web/CLI Workbench

> **Repository initialization in progress:** This README describes the full
> working project. Some application components are not yet checked in while
> they await review, so a fresh clone may not support all workflows below.

Deterministic tax-lot-backed S&P 500 direct indexing simulator, statistical path/sweep evaluator, and interactive FastAPI + React workbench designed to test direct indexing against low-cost S&P 500 ETFs (`VOO` / `SPY`) across historical windows, rolling slices, and cross-sectional block-bootstrap paths.

---

## Table of Contents

1. [Highlights & Analytical Guardrails](#highlights--analytical-guardrails)
2. [Prerequisites & Installation](#prerequisites--installation)
3. [End-to-End Quickstart](#end-to-end-quickstart)
4. [CLI Reference (`idx-fetch`, `idx-sim`, `idx-ui`)](#cli-reference-idx-fetch-idx-sim-idx-ui)
5. [TOML Configuration Reference](#toml-configuration-reference)
6. [Importing Custom Point-in-Time CSV / Parquet Datasets](#importing-custom-point-in-time-csv--parquet-datasets)
7. [Using the Web Workbench (`idx-ui`)](#using-the-web-workbench-idx-ui)
8. [Repository Layout](#repository-layout)
9. [Development & Testing](#development--testing)

---

## Highlights & Analytical Guardrails

1. **Paired Baseline Fairness:** Every direct-indexing run executes side-by-side against an aligned S&P 500 ETF baseline (`etf_baseline`) on the exact same `SimulationPath`, dates, starting capital, cash-flow schedule, and tax rules.
2. **After-Tax & Liquidation-Adjusted Truth:** Reports both pre-liquidation after-tax wealth and **hypothetical end-of-horizon full liquidation wealth** (accounting for unrealized ST/LT capital gains and remaining loss carryforwards) so tax deferral is never confused with permanent tax elimination.
3. **Exact IRS §1091 61-Calendar-Day Wash-Sale Engine:** Tracks `HIFO` / `FIFO` / `specific_lot` disposals, corporate actions (`split`, `dividend`, `spin_off`, `ticker_change`), `[-30d, +30d]` calendar-day wash sales across identical tickers and `substantially_identical_groups` (e.g., `GOOG`/`GOOGL`), partial replacement lot splitting, basis rollover, and §1223(3) holding-period tacking.
4. **Configurable Tax Cash Flows & External Capital Gains:** Models federal + NIIT + state ST/LT capital gains and qualified/ordinary dividend taxes, the `$3,000` annual ordinary-income loss cap, indefinite carryforwards, optional annual external ST/LT capital gains budgets, and configurable portfolio tax-savings reinvestment vs. external tax settlement.
5. **Cross-Sectional Block Bootstrap & Plateau Sensitivity Sweeps:** Samples contiguous multi-constituent blocks simultaneously to preserve volatility clustering and cross-sectional dispersion, and ranks parameter sweeps by neighborhood **Plateau Score** alongside optional walk-forward out-of-sample splits.
6. **Survivorship-Bias Guardrails:** Datasets pulled from current S&P 500 constituent lists via `yfinance` are automatically flagged `survivorship_biased=True`, surfacing warning banners in the CLI and Web UI and blocking `"validated"` status until evaluated on complete point-in-time data.

---

## Prerequisites & Installation

- **Python:** `3.12+` managed with [`uv`](https://docs.astral.sh/uv/) (`.python-version` pins `3.13` by default).
- **Node.js:** `20+` and `npm` (required only if building or modifying the React Web UI in `web/`).

Install the Python environment and build the static React frontend:

```bash
# 1. Create .venv and install Python dependencies + CLI entrypoints
uv sync

# 2. Install frontend packages and compile web/dist for idx-ui
cd web && npm install && npm run build && cd ..
```

All market snapshots, Parquet partitions, and DuckDB simulation results are stored locally in `data/` (gitignored).

---

## End-to-End Quickstart

```bash
# Step 1: Generate a deterministic 5-year point-in-time synthetic S&P 500 dataset
uv run idx-fetch synthetic --dataset-id sp500_synthetic_demo --trading-days 1260

# Step 2: Run a 50-path block-bootstrap direct indexing simulation with TLH
uv run idx-sim run config/full_tlh_bootstrap.toml

# Step 3: Run a parameter sensitivity sweep with a 70/30 walk-forward OOS split
uv run idx-sim sweep config/sweep_tlh_and_sample.toml

# Step 4: Launch the interactive Web Workbench at http://127.0.0.1:8000
uv run idx-ui --port 8000
```

---

## CLI Reference (`idx-fetch`, `idx-sim`, `idx-ui`)

### 1. `idx-fetch` — Dataset Management

 datasets are **immutable and versioned** (`v1`, `v2`, ...). Re-fetching or re-importing under the same `--dataset-id` mints a new version rather than overwriting prior runs' inputs.

```bash
# Generate a deterministic factor-correlated synthetic S&P 500 dataset (PIT, survivorship_biased=False)
uv run idx-fetch synthetic \
  --dataset-id sp500_synthetic_demo \
  --start-date 2019-01-02 \
  --trading-days 1260 \
  --seed 12345

# Point-in-time S&P 500 (free): fja05680/sp500 membership + annual SPY weights + yfinance prices.
# Reference files are downloaded/cached in data/raw/ automatically; every data gap
# (delisted members with no prices, etc.) is listed in data/parquet/<id>/v<N>/errors.txt.
# Price bars are cached per ticker and incrementally extended on later pulls.
uv run idx-fetch pull \
  --source public \
  --dataset-id sp500_public \
  --start-date 2019-01-01 \
  --end-date 2024-12-31 \
  --benchmark VOO   # use SPY for start dates before 2010-09 (VOO launch)

# Point-in-time, survivorship-free S&P 500 (Sharadar direct API: https://api.sharadar.com/v1.0)
# Uses SHARADAR_API_KEY (or --api-key test-api-key --tickers AAPL,NVDA,WMT,AMZN for free sample).
uv run idx-fetch pull \
  --source sharadar \
  --dataset-id sp500_sharadar \
  --start-date 2019-01-01 \
  --end-date 2024-12-31 \
  --benchmark SPY

# Import your own point-in-time CSV or Parquet files
uv run idx-fetch import \
  --dataset-id sp500_pit \
  --prices path/to/prices.parquet \
  --weights path/to/weights.parquet \
  --etf path/to/voo.parquet \
  --dividends path/to/dividends.parquet \
  --no-survivorship-biased

# List all cached dataset versions, date ranges, missing-bar diagnostics, and bias flags
uv run idx-fetch list
```

### 2. `idx-sim` — Paired Backtests, Bootstrap Sweeps & Tax-Lot Exports

```bash
# Execute a single paired simulation config (historical, rolling_window, or block_bootstrap)
uv run idx-sim run config/baseline_etf.toml
uv run idx-sim run config/full_tlh_bootstrap.toml
uv run idx-sim run config/top100_whole_shares.toml

# Execute a Cartesian parameter grid sweep (+ optional walk-forward out-of-sample split)
uv run idx-sim sweep config/sweep_tlh_and_sample.toml

# Export closed tax lots for a run (optionally filtered to one path) as CSV or JSON
uv run idx-sim export-lots <run_id> --output closed_lots.csv
uv run idx-sim export-lots <run_id> --path-id boot_0000 --output boot_0000_lots.json
```

### 3. `idx-ui` — Web Workbench & FastAPI Server

```bash
# Production / single-command mode (serves FastAPI /api/* + built React SPA from web/dist)
uv run idx-ui --host 127.0.0.1 --port 8000 --data-dir data
```

For hot-reloading frontend development:

```bash
# Terminal 1: Run FastAPI backend with auto-reload
uv run idx-ui --port 8000 --reload

# Terminal 2: Run Vite dev server at http://localhost:5173 (proxies /api -> 127.0.0.1:8000)
cd web && npm run dev
```

---

## TOML Configuration Reference

Ready-to-run examples live in [`config/`](./config):
- [`config/baseline_etf.toml`](./config/baseline_etf.toml) — Historical paired direct-index (no TLH) vs. `VOO` tracking sanity check.
- [`config/full_tlh_bootstrap.toml`](./config/full_tlh_bootstrap.toml) — Full-constituent direct indexing with TLH and external capital gains across 50 block-bootstrap paths.
- [`config/top100_whole_shares.toml`](./config/top100_whole_shares.toml) — Sector-stratified top-100 basket constrained to integer whole shares (`allow_fractional = false`) across rolling 3-year windows.
- [`config/sweep_tlh_and_sample.toml`](./config/sweep_tlh_and_sample.toml) — Multi-parameter grid sweep with 70%/30% walk-forward out-of-sample validation.

### Single Run Config Schema (`SimulationRunConfig`)

```toml
save_equity_curves = true   # Persist daily P10/P25/P50/P75/P90 wealth & paired delta bands
save_lot_details = true     # Persist closed tax lots & wash-sale rollovers in DuckDB

[dataset]
dataset_id = "sp500_synthetic_demo"
version = "latest"          # Pin e.g. "v1" for strict immutable replay, or "latest"
# start_date = "2020-01-02" # Optional date slice start
# end_date = "2023-12-29"   # Optional date slice end

[path]
mode = "block_bootstrap"    # "historical" | "rolling_window" | "block_bootstrap"
horizon_days = 1260         # Trading days per simulated path (~252 days/year)
step_days = 21              # Window stride for "rolling_window" mode
block_size_days = 21        # Contiguous cross-sectional block length for "block_bootstrap"
num_paths = 50              # Number of Monte Carlo paths (or max rolling windows)
seed = 42                   # Deterministic PCG64 RNG seed

[strategy]
name = "full_direct_tlh"
sample_size = 100                        # Omit for all constituents, or top-K stratified by GICS sector
allow_fractional = true                  # false = integer floor + greedy largest-remainder shares
tlh_enabled = true
tlh_loss_pct_threshold = "0.05"          # Harvest lots down >= 5%
tlh_min_loss_dollars = "250"             # AND unrealized loss >= $250
tlh_check_frequency_days = 5             # Scan every 5 trading days
substitute_mode = "sector_peer"          # "sector_peer" | "sector_etf" | "cash"
rebalance_mode = "quarterly"             # "none" | "monthly" | "quarterly" | "annual" | "drift_band"
drift_band_abs = "0.015"                 # Max absolute weight drift when rebalance_mode = "drift_band"
direct_expense_ratio_bps = "15"          # Annualized direct-indexing advisory/platform fee in bps
etf_expense_ratio_bps = "3"              # Paired ETF fee in bps (applied when dataset ETF is gross)
lot_policy = "HIFO"                      # "HIFO" | "FIFO"
substantially_identical_groups = [["GOOG", "GOOGL"], ["FOXA", "FOX"], ["NWSA", "NWS"]]

[tax]
fed_st_rate = "0.37"                     # Federal short-term / ordinary rate
fed_lt_rate = "0.20"                     # Federal long-term / qualified dividend rate
niit_rate = "0.038"                      # 3.8% Net Investment Income Tax
state_st_rate = "0.093"                  # State ST rate (e.g., CA 9.3%–13.3%)
state_lt_rate = "0.093"                  # State LT rate
ordinary_income_offset_cap = "3000"      # Annual IRS §1211(b) ordinary-income loss deduction cap
external_st_gains_annual = "10000"       # Investor's annual external ST capital gains budget
external_lt_gains_annual = "25000"       # Investor's annual external LT capital gains budget
qualified_dividend_fraction_default = "0.95"
tax_settlement_mode = "reinvest_and_pay_from_portfolio" # Or "external_cash"
liquidate_at_end = true                  # Deduct hypothetical terminal liquidation tax on open lots

[cashflow]
initial_capital = "500000"
periodic_contribution = "5000"           # Positive = deposit, negative = withdrawal
contribution_frequency_days = 21         # Every ~1 trading month
```

### Parameter Sweep Config Schema (`SweepConfig`)

Sweeps wrap a `[base]` `SimulationRunConfig` and override any dotted keys under `[grid]`:

```toml
name = "tlh_threshold_and_sample_sweep"
walk_forward_split_ratio = 0.7           # Optional: 70% in-sample / 30% out-of-sample split

[base]
save_equity_curves = false
save_lot_details = false
# ... [base.dataset], [base.path], [base.strategy], [base.tax], [base.cashflow] ...

[grid]
"strategy.tlh_loss_pct_threshold" = ["0.03", "0.05", "0.08"]
"strategy.sample_size" = [20, 50]
"strategy.allow_fractional" = [true, false]
```

---

## Importing Custom Point-in-Time CSV / Parquet Datasets

To run bias-free multi-year historical validations (`survivorship_biased = False`), pass CSV or Parquet files to `idx-fetch import`:

- **`--prices` (Wide format):** `date` (`YYYY-MM-DD`) + one unadjusted point-in-time close column per constituent ticker (`AAPL`, `MSFT`, ...).
- **`--weights` (Wide or Long format):**
  - *Long format:* `date`, `ticker`, `weight`, and optional `sector` (`Information Technology`, `Financials`, etc.).
  - *Wide format:* `date` + one numeric weight column per ticker (automatically normalized to sum to `1.0` each day).
- **`--etf`:** `date`, `close`, and optional `return` and `dividend` columns for the paired S&P 500 ETF.
- **`--dividends` (Optional, Wide format):** `date` + per-share cash dividend on ex-date for each ticker.

See [`data/README.md`](./data/README.md) for exact per-file semantics and a comparison of free and paid point-in-time S&P 500 membership sources.

---

## Using the Web Workbench (`idx-ui`)

Opening `http://127.0.0.1:8000` provides three integrated tabs backed by FastAPI + DuckDB:

1. **Simulator & Sweeps:**
   - Configure `PathSpec`, direct-indexing constraints (top-$K$, whole shares vs. fractional, TLH triggers, substitute mode, fees), tax brackets, external gains, and tax-savings reinvestment.
   - Launch asynchronous `202 Accepted` multi-path runs or grid sweeps with live progress bars.
   - Inspect **Liquidation Win Rate vs. ETF**, **Percentile Wealth Fan Charts (P10/P25/P50/P75/P90)**, **Paired Outperformance ($\Delta W_p$)** bands, tracking error (bps), and **Plateau-Ranked Sweep Tables**.
2. **Data Explorer:**
   - Browse cached dataset versions, check `PIT` vs. `BIASED` survivorship badges and missing-bar percentages, inspect GICS sector weights, and plot constituent/ETF price series. Dataset ingestion is strictly offline via `idx-fetch`; if no datasets are cached yet, the UI lists zero datasets and displays the `idx-fetch` CLI commands to populate `data/`.
3. **Tax Ledger & Wash Sales:**
   - Jump directly to notable paths (**Median Path**, **Downside P10 Path**, or **Highest Tracking Error Path**).
   - Compare pre-liquidation wealth, terminal liquidation tax due, post-liquidation wealth, and unused loss carryforwards.
   - Filter closed tax lots and 30-day wash-sale basis rollovers by ticker, tax year, and holding term (`ST`/`LT`), and download filtered CSV/JSON exports.

---

## Repository Layout

- [`pyproject.toml`](./pyproject.toml) — Unified `uv` package configuration (`polars`, `duckdb`, `pydantic`, `typer`, `fastapi`, `yfinance`).
- [`DESIGN.md`](./DESIGN.md) — Architectural design document and non-negotiable analytical rules.
- [`config/`](./config) — Reference TOML simulation and parameter-sweep configurations.
- [`data/`](./data) — Local dataset store; [`data/README.md`](./data/README.md) documents every file and column.
- [`src/idx_engine/ledger/`](./src/idx_engine/ledger) — `LotLedger` (`lots.py`), 61-day `WashSaleResolver` (`wash_sale.py`), and `TaxAccountant` (`taxes.py`).
- [`src/idx_engine/fetch/`](./src/idx_engine/fetch) — Versioned Parquet + DuckDB `DataStore` (`store.py`), `yfinance` / synthetic / CSV loaders (`prices.py`, `index_weights.py`).
- [`src/idx_engine/strategy/`](./src/idx_engine/strategy) — Fractional & whole-share sizing (`sizing.py`), `EtfBaselineStrategy` (`etf_baseline.py`), and `DirectIndexStrategy` (`direct_index.py`).
- [`src/idx_engine/sim/`](./src/idx_engine/sim) — Path generators (`paths.py`), explicit-clock paired simulation engine (`engine.py`), percentile metrics (`metrics.py`), and sweep runner (`sweep.py`).
- [`src/idx_engine/api/`](./src/idx_engine/api) — FastAPI routes and single-writer-locked async `JobCoordinator`.
- [`web/`](./web) — React + TypeScript + Vite workbench (`DataExplorerPage`, `SimulatorPage`, `TaxLedgerPage`).
- [`tests/`](./tests) — Unit and integration test suites.

---

## Development & Testing

```bash
# Run Python unit, simulation, sweep, CLI, and FastAPI integration tests
uv run pytest -v

# Lint Python source and tests
uv run ruff check src tests

# Typecheck and build the React + TypeScript Web UI
cd web && npm run build
```
