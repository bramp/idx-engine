"""Unit tests for the Sharadar direct API client and PIT dataset builder."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import polars as pl

from idx_engine.fetch.sources.sharadar import (
    SharadarClient,
    build_daily_membership,
    fetch_sharadar_dataset,
)
from idx_engine.fetch.store import DataStore


def _zip_csv(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.csv", csv_text)
    return buf.getvalue()


def test_build_daily_membership_applies_adds_removes_and_snapshots() -> None:
    sp500 = pl.DataFrame(
        {
            "date": ["2023-12-31", "2023-12-31", "2024-01-04", "2024-01-04"],
            "action": ["historical", "historical", "removed", "added"],
            "ticker": ["AAA", "BBB", "BBB", "CCC"],
        }
    )
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    membership = build_daily_membership(sp500, days)
    assert membership[date(2024, 1, 2)] == {"AAA", "BBB"}
    assert membership[date(2024, 1, 3)] == {"AAA", "BBB"}
    assert membership[date(2024, 1, 4)] == {"AAA", "CCC"}
    assert membership[date(2024, 1, 5)] == {"AAA", "CCC"}


def test_fetch_sharadar_dataset_bulk_validated_and_cached(tmp_path: Path) -> None:
    requests_made: list[str] = []

    tables: dict[str, str] = {
        "funds": (
            "ticker,date,close,closeadj,closeunadj\n"
            "SPY,2024-01-02,100.0,99.0,200.0\n"
            "SPY,2024-01-03,101.0,99.99,202.0\n"
            "SPY,2024-01-04,100.0,100.0,100.0\n"
        ),
        "sp500": (
            "date,action,ticker,name,contraticker,contraname,note\n"
            "2023-12-31,historical,AAA,AAA CORP,N/A,N/A,\n"
            "2023-12-31,historical,BBB,BBB CORP,N/A,N/A,\n"
            "2023-12-31,historical,NULLPX,NULL CORP,N/A,N/A,\n"
            "2024-01-04,removed,BBB,BBB CORP,CCC,CCC CORP,\n"
            "2024-01-04,added,CCC,CCC CORP,BBB,BBB CORP,\n"
        ),
        "stocks": (
            "ticker,date,close,closeunadj\n"
            "AAA,2024-01-02,50.0,100.0\n"
            "AAA,2024-01-03,51.0,102.0\n"
            "AAA,2024-01-04,52.0,52.0\n"
            "BBB,2024-01-02,20.0,20.0\n"
            "BBB,2024-01-03,21.0,21.0\n"
            "BBB,2024-01-04,22.0,22.0\n"
            "CCC,2024-01-02,40.0,40.0\n"
            "CCC,2024-01-03,41.0,41.0\n"
            "CCC,2024-01-04,42.0,42.0\n"
            "NULLPX,2024-01-02,,\n"
        ),
        "actions": (
            "date,action,ticker,name,value,contraticker,contraname\n"
            "2024-01-03,dividend,AAA,AAA CORP,0.5,N/A,N/A\n"
            "2024-01-04,split,AAA,AAA CORP,2,N/A,N/A\n"
            "2024-01-03,dividend,SPY,SPDR,1.0,N/A,N/A\n"
        ),
        "daily": (
            "ticker,date,marketcap\n"
            "AAA,2024-01-02,300\n"
            "AAA,2024-01-03,306\n"
            "AAA,2024-01-04,312\n"
            "BBB,2024-01-02,100\n"
            "BBB,2024-01-03,105\n"
            "CCC,2024-01-04,104\n"
        ),
        "tickers": (
            "table,ticker,name,sector\n"
            "insiders,AAA,AAA Corp,\n"
            "stocks,AAA,AAA Corp,Technology\n"
            "stocks,BBB,BBB Corp,Healthcare\n"
            "stocks,CCC,CCC Corp,Financial Services\n"
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-api-key") == "paid-key"
        table = request.url.path.rsplit("/", 1)[-1]
        requests_made.append(f"{table}?{request.url.query.decode()}")
        if request.url.params.get("status") == "True":
            return httpx.Response(200, json={"table": table, "modified": "2026-01-01T00:00:00Z"})
        if "years" in request.url.params:
            return httpx.Response(200, content=_zip_csv(tables[table]))
        return httpx.Response(200, text=tables[table])

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    store = DataStore(data_dir=tmp_path / "data")
    client = SharadarClient(
        api_key="paid-key", cache_dir=store.raw_dir / "sharadar", http_client=http_client
    )

    meta = fetch_sharadar_dataset(
        store=store,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 4),
        benchmark_ticker="SPY",
        client=client,
    )
    # NULLPX had only a null close row so it is dropped (making survivorship_biased True).
    assert meta.survivorship_biased is True
    assert meta.ticker_count == 3

    ds = store.load_dataset("sp500_sharadar")
    assert "NULLPX" not in ds.prices_df.columns
    # ETF uses split-adjusted close & dividend (no spurious 200 -> 100 drop).
    assert ds.etf_df.filter(pl.col("date") == date(2024, 1, 2))["close"][0] == 100.0
    assert ds.etf_df.filter(pl.col("date") == date(2024, 1, 3))["dividend"][0] == 1.0
    # Pre-split dividend of 0.5 (split-adjusted) scaled by closeunadj/close (102/51 = 2) -> 1.0.
    div_row = ds.dividends_df.filter(pl.col("date") == date(2024, 1, 3))
    assert div_row["AAA"][0] == 1.0
    assert len(ds.corporate_actions) == 1
    assert ds.corporate_actions[0].ticker == "AAA"
    assert float(ds.corporate_actions[0].ratio_numerator) == 2.0

    # PIT membership & marketcap weights: 2024-01-02 has AAA=0.75, BBB=0.25, CCC=0.0.
    w0 = ds.weights_df.filter(pl.col("date") == date(2024, 1, 2))
    assert abs(w0["AAA"][0] - 0.75) < 1e-9
    assert abs(w0["BBB"][0] - 0.25) < 1e-9
    assert w0["CCC"][0] == 0.0
    w2 = ds.weights_df.filter(pl.col("date") == date(2024, 1, 4))
    assert w2["BBB"][0] == 0.0
    assert abs(w2["AAA"][0] - 0.75) < 1e-9
    assert abs(w2["CCC"][0] - 0.25) < 1e-9

    # Sectors mapped to GICS labels.
    assert ds.sectors["AAA"] == "Information Technology"
    assert ds.sectors["BBB"] == "Health Care"
    assert ds.sectors["CCC"] == "Financials"

    # Second run only checks bulk `status=True` and downloads zero zips.
    requests_made.clear()
    fetch_sharadar_dataset(
        store=store,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 4),
        benchmark_ticker="SPY",
        client=client,
    )
    assert all("status=True" in req for req in requests_made)


def test_fetch_sharadar_dataset_falls_back_when_daily_forbidden(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        if table == "daily":
            return httpx.Response(403, json={"error": "Exceeds plan"})
        if request.url.params.get("status") == "True":
            return httpx.Response(401, json={"error": "Unauthorized"})
        csv_map = {
            "funds": "ticker,date,close,closeadj,closeunadj\nSPY,2024-01-02,100,100,100\nSPY,2024-01-03,101,101,101\n",
            "sp500": "date,action,ticker\n2023-12-31,historical,AAA\n",
            "stocks": "ticker,date,close,closeunadj\nAAA,2024-01-02,50,50\nAAA,2024-01-03,55,55\n",
            "actions": "date,action,ticker,name,value,contraticker,contraname\n",
            "tickers": "table,ticker,name,sector\nstocks,AAA,AAA Corp,Technology\n",
        }
        return httpx.Response(200, text=csv_map[table])

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    store = DataStore(data_dir=tmp_path / "data")
    client = SharadarClient(
        api_key="prices-only-key", cache_dir=store.raw_dir / "sharadar", http_client=http_client
    )

    meta = fetch_sharadar_dataset(
        store=store,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        benchmark_ticker="SPY",
        client=client,
    )
    assert "equal_weight_price_drifted" in meta.source
    report = (store.parquet_dir / "sp500_sharadar" / meta.version / "errors.txt").read_text()
    assert "Sharadar `daily` table (marketcap) is not included" in report
