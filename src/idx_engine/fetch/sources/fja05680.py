"""fja05680/sp500 point-in-time S&P 500 membership and current-member metadata loader."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import polars as pl

from idx_engine.fetch.sources.http import download_cached, normalize_ticker_expr

FJA05680_BASE_URL = "https://raw.githubusercontent.com/fja05680/sp500/master/"
FJA05680_MEMBERSHIP_FILE = "S&P 500 Historical Components & Changes (Updated).csv"
FJA05680_CURRENT_FILE = "sp500.csv"

# Pure ticker changes (same company) among historical S&P 500 members. fja05680 lists the
# symbol in use at the time, but yfinance only serves the history under the newest symbol,
# so membership is rewritten old -> new before downloading. Chains are resolved.
KNOWN_TICKER_RENAMES: dict[str, str] = {
    "ABC": "COR",
    "ADS": "BFH",
    "ANTM": "ELV",
    "ARNC": "HWM",
    "BK": "BNY",
    "BLL": "BALL",
    "CDAY": "DAY",
    "COG": "CTRA",
    "CTL": "LUMN",
    "DWDP": "DD",
    "FB": "META",
    "FBHS": "FBIN",
    "FI": "FISV",
    "FLT": "CPAY",
    "GPS": "GAP",
    "HCP": "PEAK",
    "HFC": "DINO",
    "HRS": "LHX",
    "JEC": "J",
    "MMC": "MRSH",
    "NLOK": "GEN",
    "PEAK": "DOC",
    "PKI": "RVTY",
    "RE": "EG",
    "SYMC": "NLOK",
    "TMK": "GL",
    "UTX": "RTX",
    "VIAB": "VIAC",
    "VIAC": "PARA",
    "WLTW": "WTW",
}


def resolve_ticker_rename(ticker: str) -> str:
    seen = {ticker}
    while ticker in KNOWN_TICKER_RENAMES:
        ticker = KNOWN_TICKER_RENAMES[ticker]
        if ticker in seen:
            break
        seen.add(ticker)
    return ticker


def load_sp500_membership(
    cache_dir: str | Path, refresh: bool = False, timeout_sec: float = 60.0
) -> pl.DataFrame:
    """Load point-in-time S&P 500 membership from fja05680/sp500 (cached under `cache_dir`).

    Returns a long `(date, ticker)` frame. Each `date` is a membership change date and the
    ticker set applies from that date until the next listed date.
    """
    path = download_cached(
        FJA05680_BASE_URL + quote(FJA05680_MEMBERSHIP_FILE),
        Path(cache_dir) / FJA05680_MEMBERSHIP_FILE,
        timeout_sec,
        refresh,
    )
    raw = pl.read_csv(path, try_parse_dates=True)
    return (
        raw.select(pl.col("date").cast(pl.Date), pl.col("tickers").str.split(",").alias("ticker"))
        .explode("ticker")
        .with_columns(normalize_ticker_expr("ticker"))
        .filter(pl.col("ticker") != "")
        .unique()
        .sort(["date", "ticker"])
    )


def load_sp500_current_info(
    cache_dir: str | Path, refresh: bool = False, timeout_sec: float = 60.0
) -> dict[str, tuple[str, str]]:
    """Return `{ticker: (name, gics_sector)}` for *current* members from fja05680's `sp500.csv`."""
    path = download_cached(
        FJA05680_BASE_URL + FJA05680_CURRENT_FILE,
        Path(cache_dir) / FJA05680_CURRENT_FILE,
        timeout_sec,
        refresh,
    )
    df = pl.read_csv(path).select(
        normalize_ticker_expr("Symbol").alias("ticker"),
        pl.col("Security").alias("name"),
        pl.col("GICS Sector").alias("sector"),
    )
    return {t: (n, s) for t, n, s in df.iter_rows()}
