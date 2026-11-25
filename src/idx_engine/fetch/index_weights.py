"""Sector ETF mapping and local CSV/Parquet weight-file loader."""

from __future__ import annotations

from pathlib import Path

import polars as pl

SECTOR_ETFS: dict[str, str] = {
    "Information Technology": "XLK",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
}


def load_weights_file(path: str | Path) -> tuple[pl.DataFrame, dict[str, str]]:
    """Load constituent weights from a CSV or Parquet file.

    Supports either:
    1. Wide format: `date` column + one numeric column per ticker.
    2. Long format: `date`, `ticker`, `weight` (and optional `sector`) columns.
    Returns `(wide_weights_df, sectors_map)`.
    """
    from idx_engine.fetch.sources.http import normalize_ticker_expr, normalize_yahoo_ticker

    p = Path(path)
    raw = (
        pl.read_parquet(p)
        if p.suffix.lower() == ".parquet"
        else pl.read_csv(p, try_parse_dates=True)
    )

    sectors_map: dict[str, str] = {}
    if {"date", "ticker", "weight"}.issubset(set(raw.columns)):
        normalized_long = raw.with_columns(
            pl.col("date").cast(pl.Date),
            normalize_ticker_expr("ticker").alias("ticker"),
            pl.col("weight").cast(pl.Float64),
        )
        if "sector" in normalized_long.columns:
            pairs = normalized_long.select(["ticker", "sector"]).unique().iter_rows()
            sectors_map = {str(t): str(s or "Unknown") for t, s in pairs}
        wide = normalized_long.pivot(
            on="ticker", index="date", values="weight", aggregate_function="first"
        )
        wide = wide.sort("date").fill_null(0.0)
    else:
        if "date" not in raw.columns:
            raise ValueError(f"Weights file {p} must contain a 'date' column")
        rename_map = {c: normalize_yahoo_ticker(c) for c in raw.columns if c != "date"}
        ticker_cols = list(rename_map.values())
        wide = (
            raw.rename(rename_map)
            .with_columns(
                pl.col("date").cast(pl.Date),
                *[pl.col(c).cast(pl.Float64).fill_null(0.0) for c in ticker_cols],
            )
            .sort("date")
        )

    # Normalize each row so constituent weights sum to 1.0 when positive
    ticker_cols = [c for c in wide.columns if c != "date"]
    row_sum = pl.sum_horizontal([pl.col(c) for c in ticker_cols])
    normalized = wide.with_columns(
        *[
            pl.when(row_sum > 0.0).then(pl.col(c) / row_sum).otherwise(0.0).alias(c)
            for c in ticker_cols
        ]
    )
    return normalized, sectors_map
