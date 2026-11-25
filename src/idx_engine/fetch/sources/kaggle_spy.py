"""Kaggle annual SPY holdings weight snapshots (`devaangbarthwal/...spy-2000-2024`, CC0)."""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import polars as pl

from idx_engine.fetch.sources.http import download_cached, normalize_ticker_expr

SPY_WEIGHTS_KAGGLE_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/"
    "devaangbarthwal/s-and-p-500-holdings-and-weights-spy-2000-2024"
)


def load_spy_annual_weights(
    cache_dir: str | Path, refresh: bool = False, timeout_sec: float = 120.0
) -> pl.DataFrame:
    """Load annual SPY holdings weights (Kaggle `devaangbarthwal/...spy-2000-2024`, CC0).

    One snapshot per year, as of September 30 (SPY's fiscal year end). Returns
    `(as_of, ticker, weight)` with `weight` as a fraction of the index.

    The source labels holdings with their *current successor* ticker (e.g. Activision is
    `MSFT`, Celgene is `BMY`, bankruptcies are `DELISTED`), so one ticker can cover several
    companies in a snapshot. The largest row is kept (normally the ticker's own company);
    acquired companies are left unmatched and get a fallback weight downstream.
    """
    cache = Path(cache_dir)
    if refresh or not any(cache.glob("[0-9][0-9][0-9][0-9].csv")):
        zip_path = download_cached(SPY_WEIGHTS_KAGGLE_URL, cache / "dataset.zip", timeout_sec, True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(cache)
        zip_path.unlink()

    frames = []
    for f in sorted(cache.glob("[0-9][0-9][0-9][0-9].csv")):
        frames.append(
            pl.read_csv(f, infer_schema_length=0).select(
                pl.lit(date(int(f.stem), 9, 30)).alias("as_of"),
                pl.when((pl.col("Ticker") == "GOOG") & pl.col("Company").str.contains("Class A"))
                .then(pl.lit("GOOGL"))
                .otherwise(normalize_ticker_expr("Ticker"))
                .alias("ticker"),
                (pl.col("Weight").cast(pl.Float64, strict=False) / 100.0).alias("weight"),
            )
        )
    if not frames:
        raise RuntimeError(f"No annual SPY weight files found in {cache}")
    return (
        pl.concat(frames)
        .drop_nulls()
        .filter((pl.col("ticker") != "") & (pl.col("ticker") != "DELISTED"))
        .group_by(["as_of", "ticker"])
        .agg(pl.col("weight").max())
        .sort(["as_of", "ticker"])
    )
