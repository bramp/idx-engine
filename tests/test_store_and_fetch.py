"""Unit tests for versioned Parquet + DuckDB DataStore, synthetic builder, and local import."""

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from idx_engine.fetch import (
    DataStore,
    generate_synthetic_sp500_dataset,
    import_local_dataset,
)


def test_synthetic_dataset_versioning_immutability_and_slicing(tmp_path: Path) -> None:
    store = DataStore(data_dir=tmp_path / "data")
    meta_v1 = generate_synthetic_sp500_dataset(
        store,
        dataset_id="syn_test",
        start_date=date(2022, 1, 3),
        trading_days=260,
        max_constituents=15,
        seed=99,
    )
    assert meta_v1.version == "v1"
    assert meta_v1.ticker_count >= 15
    assert meta_v1.trading_days == 260
    assert meta_v1.survivorship_biased is False

    # Saving again mints v2 automatically rather than mutating v1
    meta_v2 = generate_synthetic_sp500_dataset(
        store,
        dataset_id="syn_test",
        start_date=date(2022, 1, 3),
        trading_days=300,
        max_constituents=15,
        seed=100,
    )
    assert meta_v2.version == "v2"

    # Attempting to overwrite v1 raises FileExistsError (Rule #8)
    ds_v1 = store.load_dataset("syn_test", version="v1")
    with pytest.raises(FileExistsError):
        store.save_dataset(
            dataset_id="syn_test",
            version="v1",
            source="dup",
            prices_df=ds_v1.prices_df,
            returns_df=ds_v1.returns_df,
            dividends_df=ds_v1.dividends_df,
            weights_df=ds_v1.weights_df,
            etf_df=ds_v1.etf_df,
            sectors=ds_v1.sectors,
        )

    # Latest resolves to v2
    ds_latest = store.load_dataset("syn_test", version="latest")
    assert ds_latest.metadata.version == "v2"
    assert ds_latest.metadata.trading_days == 300
    assert len(ds_v1.corporate_actions) == 1  # mid-horizon split generated

    # Date slicing works cleanly
    sliced = ds_v1.slice_dates(start_date=date(2022, 3, 1), end_date=date(2022, 6, 30))
    assert sliced.metadata.start_date >= date(2022, 3, 1)
    assert sliced.metadata.end_date <= date(2022, 6, 30)
    assert sliced.metadata.trading_days == sliced.prices_df.height


def test_local_csv_dataset_import_normalizes_weights(tmp_path: Path) -> None:
    store = DataStore(data_dir=tmp_path / "data")
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()

    pl.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "AAPL": [185.0, 184.0, 186.5],
            "MSFT": [370.0, 372.0, 375.0],
        }
    ).write_csv(csv_dir / "prices.csv")

    pl.DataFrame(
        {
            "date": [
                "2024-01-02",
                "2024-01-02",
                "2024-01-03",
                "2024-01-03",
                "2024-01-04",
                "2024-01-04",
            ],
            "ticker": ["AAPL", "MSFT", "AAPL", "MSFT", "AAPL", "MSFT"],
            "weight": [60.0, 40.0, 60.0, 40.0, 60.0, 40.0],
            "sector": ["Tech", "Tech", "Tech", "Tech", "Tech", "Tech"],
        }
    ).write_csv(csv_dir / "weights.csv")

    pl.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "close": [430.0, 430.5, 434.0],
            "dividend": [0.0, 0.0, 1.5],
        }
    ).write_csv(csv_dir / "etf.csv")

    meta = import_local_dataset(
        store,
        dataset_id="custom_pit",
        prices_path=csv_dir / "prices.csv",
        weights_path=csv_dir / "weights.csv",
        etf_path=csv_dir / "etf.csv",
        survivorship_biased=False,
    )
    assert meta.survivorship_biased is False
    loaded = store.load_dataset("custom_pit")
    assert loaded.weights_df["AAPL"].to_list() == pytest.approx([0.6, 0.6, 0.6])
    assert loaded.weights_df["MSFT"].to_list() == pytest.approx([0.4, 0.4, 0.4])


def test_pit_pull_masks_membership_applies_renames_and_reports_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import numpy as np
    import pandas as pd

    from idx_engine.fetch import fetch_yfinance_dataset
    from idx_engine.fetch.sources import price_cache as price_cache_mod
    from idx_engine.fetch.sources import yfinance as yf_mod

    # AAA always a member; BBB leaves on 2024-01-04 and CCC joins; FB is renamed META;
    # GONE has no yfinance history at all.
    membership = pl.DataFrame(
        {
            "date": [date(2023, 12, 1)] * 4 + [date(2024, 1, 4)] * 4,
            "ticker": ["AAA", "BBB", "FB", "GONE", "AAA", "CCC", "FB", "GONE"],
        }
    )
    spy = pl.DataFrame(
        {
            "as_of": [date(2023, 9, 30)] * 4,
            "ticker": ["AAA", "BBB", "META", "GONE"],
            "weight": [0.5, 0.2, 0.2, 0.1],
        }
    )
    monkeypatch.setattr(yf_mod, "load_sp500_membership", lambda *a, **k: membership)
    monkeypatch.setattr(yf_mod, "load_sp500_current_info", lambda *a, **k: {})
    monkeypatch.setattr(yf_mod, "load_spy_annual_weights", lambda *a, **k: spy)

    idx = pd.DatetimeIndex(pd.bdate_range("2024-01-02", periods=5))

    def fake_download(tickers: list[str], **_: object) -> pd.DataFrame:
        cols = {}
        for t in tickers:
            if t == "GONE":
                continue
            px = np.full(len(idx), 100.0)
            if t == "CCC":
                px[:1] = np.nan  # no bar before listing
            for field, vals in (
                ("Close", px),
                ("Dividends", np.zeros(len(idx))),
                ("Stock Splits", np.zeros(len(idx))),
            ):
                cols[(t, field)] = vals
        return pd.DataFrame(cols, index=idx)

    monkeypatch.setattr(price_cache_mod.yf, "download", fake_download)
    monkeypatch.setattr(price_cache_mod.time, "sleep", lambda _s: None)

    store = DataStore(data_dir=tmp_path / "data")
    meta = fetch_yfinance_dataset(
        store=store, start_date=date(2024, 1, 2), end_date=date(2024, 1, 8), benchmark_ticker="VOO"
    )

    ds = store.load_dataset("sp500_public")
    w = ds.weights_df
    assert "META" in w.columns and "FB" not in w.columns
    assert "GONE" not in w.columns
    before = w.filter(pl.col("date") == date(2024, 1, 3))
    after = w.filter(pl.col("date") == date(2024, 1, 4))
    assert before["BBB"][0] > 0 and before["CCC"][0] == 0
    assert after["BBB"][0] == 0 and after["CCC"][0] > 0
    assert meta.survivorship_biased

    report = (store.parquet_dir / "sp500_public" / meta.version / "errors.txt").read_text()
    assert "FB       -> META" in report
    assert "NO PRICE DATA (1)" in report and "  GONE " in report
    assert "  CCC " in report  # joined mid-window without a SPY weight


def test_import_local_dataset_normalizes_dot_tickers_across_all_files(tmp_path: Path) -> None:
    prices_p = tmp_path / "prices.csv"
    weights_p = tmp_path / "weights.csv"
    divs_p = tmp_path / "divs.csv"
    etf_p = tmp_path / "etf.csv"

    prices_p.write_text("date,AAPL,BRK.B\n2024-01-02,100.0,300.0\n2024-01-03,101.0,303.0\n")
    weights_p.write_text(
        "date,ticker,weight,sector\n"
        "2024-01-02,AAPL,0.6,Information Technology\n"
        "2024-01-02,BRK.B,0.4,Financials\n"
        "2024-01-03,AAPL,0.6,Information Technology\n"
        "2024-01-03,BRK.B,0.4,Financials\n"
    )
    divs_p.write_text("date,AAPL,BRK.B\n2024-01-02,0.0,0.0\n2024-01-03,0.25,1.5\n")
    etf_p.write_text("date,close,dividend\n2024-01-02,400.0,0.0\n2024-01-03,404.0,0.0\n")

    store = DataStore(data_dir=tmp_path / "data")
    import_local_dataset(
        store=store,
        dataset_id="dot_test",
        prices_path=prices_p,
        weights_path=weights_p,
        etf_path=etf_p,
        dividends_path=divs_p,
    )
    ds = store.load_dataset("dot_test")
    assert set(ds.tickers) == {"AAPL", "BRK-B"}
    assert abs(ds.weights_df["BRK-B"][0] - 0.4) < 1e-9
    assert abs(ds.weights_df["AAPL"][0] + ds.weights_df["BRK-B"][0] - 1.0) < 1e-9
    assert ds.dividends_df["BRK-B"][1] == 1.5
    assert ds.sectors["BRK-B"] == "Financials"
