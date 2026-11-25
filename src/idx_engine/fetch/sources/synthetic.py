"""Deterministic offline synthetic S&P 500 dataset generator (for demos and tests)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import polars as pl

from idx_engine.fetch.index_weights import SECTOR_ETFS
from idx_engine.fetch.store import DataStore
from idx_engine.models import CorporateAction, DatasetMetadata


@dataclass(frozen=True)
class ConstituentInfo:
    ticker: str
    name: str
    sector: str
    approx_weight: float


# Representative multi-sector S&P 500 constituent seed across all 11 GICS sectors,
# ordered by descending approximate index weight so top-K slices span all major sectors.
DEFAULT_SP500_SEED: tuple[ConstituentInfo, ...] = tuple(
    sorted(
        (
            ConstituentInfo("AAPL", "Apple Inc.", "Information Technology", 0.070),
            ConstituentInfo("MSFT", "Microsoft Corp.", "Information Technology", 0.068),
            ConstituentInfo("NVDA", "NVIDIA Corp.", "Information Technology", 0.064),
            ConstituentInfo("AVGO", "Broadcom Inc.", "Information Technology", 0.020),
            ConstituentInfo("ORCL", "Oracle Corp.", "Information Technology", 0.012),
            ConstituentInfo("CRM", "Salesforce Inc.", "Information Technology", 0.010),
            ConstituentInfo("AMD", "Advanced Micro Devices", "Information Technology", 0.009),
            ConstituentInfo("ADBE", "Adobe Inc.", "Information Technology", 0.008),
            ConstituentInfo("CSCO", "Cisco Systems", "Information Technology", 0.008),
            ConstituentInfo("ACN", "Accenture plc", "Information Technology", 0.007),
            ConstituentInfo("AMZN", "Amazon.com Inc.", "Consumer Discretionary", 0.040),
            ConstituentInfo("TSLA", "Tesla Inc.", "Consumer Discretionary", 0.020),
            ConstituentInfo("HD", "Home Depot Inc.", "Consumer Discretionary", 0.013),
            ConstituentInfo("MCD", "McDonald's Corp.", "Consumer Discretionary", 0.009),
            ConstituentInfo("LOW", "Lowe's Companies", "Consumer Discretionary", 0.006),
            ConstituentInfo("BKNG", "Booking Holdings", "Consumer Discretionary", 0.006),
            ConstituentInfo("TJX", "TJX Companies", "Consumer Discretionary", 0.005),
            ConstituentInfo("NKE", "NIKE Inc.", "Consumer Discretionary", 0.004),
            ConstituentInfo("META", "Meta Platforms Inc.", "Communication Services", 0.026),
            ConstituentInfo("GOOGL", "Alphabet Inc. Class A", "Communication Services", 0.022),
            ConstituentInfo("GOOG", "Alphabet Inc. Class C", "Communication Services", 0.018),
            ConstituentInfo("NFLX", "Netflix Inc.", "Communication Services", 0.012),
            ConstituentInfo("TMUS", "T-Mobile US", "Communication Services", 0.005),
            ConstituentInfo("DIS", "Walt Disney Co.", "Communication Services", 0.007),
            ConstituentInfo("VZ", "Verizon Communications", "Communication Services", 0.006),
            ConstituentInfo("BRK-B", "Berkshire Hathaway Class B", "Financials", 0.028),
            ConstituentInfo("JPM", "JPMorgan Chase & Co.", "Financials", 0.022),
            ConstituentInfo("V", "Visa Inc.", "Financials", 0.017),
            ConstituentInfo("MA", "Mastercard Inc.", "Financials", 0.014),
            ConstituentInfo("BAC", "Bank of America", "Financials", 0.010),
            ConstituentInfo("WFC", "Wells Fargo & Co.", "Financials", 0.008),
            ConstituentInfo("GS", "Goldman Sachs Group", "Financials", 0.006),
            ConstituentInfo("MS", "Morgan Stanley", "Financials", 0.005),
            ConstituentInfo("LLY", "Eli Lilly & Co.", "Health Care", 0.024),
            ConstituentInfo("UNH", "UnitedHealth Group", "Health Care", 0.018),
            ConstituentInfo("JNJ", "Johnson & Johnson", "Health Care", 0.014),
            ConstituentInfo("ABBV", "AbbVie Inc.", "Health Care", 0.012),
            ConstituentInfo("MRK", "Merck & Co.", "Health Care", 0.010),
            ConstituentInfo("TMO", "Thermo Fisher Scientific", "Health Care", 0.008),
            ConstituentInfo("ABT", "Abbott Laboratories", "Health Care", 0.007),
            ConstituentInfo("WMT", "Walmart Inc.", "Consumer Staples", 0.014),
            ConstituentInfo("PG", "Procter & Gamble", "Consumer Staples", 0.014),
            ConstituentInfo("COST", "Costco Wholesale", "Consumer Staples", 0.014),
            ConstituentInfo("KO", "Coca-Cola Co.", "Consumer Staples", 0.010),
            ConstituentInfo("PEP", "PepsiCo Inc.", "Consumer Staples", 0.009),
            ConstituentInfo("PM", "Philip Morris Intl.", "Consumer Staples", 0.007),
            ConstituentInfo("GE", "GE Aerospace", "Industrials", 0.008),
            ConstituentInfo("CAT", "Caterpillar Inc.", "Industrials", 0.007),
            ConstituentInfo("RTX", "RTX Corp.", "Industrials", 0.006),
            ConstituentInfo("UNP", "Union Pacific Corp.", "Industrials", 0.006),
            ConstituentInfo("HON", "Honeywell Intl.", "Industrials", 0.005),
            ConstituentInfo("DE", "Deere & Co.", "Industrials", 0.004),
            ConstituentInfo("XOM", "Exxon Mobil Corp.", "Energy", 0.018),
            ConstituentInfo("CVX", "Chevron Corp.", "Energy", 0.010),
            ConstituentInfo("COP", "ConocoPhillips", "Energy", 0.005),
            ConstituentInfo("EOG", "EOG Resources", "Energy", 0.004),
            ConstituentInfo("NEE", "NextEra Energy", "Utilities", 0.006),
            ConstituentInfo("SO", "Southern Company", "Utilities", 0.004),
            ConstituentInfo("DUK", "Duke Energy", "Utilities", 0.003),
            ConstituentInfo("PLD", "Prologis Inc.", "Real Estate", 0.004),
            ConstituentInfo("AMT", "American Tower Corp.", "Real Estate", 0.004),
            ConstituentInfo("EQIX", "Equinix Inc.", "Real Estate", 0.003),
            ConstituentInfo("LIN", "Linde plc", "Materials", 0.008),
            ConstituentInfo("SHW", "Sherwin-Williams", "Materials", 0.004),
            ConstituentInfo("FCX", "Freeport-McMoRan", "Materials", 0.003),
        ),
        key=lambda c: (-c.approx_weight, c.ticker),
    )
)


def _business_dates(start: date, trading_days: int) -> list[date]:
    dates: list[date] = []
    cur = start
    while len(dates) < trading_days:
        if cur.weekday() < 5:
            dates.append(cur)
        cur += timedelta(days=1)
    return dates


def generate_synthetic_sp500_dataset(
    store: DataStore,
    dataset_id: str = "sp500_synthetic_demo",
    start_date: date = date(2019, 1, 2),
    trading_days: int = 1260,
    max_constituents: int | None = None,
    seed: int = 12345,
    include_splits: bool = True,
) -> DatasetMetadata:
    """Create a deterministic, complete point-in-time multi-sector synthetic S&P 500 dataset.

    Generates realistic factor-correlated constituent returns (market + sector + idiosyncratic
    volatility), quarterly dividends (~1.5% yield), floating cap-weighted constituent weights,
    optional stock splits, and an aligned `VOO` ETF baseline tracking the exact index minus 3 bps.
    """
    rng = np.random.Generator(np.random.PCG64(seed))
    universe: Sequence[ConstituentInfo] = (
        DEFAULT_SP500_SEED[:max_constituents] if max_constituents else DEFAULT_SP500_SEED
    )
    tickers = [c.ticker for c in universe]
    n_tickers = len(tickers)
    dates = _business_dates(start_date, trading_days)

    sectors = {c.ticker: c.sector for c in universe}
    names = {c.ticker: c.name for c in universe}
    unique_sectors = sorted(set(sectors.values()))
    sector_idx = np.array([unique_sectors.index(sectors[t]) for t in tickers], dtype=np.int64)

    # Daily market + sector + idiosyncratic log-returns (~9.5% drift, realistic cross-dispersion)
    mkt_ret = rng.normal(loc=0.00036, scale=0.0105, size=trading_days)
    sec_ret = rng.normal(loc=0.0, scale=0.0065, size=(trading_days, len(unique_sectors)))
    betas = rng.uniform(0.75, 1.30, size=n_tickers)
    idio_vol = rng.uniform(0.011, 0.022, size=n_tickers)
    idio_ret = rng.normal(loc=0.0, scale=1.0, size=(trading_days, n_tickers)) * idio_vol

    daily_ret = mkt_ret[:, None] * betas[None, :] + sec_ret[:, sector_idx] + idio_ret
    daily_ret = np.clip(daily_ret, -0.35, 0.35)
    daily_ret[0, :] = 0.0

    init_prices = rng.uniform(45.0, 380.0, size=n_tickers)
    adj_prices = init_prices[None, :] * np.cumprod(1.0 + daily_ret, axis=0)

    # Point-in-time raw prices (apply a deterministic 2-for-1 split mid-horizon on top ticker if enabled)
    raw_prices = adj_prices.copy()
    corporate_actions: list[CorporateAction] = []
    if include_splits and trading_days >= 200 and n_tickers >= 1:
        split_day = trading_days // 2
        split_ticker = tickers[0]
        raw_prices[split_day:, 0] = raw_prices[split_day:, 0] / 2.0
        corporate_actions.append(
            CorporateAction(
                action_id=f"syn_split_{split_ticker}_{dates[split_day].isoformat()}",
                effective_date=dates[split_day],
                action_type="split",
                ticker=split_ticker,
                ratio_numerator=Decimal("2"),
                ratio_denominator=Decimal("1"),
            )
        )

    # Quarterly cash dividends (~0.375% per quarter = ~1.5% annual dividend yield)
    div_yield_q = rng.uniform(0.0015, 0.0060, size=n_tickers)
    dividends = np.zeros((trading_days, n_tickers), dtype=np.float64)
    for t_idx in range(60, trading_days, 63):
        dividends[t_idx, :] = np.round(raw_prices[t_idx, :] * div_yield_q, 4)

    # Floating cap-weighted index weights seeded from initial approx_weights
    base_shares = np.array([c.approx_weight for c in universe], dtype=np.float64)
    base_shares = base_shares / base_shares.sum()
    rel_growth = adj_prices / adj_prices[0:1, :]
    mcap = rel_growth * base_shares[None, :]
    weights = mcap / mcap.sum(axis=1, keepdims=True)

    # Aligned gross index benchmark (expense ratio is applied explicitly by EtfBaselineStrategy)
    lag_weights = np.vstack([weights[0:1, :], weights[:-1, :]])
    index_price_ret = np.sum(lag_weights * daily_ret, axis=1)
    index_price_ret[0] = 0.0
    div_yield_daily = np.sum(lag_weights * (dividends / np.maximum(raw_prices, 1e-8)), axis=1)

    etf_price_ret = index_price_ret.copy()
    etf_price_ret[0] = 0.0
    etf_close = 400.0 * np.cumprod(1.0 + etf_price_ret)
    etf_div = np.where(div_yield_daily > 0.0, np.round(etf_close * div_yield_daily, 4), 0.0)

    # Generate aligned zero-index-weight sector ETF proxy series (XLK, XLF, ...) so
    # `substitute_mode="sector_etf"` can trade real priced sector ETFs cleanly.
    sec_etf_tickers: list[str] = []
    sec_etf_prices_cols: dict[str, np.ndarray] = {}
    sec_etf_ret_cols: dict[str, np.ndarray] = {}
    sec_etf_div_cols: dict[str, np.ndarray] = {}
    for sec_idx_num, sec_name in enumerate(unique_sectors):
        sec_sym = SECTOR_ETFS.get(sec_name)
        if not sec_sym or sec_sym in tickers:
            continue
        sec_etf_tickers.append(sec_sym)
        sectors[sec_sym] = sec_name
        names[sec_sym] = f"{sec_name} Select Sector ETF"
        s_ret = mkt_ret + sec_ret[:, sec_idx_num]
        s_ret[0] = 0.0
        s_px = 100.0 * np.cumprod(1.0 + s_ret)
        s_div = np.zeros(trading_days, dtype=np.float64)
        for t_idx in range(60, trading_days, 63):
            s_div[t_idx] = round(float(s_px[t_idx]) * 0.0035, 4)
        sec_etf_prices_cols[sec_sym] = s_px
        sec_etf_ret_cols[sec_sym] = s_ret
        sec_etf_div_cols[sec_sym] = s_div

    prices_df = pl.DataFrame(
        {
            "date": dates,
            **{tickers[j]: raw_prices[:, j] for j in range(n_tickers)},
            **sec_etf_prices_cols,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    returns_df = pl.DataFrame(
        {
            "date": dates,
            **{tickers[j]: daily_ret[:, j] for j in range(n_tickers)},
            **sec_etf_ret_cols,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    dividends_df = pl.DataFrame(
        {
            "date": dates,
            **{tickers[j]: dividends[:, j] for j in range(n_tickers)},
            **sec_etf_div_cols,
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    weights_df = pl.DataFrame(
        {
            "date": dates,
            **{tickers[j]: weights[:, j] for j in range(n_tickers)},
            **{sym: np.zeros(trading_days, dtype=np.float64) for sym in sec_etf_tickers},
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    etf_df = pl.DataFrame(
        {
            "date": dates,
            "close": etf_close,
            "return": etf_price_ret,
            "dividend": etf_div,
        }
    ).with_columns(pl.col("date").cast(pl.Date))

    return store.save_dataset(
        dataset_id=dataset_id,
        source="synthetic_factor_sp500",
        prices_df=prices_df,
        returns_df=returns_df,
        dividends_df=dividends_df,
        weights_df=weights_df,
        etf_df=etf_df,
        sectors=sectors,
        names=names,
        corporate_actions=tuple(corporate_actions),
        benchmark_ticker="VOO",
        survivorship_biased=False,
        missing_bar_fraction=0.0,
        notes="Deterministic point-in-time multi-sector synthetic S&P 500 dataset.",
        created_at_iso="2026-01-01T00:00:00+00:00"
        if dataset_id == "sp500_synthetic_demo" and not store.list_datasets()
        else None,
    )
