"""Local CSV/Parquet dataset importer."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from idx_engine.fetch.index_weights import load_weights_file
from idx_engine.fetch.sources.http import normalize_yahoo_ticker
from idx_engine.fetch.sources.synthetic import generate_synthetic_sp500_dataset
from idx_engine.fetch.sources.yfinance import fetch_yfinance_dataset
from idx_engine.fetch.store import DataStore
from idx_engine.models import DatasetMetadata

__all__ = [
    "fetch_yfinance_dataset",
    "generate_synthetic_sp500_dataset",
    "import_local_dataset",
]


def import_local_dataset(
    store: DataStore,
    dataset_id: str,
    prices_path: str | Path,
    weights_path: str | Path,
    etf_path: str | Path,
    dividends_path: str | Path | None = None,
    survivorship_biased: bool = False,
    benchmark_ticker: str = "VOO",
    notes: str = "Imported from local CSV/Parquet files.",
) -> DatasetMetadata:
    """Import point-in-time or custom CSV/Parquet files into an immutable dataset version."""
    p_file = Path(prices_path)
    prices_raw = (
        pl.read_parquet(p_file)
        if p_file.suffix.lower() == ".parquet"
        else pl.read_csv(p_file, try_parse_dates=True)
    )
    if "date" not in prices_raw.columns:
        raise ValueError("prices file must contain a 'date' column")
    tickers = [normalize_yahoo_ticker(c) for c in prices_raw.columns if c != "date"]
    rename_map = {c: normalize_yahoo_ticker(c) for c in prices_raw.columns if c != "date"}
    prices_df = (
        prices_raw.rename(rename_map)
        .with_columns(
            pl.col("date").cast(pl.Date),
            *[pl.col(t).cast(pl.Float64) for t in tickers],
        )
        .sort("date")
    )

    weights_df, sectors_from_w = load_weights_file(weights_path)
    # Align columns & dates
    common_dates = prices_df.select("date").join(weights_df.select("date"), on="date", how="inner")
    prices_df = common_dates.join(prices_df, on="date", how="left")
    weights_df = common_dates.join(weights_df, on="date", how="left")
    for t in tickers:
        if t not in weights_df.columns:
            weights_df = weights_df.with_columns(pl.lit(0.0).alias(t))
    weights_df = weights_df.select(["date", *tickers])
    w_row_sum = pl.sum_horizontal([pl.col(t).fill_null(0.0) for t in tickers])
    weights_df = weights_df.with_columns(
        *[
            pl.when(w_row_sum > 0.0)
            .then(pl.col(t).fill_null(0.0) / w_row_sum)
            .otherwise(0.0)
            .alias(t)
            for t in tickers
        ]
    )

    # Sanitize constituent prices (forward-fill then back-fill missing bars) & compute returns
    p_mat = prices_df.select(tickers).to_numpy().astype(np.float64, copy=True)
    missing_fraction = float(np.isnan(p_mat).sum() / max(1, p_mat.size))
    n_days_imp = p_mat.shape[0]
    for j in range(len(tickers)):
        col = p_mat[:, j]
        for i in range(1, n_days_imp):
            if (np.isnan(col[i]) or not np.isfinite(col[i])) and np.isfinite(col[i - 1]):
                col[i] = col[i - 1]
        for i in range(n_days_imp - 2, -1, -1):
            if (np.isnan(col[i]) or not np.isfinite(col[i])) and np.isfinite(col[i + 1]):
                col[i] = col[i + 1]
        p_mat[:, j] = np.nan_to_num(col, nan=0.0, posinf=0.0, neginf=0.0)

    dates = prices_df.get_column("date").to_list()
    prices_df = pl.DataFrame(
        {"date": dates, **{tickers[j]: p_mat[:, j] for j in range(len(tickers))}}
    ).with_columns(pl.col("date").cast(pl.Date))

    ret_mat = np.zeros_like(p_mat)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(p_mat[:-1, :] > 0, p_mat[1:, :] / p_mat[:-1, :] - 1.0, 0.0)
    ret_mat[1:, :] = ratio
    returns_df = pl.DataFrame(
        {"date": dates, **{tickers[j]: ret_mat[:, j] for j in range(len(tickers))}}
    ).with_columns(pl.col("date").cast(pl.Date))

    if dividends_path is not None:
        d_file = Path(dividends_path)
        div_raw = (
            pl.read_parquet(d_file)
            if d_file.suffix.lower() == ".parquet"
            else pl.read_csv(d_file, try_parse_dates=True)
        )
        div_rename = {c: normalize_yahoo_ticker(c) for c in div_raw.columns if c != "date"}
        div_df = common_dates.join(
            div_raw.rename(div_rename).with_columns(pl.col("date").cast(pl.Date)),
            on="date",
            how="left",
        )
        for t in tickers:
            if t not in div_df.columns:
                div_df = div_df.with_columns(pl.lit(0.0).alias(t))
            else:
                div_df = div_df.with_columns(pl.col(t).cast(pl.Float64).fill_null(0.0))
        dividends_df = div_df.select(["date", *tickers])
    else:
        dividends_df = pl.DataFrame(
            {"date": dates, **{t: np.zeros(len(dates), dtype=np.float64) for t in tickers}}
        ).with_columns(pl.col("date").cast(pl.Date))

    e_file = Path(etf_path)
    etf_raw = (
        pl.read_parquet(e_file)
        if e_file.suffix.lower() == ".parquet"
        else pl.read_csv(e_file, try_parse_dates=True)
    )
    etf_joined = common_dates.join(
        etf_raw.with_columns(pl.col("date").cast(pl.Date)), on="date", how="left"
    )
    etf_close = etf_joined.get_column("close").cast(pl.Float64).to_numpy().astype(np.float64, copy=True)
    for i in range(1, len(etf_close)):
        if (np.isnan(etf_close[i]) or not np.isfinite(etf_close[i])) and np.isfinite(etf_close[i - 1]):
            etf_close[i] = etf_close[i - 1]
    for i in range(len(etf_close) - 2, -1, -1):
        if (np.isnan(etf_close[i]) or not np.isfinite(etf_close[i])) and np.isfinite(etf_close[i + 1]):
            etf_close[i] = etf_close[i + 1]
    etf_close = np.nan_to_num(etf_close, nan=0.0, posinf=0.0, neginf=0.0)

    if "return" in etf_joined.columns:
        etf_ret = np.nan_to_num(
            etf_joined.get_column("return").cast(pl.Float64).fill_null(0.0).to_numpy(),
            nan=0.0,
            posinf=0.0,
            neginf=0.0,
        )
    else:
        etf_ret = np.zeros_like(etf_close)
        with np.errstate(divide="ignore", invalid="ignore"):
            etf_ret[1:] = np.where(
                etf_close[:-1] > 0, etf_close[1:] / etf_close[:-1] - 1.0, 0.0
            )
    etf_div = (
        np.nan_to_num(
            etf_joined.get_column("dividend").cast(pl.Float64).fill_null(0.0).to_numpy(),
            nan=0.0,
            posinf=0.0,
            neginf=0.0,
        )
        if "dividend" in etf_joined.columns
        else np.zeros_like(etf_close)
    )
    etf_df = pl.DataFrame(
        {"date": dates, "close": etf_close, "return": etf_ret, "dividend": etf_div}
    ).with_columns(pl.col("date").cast(pl.Date))

    sectors = {t: sectors_from_w.get(t, "Unknown") for t in tickers}

    return store.save_dataset(
        dataset_id=dataset_id,
        source="local_import",
        prices_df=prices_df,
        returns_df=returns_df,
        dividends_df=dividends_df,
        weights_df=weights_df,
        etf_df=etf_df,
        sectors=sectors,
        benchmark_ticker=benchmark_ticker,
        survivorship_biased=survivorship_biased,
        missing_bar_fraction=missing_fraction,
        notes=notes,
    )
