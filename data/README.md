# `data/` — Local Data Store

Everything under `data/` is managed by
[`DataStore`](../src/idx_engine/fetch/store.py) and can be regenerated with the
`idx-fetch` commands. Override the location with `--data-dir` (CLI) or
`IDX_DATA_DIR` (API server).

```text
data/
├── app.duckdb                      # Catalog of datasets + all simulation/sweep results
├── raw/                            # Reference downloads cached by `idx-fetch pull`
│   ├── fja05680/                   #   PIT membership + current sectors (fja05680/sp500)
│   ├── kaggle_spy/                 #   Annual SPY holdings weights, 2000.csv..2025.csv
│   └── yfinance/                   #   Per-ticker price cache + coverage manifest
└── parquet/
    └── <dataset_id>/               # e.g. sp500_synthetic_demo, sp500_public
        └── v<N>/                   # Immutable version; re-fetching mints v2, v3, ...
            ├── metadata.json
            ├── prices.parquet
            ├── returns.parquet
            ├── dividends.parquet
            ├── weights.parquet
            ├── etf.parquet
            ├── sectors.parquet
            ├── corporate_actions.parquet
            └── errors.txt          # `pull` only: data-quality / missing-data report
```

`idx-fetch pull` stores yfinance responses per ticker under
`data/raw/yfinance/`. Each Parquet file and the coverage manifest are updated
atomically, so an interrupted pull can be repeated without re-downloading completed
symbols. Later pulls request only uncovered dates, with a short overlap to
detect split-adjusted history changes. Use `--refresh-prices` to fetch the
requested ranges again, or `--retry-missing-prices` to retry symbols previously
recorded as having no Yahoo history.

Dataset versions are **write-once**. `save_dataset` refuses to overwrite an
existing `v<N>` directory. Dataset discovery reads `metadata.json` directly
from `data/parquet/<dataset_id>/<version>/` on demand (taking `dataset_id` and
`version` from the directory path), and `version = "latest"` resolves to the
version with the newest `created_at_iso`. You can safely delete or rename
version folders on disk, or delete `app.duckdb` to clear run history, without
desynchronizing your datasets.

Only this README is tracked in git; everything else here is regenerable and is
git-ignored. The Parquet folders and `app.duckdb` must stay in sync, so
`sp500_synthetic_demo` is not committed. It is rebuilt exactly from its seed
the first time `idx-sim` or `idx-ui` runs. Real price data isn't committed
because of Yahoo's terms and the file sizes.

## Dataset files

`prices`, `returns`, `dividends`, and `weights` are **wide** tables. Each has
one row per trading date and one `Float64` column per constituent ticker.
All four share the same `date` column and ticker columns, in the same order.
Tickers use Yahoo format (`BRK-B`, not `BRK.B`). The ETF benchmark is stored
separately in `etf.parquet`, not as a ticker column.

| File | Shape | Contents |
|---|---|---|
| `prices.parquet` | `date` + 1 col/ticker | **As-traded (split-unadjusted) close price** per share on that date. This is the price lots are bought and sold at, so a split shows up as a price drop that `corporate_actions` offsets. It is *not* dividend-adjusted. |
| `returns.parquet` | `date` + 1 col/ticker | **Daily simple price return** `close[t]/close[t-1] - 1`, computed from split-*adjusted* closes so splits don't appear as crashes. Excludes dividends. The first row is `0`. Only the block bootstrap uses it, to resample days; the historical path engine trades off `prices`. |
| `dividends.parquet` | `date` + 1 col/ticker | **Cash dividend per share** (as-traded, not split-adjusted), placed on the **ex-dividend date**. The value is `0.0` on all other days. The engine credits `shares_held × amount` on that row's date. |
| `weights.parquet` | `date` + 1 col/ticker | **Target index weight** of each ticker on that date. Each row sums to 1.0. A weight of `0` means "not in the index today", and the direct-index strategy will not target it. See the per-source notes below: for `sp500_public` these are approximated from annual SPY snapshots drifted by price. |
| `etf.parquet` | `date, close, return, dividend` | Benchmark ETF (default `VOO`): close price, daily price return (excluding dividends), and cash dividend per share on the ex-date. No expense ratio is deducted here; `EtfBaselineStrategy` applies the fee explicitly. |
| `sectors.parquet` | `ticker, sector, name` | One row per ticker with its GICS sector (used for sector-stratified sampling and wash-sale substitutes) and company name. The sector is a single static value for the whole history. |
| `corporate_actions.parquet` | one row per action | `action_id, effective_date, action_type, ticker, ratio_numerator, ratio_denominator, cash_amount_per_share, qualified_fraction, target_ticker, target_sector, basis_allocation_fraction`. Decimal fields are stored as strings. The engine applies each action to the tax-lot ledger on `effective_date`. For example, a `split` of 4/1 multiplies the share count and divides the per-share basis. |
| `metadata.json` | object | `dataset_id, version, source, created_at_iso, start_date, end_date, trading_days, ticker_count, benchmark_ticker, survivorship_biased, missing_bar_fraction, notes`. Source of truth for dataset discovery (`idx-fetch list`, `DataStore.load_dataset`). |

Every row is a trading date. There is no row for weekends or market holidays
(the synthetic dataset also skips weekends, but not holidays).

## How each source populates the files

### `sp500_synthetic_demo` (`idx-fetch synthetic`)

Fully **synthetic** data. Only the tickers, names, and sectors are real.

- **Universe:** the 65 hard-coded large caps in `DEFAULT_SP500_SEED`
  ([index_weights.py](../src/idx_engine/fetch/index_weights.py)), covering all
  11 GICS sectors. Membership is fixed, so there's no survivorship issue, but
  also no index adds or drops.
- **Dates:** Mon–Fri business days starting at `--start-date` (default
  1260 days from 2019-01-02). Holidays are not removed.
- **Returns:** a factor model, `beta × market + sector + idiosyncratic` noise,
  from a seeded RNG (`--seed`). It reproduces exactly for the same seed.
- **Prices:** random starting prices of $45–$380, compounded by the returns.
  `AAPL` gets one 2-for-1 split halfway through, recorded in
  `corporate_actions`.
- **Dividends:** a quarterly cash dividend every 63 trading days (starting on
  day 60), for a yield of about 0.6–2.4%/yr per stock.
- **Weights:** start from each seed's `approx_weight`, then drift with price
  (cap-weighted, constant share counts).
- **ETF:** the exact cap-weighted index price return, starting at $400. The
  dividend equals the weighted constituent dividend yield.

### `sp500_public` (`idx-fetch pull`)

**Point-in-time membership** with **real prices and dividends** from Yahoo
Finance. All inputs are downloaded by the command itself; there is no separate
download step. Reference files are cached in `data/raw/`, and
`--refresh-reference-data` re-downloads them.

- **Universe:** [fja05680/sp500](https://github.com/fja05680/sp500)
  "Historical Components & Changes". Every ticker that was an S&P 500 member at
  any point in the window is included, and its weight is `0` on days it was
  not a member. Old symbols are mapped to current ones (`FB`→`META`, etc.)
  via `KNOWN_TICKER_RENAMES` so that yfinance can find them.
  `--max-constituents N` is for debugging only: it keeps the N largest members
  at the start date and drops later additions.
- **Remaining survivorship gap:** yfinance has no history for many delisted,
  acquired, or bankrupt companies. Those members are dropped and their weight
  is redistributed to the other members. The missing share is about 1% of
  index weight in 2024, about 5% in 2019, and about 22% in the early 2000s.
  Each affected ticker is listed in `errors.txt`. Whenever anything is
  missing, the dataset is flagged `survivorship_biased=true`, which blocks
  `validated` run status.
- **Prices:** yfinance `Close` with `auto_adjust=False`, which is
  split-adjusted but not dividend-adjusted. Each value is multiplied back by
  the product of later split ratios to recover the as-traded price. Gaps are
  forward-filled and then **back-filled**, so a stock that IPO'd mid-window
  shows a flat price at its first close for earlier dates.
  `missing_bar_fraction` records the share of gaps before filling.
- **Dividends:** the yfinance `Dividends` column (per share, keyed by ex-date),
  un-split-adjusted the same way as prices.
- **Splits:** from the yfinance `Stock Splits` column, written to
  `corporate_actions` as `split` rows. Mergers, spin-offs, ticker changes, and
  cash takeovers are **not** captured.
- **Weights:** annual SPY holdings snapshots (Kaggle
  `devaangbarthwal/s-and-p-500-holdings-and-weights-spy-2000-2024`, CC0, as of
  Sept 30 each year). Weights drift daily with each stock's split-adjusted
  price until the next snapshot and are renormalized over the members that
  have prices. Members missing from a snapshot (added mid-year, or relabelled
  under an acquirer's ticker in the source) get that snapshot's median
  weight. Share-count changes between snapshots are not captured. With
  `--no-spy-weights`, the weights fall back to equal weight at the start date,
  drifted by price.
- **ETF:** the benchmark ticker (default `VOO`), with its yfinance close and
  dividends. VOO only starts in 2010-09, so use `--benchmark SPY` for earlier
  start dates.
- **`errors.txt`:** a summary with the missing index weight for each year,
  the ticker renames applied, members with no price data or with partial
  coverage (which also catches reused tickers), members with no SPY weight,
  members with unknown sectors, and what is not modelled.

### Custom imports (`idx-fetch import`)

Use this to bring point-in-time data from another provider. The importer
accepts CSV or Parquet:

- `--prices`: wide, `date` + one column per ticker of **as-traded close
  prices**. Use NaN or blank when a ticker isn't trading.
- `--weights`: either the same wide shape, or long format with
  `date, ticker, weight[, sector]` columns. Each row is renormalized to sum
  to 1. **Put `0` weight on dates a ticker is not in the index.** This is how
  point-in-time membership is expressed.
- `--etf`: `date, close[, return][, dividend]`.
- `--dividends` (optional): wide, cash per share on the ex-date.

The output keeps only the dates that appear in **both** the prices and the
weights files.

Limitations of the importer:
- It can't import corporate actions yet, so splits must already be removed
  from prices (or use split-adjusted prices and dividends consistently).
- Missing prices are not filled; only `returns` treats them as 0. Delistings
  and cash takeovers aren't modeled, so a held ticker whose prices stop is
  never sold at a real price. Set its weight to `0` before the last trading
  day so the strategy exits it first.

## `app.duckdb`

DuckDB results store for simulation runs, tax lots, and sweeps. Open it with
`duckdb data/app.duckdb` or `DataStore(...)`.

| Table | One row per | Purpose |
|---|---|---|
| `runs` | simulation run | Config JSON, summary JSON, validation status, and the dataset/version used. |
| `path_outcomes` | run × path | Per-path direct vs. benchmark results and paired deltas. |
| `equity_band_points` | run × day | P10–P90 equity and wealth-delta bands used by the fan charts. |
| `closed_lots` | closed tax lot | Ticker, dates, quantity, proceeds, basis, realized and wash-adjusted gain/loss. Exported by `idx-sim export-lots`. |
| `wash_sales` | wash-sale adjustment | Loss lot, replacement lot, and the disallowed loss carried into the replacement's basis. |
| `sweeps` / `sweep_rows` | sweep / sweep × param set | Parameter-sweep configs and in-sample/out-of-sample summaries. |

Deleting `app.duckdb` clears run/sweep history while leaving all Parquet
datasets under `data/parquet/` intact and immediately discoverable.

## Getting point-in-time (survivorship-free) S&P 500 data

A survivorship-free backtest needs three things: **(1)** index membership on
each date, **(2)** prices and dividends for every member, including companies
that later left the index or were delisted, and ideally **(3)** real
weights, from market cap or ETF holdings.

| Source | Cost | Membership | Delisted prices | Weights | Notes |
|---|---|---|---|---|---|
| [fja05680/sp500](https://github.com/fja05680/sp500) | Free (MIT) | Daily since 1996 | No | No | Use `S&P 500 Historical Components & Changes (Updated).csv` (`date,tickers`). Tickers carry `-YYYYMM` suffixes to resolve reused tickers. Unofficial, so cross-check it. |
| [hanshof/sp500_constituents](https://github.com/hanshof/sp500_constituents) | Free | Daily since 1996 | No | No | Same idea with plain tickers, so reused tickers are ambiguous. |
| Wikipedia "Selected changes" table | Free | Partial | No | No | Good for sanity checks only. |
| [Norgate Data](https://norgatedata.com) | About $300–600/yr | Yes, since 1990 | Yes | No | The best retail option. Python API. The data updater is Windows-only. |
| [Sharadar](https://data.nasdaq.com/databases/SFA) (Nasdaq Data Link) | About $30–80/mo | `SP500` table | Yes (`SEP`) | Market cap via fundamentals | Cheap, clean, and includes delisted names. |
| CRSP via WRDS | Institutional | `crsp.dsp500list` | Yes, plus delisting returns | Market cap, so weights can be derived | The gold standard. Needs a university or employer subscription. |
| EODHD / FMP / Polygon / Tiingo | Free tier up to about $50/mo | EODHD and FMP only (FMP is a changes list) | Partial or yes | No | Useful as price sources for delisted tickers. |
| iShares IVV / SPDR SPY holdings | Free | Today only | No | **Real weights** | Download daily going forward, or scrape the Wayback Machine for history. |

Prices are from memory and not re-checked; confirm them before buying.

**Free route:** use fja05680 for membership and yfinance or Tiingo for
prices. Free sources lack prices for many delisted tickers, so the result is
*less* biased but not completely survivorship-free. **Paid route:** Norgate or
Sharadar cover both membership and delisted prices. CRSP is the best source if
you have access.

To load the data, write the files described under *Custom imports* above.
The weights file should use the long format (`date,ticker,weight`), with a row
only on dates when the ticker is an index member (or a weight of `0`
otherwise). Then run:

```bash
uv run idx-fetch import --dataset-id sp500_pit \
  --prices prices.parquet --weights weights.parquet \
  --etf voo.parquet --dividends dividends.parquet
```

Only pass `--survivorship-biased` if some members are still missing price data.
