"""Closed tax-lot, wash-sale adjustment, and CSV/JSON export routes."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from idx_engine.fetch import DataStore

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


def _get_store(request: Request) -> DataStore:
    return request.app.state.store


def _require_run_exists(store: DataStore, run_id: str) -> None:
    if not store.run_exists(run_id):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")


def _safe_filename_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:64]


@router.get("/{run_id}/closed-lots")
def get_closed_lots(
    run_id: str,
    request: Request,
    path_id: str | None = None,
    ticker: str | None = None,
    tax_year: int | None = None,
    term: Literal["ST", "LT"] | None = None,
    strategy_role: Literal["direct", "benchmark"] = "direct",
    limit: int = Query(default=1000, ge=1, le=50000),
) -> list[dict[str, Any]]:
    store = _get_store(request)
    _require_run_exists(store, run_id)
    return store.query_closed_lots(
        run_id=run_id,
        path_id=path_id,
        ticker=ticker,
        tax_year=tax_year,
        term=term,
        strategy_role=strategy_role,
        limit=limit,
    )


@router.get("/{run_id}/wash-sales")
def get_wash_sales(
    run_id: str,
    request: Request,
    path_id: str | None = None,
    ticker: str | None = None,
    limit: int = Query(default=1000, ge=1, le=50000),
) -> list[dict[str, Any]]:
    store = _get_store(request)
    _require_run_exists(store, run_id)
    return store.query_wash_sales(
        run_id=run_id,
        path_id=path_id,
        ticker=ticker,
        limit=limit,
    )


@router.get("/{run_id}/export")
def export_closed_lots(
    run_id: str,
    request: Request,
    export_format: Literal["csv", "json"] = Query(default="csv", alias="format"),
    path_id: str | None = None,
    ticker: str | None = None,
    tax_year: int | None = None,
    term: Literal["ST", "LT"] | None = None,
    strategy_role: Literal["direct", "benchmark"] = "direct",
) -> Response:
    store = _get_store(request)
    _require_run_exists(store, run_id)
    lots = store.query_closed_lots(
        run_id=run_id,
        path_id=path_id,
        ticker=ticker,
        tax_year=tax_year,
        term=term,
        strategy_role=strategy_role,
        limit=None,
    )
    safe_run = _safe_filename_part(run_id)
    suffix = f"_{_safe_filename_part(path_id)}" if path_id else ""
    if export_format == "json":
        payload = json.dumps(lots, indent=2)
        return Response(
            content=payload,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="closed_lots_{safe_run}{suffix}.json"'
            },
        )

    buf = io.StringIO()
    fieldnames = (
        list(lots[0].keys())
        if lots
        else [
            "path_id",
            "close_id",
            "source_lot_id",
            "ticker",
            "sector",
            "acquired_date",
            "sold_date",
            "tax_year",
            "holding_days",
            "term",
            "quantity",
            "proceeds_total",
            "cost_basis_total",
            "raw_realized_gain_loss",
            "disallowed_wash_loss",
            "adjusted_realized_gain_loss",
            "reason",
        ]
    )
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(lots)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="closed_lots_{safe_run}{suffix}.csv"'
        },
    )
