"""Read-only dataset listing, constituent breakdown, and price series routes."""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from idx_engine.fetch import DataStore

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


def _safe_float(val: Any, ndigits: int = 4) -> float:
    if val is None:
        return 0.0
    f = float(val)
    if not math.isfinite(f):
        return 0.0
    return round(f, ndigits)


def _get_store(request: Request) -> DataStore:
    return request.app.state.store


@router.get("")
def list_datasets(request: Request) -> list[dict[str, Any]]:
    store = _get_store(request)
    return [asdict(m) for m in store.list_datasets()]


@router.get("/{dataset_id}")
def inspect_dataset(
    dataset_id: str,
    request: Request,
    version: str = "latest",
) -> dict[str, Any]:
    store = _get_store(request)
    try:
        ds = store.load_dataset(dataset_id=dataset_id, version=version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    tickers = ds.tickers
    last_row_idx = ds.weights_df.height - 1
    first_weights = ds.weights_df.select(tickers).row(0)
    last_weights = ds.weights_df.select(tickers).row(last_row_idx)
    first_prices = ds.prices_df.select(tickers).row(0)
    last_prices = ds.prices_df.select(tickers).row(last_row_idx)
    div_sums = ds.dividends_df.select(tickers).sum().row(0)

    constituents: list[dict[str, Any]] = []
    sector_weights: dict[str, float] = {}
    for i, t in enumerate(tickers):
        sec = ds.sectors.get(t, "Unknown")
        w_end = _safe_float(last_weights[i], 6)
        sector_weights[sec] = sector_weights.get(sec, 0.0) + w_end
        constituents.append(
            {
                "ticker": t,
                "name": ds.names.get(t, t),
                "sector": sec,
                "start_weight": _safe_float(first_weights[i], 6),
                "end_weight": w_end,
                "start_price": _safe_float(first_prices[i], 2),
                "end_price": _safe_float(last_prices[i], 2),
                "total_dividends_per_share": _safe_float(div_sums[i], 4),
            }
        )
    constituents.sort(key=lambda item: (-item["end_weight"], item["ticker"]))

    sectors_summary = [
        {"sector": sec, "weight": round(w, 6)}
        for sec, w in sorted(sector_weights.items(), key=lambda item: -item[1])
    ]

    return {
        "metadata": asdict(ds.metadata),
        "sectors": sectors_summary,
        "constituents": constituents,
        "corporate_actions": [
            {
                "action_id": ca.action_id,
                "effective_date": ca.effective_date.isoformat(),
                "action_type": ca.action_type,
                "ticker": ca.ticker,
                "ratio_numerator": str(ca.ratio_numerator),
                "ratio_denominator": str(ca.ratio_denominator),
                "target_ticker": ca.target_ticker,
            }
            for ca in ds.corporate_actions
        ],
    }


@router.get("/{dataset_id}/series/{ticker}")
def get_ticker_series(
    dataset_id: str,
    ticker: str,
    request: Request,
    version: str = "latest",
) -> dict[str, Any]:
    store = _get_store(request)
    try:
        ds = store.load_dataset(dataset_id=dataset_id, version=version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sym = ticker.upper()
    if sym == ds.metadata.benchmark_ticker.upper():
        dates = ds.etf_df.get_column("date").to_list()
        closes = ds.etf_df.get_column("close").to_list()
        rets = ds.etf_df.get_column("return").to_list()
        divs = ds.etf_df.get_column("dividend").to_list()
        return {
            "dataset_id": ds.metadata.dataset_id,
            "version": ds.metadata.version,
            "ticker": ds.metadata.benchmark_ticker,
            "sector": "Index ETF",
            "points": [
                {
                    "date": d.isoformat(),
                    "price": _safe_float(c, 4),
                    "return": _safe_float(r, 6),
                    "dividend": _safe_float(dv, 4),
                    "weight": 1.0,
                }
                for d, c, r, dv in zip(dates, closes, rets, divs, strict=True)
            ],
        }

    if sym not in ds.prices_df.columns:
        raise HTTPException(
            status_code=404, detail=f"Ticker '{sym}' not found in dataset '{dataset_id}'"
        )

    dates = ds.prices_df.get_column("date").to_list()
    prices = ds.prices_df.get_column(sym).to_list()
    rets = ds.returns_df.get_column(sym).to_list()
    divs = ds.dividends_df.get_column(sym).to_list()
    weights = ds.weights_df.get_column(sym).to_list()

    return {
        "dataset_id": ds.metadata.dataset_id,
        "version": ds.metadata.version,
        "ticker": sym,
        "sector": ds.sectors.get(sym, "Unknown"),
        "points": [
            {
                "date": d.isoformat(),
                "price": _safe_float(p, 4),
                "return": _safe_float(r, 6),
                "dividend": _safe_float(dv, 4),
                "weight": _safe_float(w, 6),
            }
            for d, p, r, dv, w in zip(dates, prices, rets, divs, weights, strict=True)
        ],
    }
