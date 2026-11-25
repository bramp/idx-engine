# Market & Index Data Sources for `idx-fetch`

The goal of `idx-engine` is to compare holding a broad index fund—such as **`VFIAX` / `VOO`** (S&P 500) or **`VTSAX` / `VTI`** (Total US Stock Market)—against reproducing that index manually with individual stocks (direct indexing with tax-loss harvesting).

To make that comparison fair and avoid survivorship bias (especially important for tax-loss harvesting, where distressed and delisted stocks generate disproportionate harvested losses), every dataset version (`data/parquet/<dataset_id>/v<N>/`) needs:

- **`weights.parquet`** — daily point-in-time (PIT) target index weights summing to `1.0`, with `0.0` on dates a ticker is not an index member.
- **`prices.parquet`** — as-traded (split-unadjusted, not dividend-adjusted) daily close prices.
- **`returns.parquet`** — split-adjusted daily simple price returns (excluding dividends), used by block-bootstrap path generation.
- **`dividends.parquet`** — as-traded cash dividends per share on ex-dividend dates.
- **`corporate_actions.parquet`** — splits (and ideally ticker renames, mergers, spin-offs, and cash takeovers) applied to the tax-lot ledger.
- **`sectors.parquet`** — GICS sector and company name per ticker, used for sector-stratified sampling and wash-sale `sector_peer` substitutes.
- **`etf.parquet`** — paired benchmark fund (`VOO`, `SPY`, `IVV`, or `VTI`) close, daily price return, and ex-date cash dividend (expense ratio is applied separately by `EtfBaselineStrategy`).
- **`errors.txt`** — data-quality report listing dropped members, partial price coverage, missing weights, and unmodeled events.

---

## 1. What Data Is Needed & Which Source Provides It

| Data Requirement | Needed For | Best Paid Source (Survivorship-Free) | Best Free / Approximate Source |
| --- | --- | --- | --- |
| **S&P 500 PIT membership** (`VFIAX` / `VOO` / `SPY`) | `weights.parquet` mask | **Sharadar `sp500`** (quarterly snapshots + daily add/remove effective dates since 1998), Norgate Data, CRSP | **`fja05680/sp500`** (daily changes since 1996) or iShares `IVV` historical holdings |
| **Total US Market PIT membership & quarterly reconstitutions** (`VTSAX` / `VTI`) | `weights.parquet` mask | **CRSP US Total Market** (exact index rules/buffers, institutional) or **Sharadar `tickers` + `daily`** (quarterly reconstitution across all `Domestic Common Stock` active + delisted) | SEC **N-PORT** quarterly filings for `VTSAX`/`VTI` (2019+ only, requires parsing) |
| **Daily target index weights** | `weights.parquet` | **Sharadar `daily` (`marketcap`)** (included in Bundle) or CRSP float-adjusted cap | Kaggle annual **`SPY`** snapshots drifted by price, or iShares **`IVV`** historical holdings |
| **As-traded & split-adjusted stock prices (incl. delisted)** | `prices.parquet`, `returns.parquet` | **Sharadar `stocks`** (`closeunadj` + `close`, active & delisted, recycled tickers disambiguated), Norgate Data, EODHD | **`yfinance`** (free, but missing ~343 of 989 S&P 500 members over 2001–2024) |
| **Dividends & corporate actions** | `dividends.parquet`, `corporate_actions.parquet` | **Sharadar `actions`** (splits, dividends, ticker changes, spin-offs, acquisitions, delistings) | **`yfinance`** (dividends and splits only; no mergers/spin-offs/takeovers) |
| **GICS sectors & security master** | `sectors.parquet` | **Sharadar `tickers`** (covers active & delisted; mapped to GICS labels) | `fja05680` `sp500.csv` (current members only; former members marked `Unknown`) |
| **Paired benchmark fund** | `etf.parquet` | **Sharadar `funds`** (`SPY`, `VOO`, `IVV`, `VTI`) | **`yfinance`** (`VOO` 2010+, `SPY` 1993+, `VTI` 2001+) |

---

## 2. What Is Currently Implemented in `idx-fetch`

| Command / Mode | Universe | Implementation & Caching | Survivorship Status |
| --- | --- | --- | --- |
| **`idx-fetch pull --source sharadar`** *(recommended)* | **S&P 500** (`SPY` / `VOO`) | Direct Sharadar API (`https://api.sharadar.com/v1.0`, `SHARADAR_API_KEY`). Uses `sp500`, `stocks`, `funds`, `actions`, `tickers`, and `daily` (`marketcap`, with automatic fallback to price-drifted equal weights on Prices-only plans). Caches bulk Parquet zips in `data/raw/sharadar/bulk/` (checked via `?status=True`) and sliced REST queries in `data/raw/sharadar/slices/`. Zero hardcoded ticker renames. Supports `--api-key test-api-key --tickers AAPL,NVDA,WMT,AMZN` for free testing. | **`survivorship_biased=False`** (`validated`) on full bulk PIT pulls with complete price coverage. |
| **`idx-fetch pull --source public`** *(default, free)* | **S&P 500** (`VOO` / `SPY`) | `fja05680/sp500` daily membership + Kaggle annual `SPY` Sept 30 weights drifted by price + `yfinance` prices/dividends/splits. Uses resumable, incremental per-ticker `YahooPriceCache` in `data/raw/yfinance/` and static `KNOWN_TICKER_RENAMES`. Missing index weight is ~1% (2024), ~5% (2019), and ~22% (early 2000s). | **`survivorship_biased=True`** (`exploratory_survivorship_biased`) whenever any member lacks full price coverage. |
| **`idx-fetch import`** | Any (S&P 500, Total Market, etc.) | Imports local wide/long CSV or Parquet (`--prices`, `--weights`, `--etf`, `--dividends`). | Controlled by `--no-survivorship-biased`. |
| **`idx-fetch synthetic`** | 65-stock demo | Deterministic factor-model synthetic dataset (`DEFAULT_SP500_SEED`) for offline testing. | `survivorship_biased=False` (synthetic). |

### Not Yet Implemented
- **Total US Stock Market (`VTSAX` / `VTI`) universe in `--source sharadar`:** Reconstituting quarterly from Sharadar `tickers` (`category = "Domestic Common Stock"`) + `daily.marketcap`, paired with `VTI` in `funds`.
- **Non-split corporate actions:** Mergers, spin-offs, and cash takeovers (present in Sharadar's `actions` table) are not yet emitted into `corporate_actions.parquet` or accepted by `idx-fetch import`.
- **iShares `IVV` historical holdings / SEC N-PORT** scrapers for free float-adjusted weights.

---

## 3. Source Reference Details

### Membership & Target Weights (`weights.parquet`, `sectors.parquet`)

| Source | Populates | Pros | Cons |
| --- | --- | --- | --- |
| **Sharadar (`sp500`, `daily`, `tickers`)** *(implemented)* | `weights.parquet`, `sectors.parquet` | Direct REST + bulk CSV zip API (`api.sharadar.com/v1.0`); survivorship-free back to 1998; recycled tickers disambiguated; history rewired across ticker renames; daily `marketcap` in Bundle | Paid subscription (Prices plan $9–$39/mo excludes `daily`; Bundle $29–$69/mo includes `daily`); total market cap rather than proprietary S&P/CRSP float factors |
| **GitHub (`fja05680/sp500`)** *(implemented)* | `weights.parquet` (mask), `sectors.parquet` (current only) | Free (MIT); daily S&P 500 add/remove history since 1996; cached in `data/raw/fja05680/` | No weights; historical symbols require rename mapping (`KNOWN_TICKER_RENAMES`); no sectors for former members |
| **Kaggle Annual `SPY` Holdings** *(implemented)* | `weights.parquet` (annual Sept 30 anchors) | Free (CC0); cached in `data/raw/kaggle_spy/` (`2000.csv`–`2025.csv`) | Annual snapshots only; retroactively relabels acquired companies under successor tickers and bankruptcies as `DELISTED`; relies on third-party maintenance |
| **iShares `IVV` Historical Holdings** *(not implemented)* | `weights.parquet`, `sectors.parquet` | Free; official BlackRock float-adjusted weights and sectors back to ~2007 | Requires paced historical scraping and identity resolution across ticker changes |
| **CRSP via WRDS** *(import only)* | Exact S&P 500 & CRSP US Total Market (`VTSAX`) | Gold standard; exact float-adjusted weights, quarterly reconstitutions, and delisting returns | Institutional license required |

### Constituent Prices, Dividends & Corporate Actions (`prices.parquet`, `returns.parquet`, `dividends.parquet`, `corporate_actions.parquet`)

| Source | Populates | Pros | Cons |
| --- | --- | --- | --- |
| **Sharadar (`stocks`, `actions`)** *(implemented)* | `prices`, `returns`, `dividends`, `corporate_actions` (splits) | Active + delisted US equities; provides both `closeunadj` and split-adjusted `close` directly; clean bulk zip downloads; works natively on macOS/Linux/Windows | Paid subscription |
| **Yahoo Finance (`yfinance`)** *(implemented)* | `prices`, `returns`, `dividends`, `corporate_actions` (splits) | Free; no API key; incrementally cached per ticker in `data/raw/yfinance/` via `YahooPriceCache` | Missing most delisted/acquired stocks (~343 of 989 S&P 500 members over 2001–2024); rate-limits large batches; splits only |
| **Norgate Data / EODHD / Tiingo** *(import only)* | `prices`, `returns`, `dividends`, splits | Include delisted US equities | Paid (Norgate updater is Windows-only; EODHD S&P 500 constituents start in April 2012) |

### Paired Benchmark Fund (`etf.parquet`)

`idx-engine` evaluates direct-indexing strategies against a **tradable index ETF** (`EtfBaselineStrategy`), so `etf.parquet` needs actual fund closes and ex-date cash dividends—not price-only index levels like `^GSPC` or FRED `SP500`.

| Benchmark | Tracks | Coverage | Notes |
| --- | --- | --- | --- |
| **`VOO`** | S&P 500 (ETF twin of `VFIAX`) | 2010-09-09 to present | Default for `--source public`; 3 bps expense ratio modeled in `EtfBaselineStrategy` |
| **`SPY`** | S&P 500 | 1993-01-29 to present | Default for `--source sharadar` (also available on `test-api-key`); required before `2010-09-09` |
| **`IVV`** | S&P 500 | 2000-05-19 to present | Long-history S&P 500 alternative |
| **`VTI`** | CRSP US Total Market (ETF twin of `VTSAX`) | 2001-05-31 to present | Paired benchmark for Total US Stock Market comparisons |
