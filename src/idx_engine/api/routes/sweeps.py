"""Parameter grid sweep routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from idx_engine.api.jobs import JobCoordinator
from idx_engine.api.schemas import JobStatusResponse
from idx_engine.config import SweepConfig
from idx_engine.fetch import DataStore

router = APIRouter(prefix="/api/sweeps", tags=["sweeps"])


def _get_store(request: Request) -> DataStore:
    return request.app.state.store


def _get_jobs(request: Request) -> JobCoordinator:
    return request.app.state.jobs


@router.get("")
def list_sweeps(request: Request) -> list[dict[str, Any]]:
    return _get_store(request).list_sweeps()


@router.post("", status_code=202, response_model=JobStatusResponse)
def submit_parameter_sweep(
    sweep_cfg: SweepConfig,
    request: Request,
) -> JobStatusResponse:
    store = _get_store(request)
    try:
        resolved_ver = store.resolve_dataset_version(
            sweep_cfg.base.dataset.dataset_id, sweep_cfg.base.dataset.version
        )
        ds_check = store.load_dataset(
            dataset_id=sweep_cfg.base.dataset.dataset_id,
            version=resolved_ver,
            start_date=sweep_cfg.base.dataset.start_date,
            end_date=sweep_cfg.base.dataset.end_date,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if sweep_cfg.walk_forward_split_ratio is not None and len(ds_check.trading_dates) < 20:
        raise HTTPException(
            status_code=422,
            detail="Dataset slice must have at least 20 trading days for walk-forward split",
        )

    pinned_base = sweep_cfg.base.model_copy(
        update={"dataset": sweep_cfg.base.dataset.model_copy(update={"version": resolved_ver})}
    )
    pinned_sweep = sweep_cfg.model_copy(update={"base": pinned_base})
    return _get_jobs(request).submit_sweep(pinned_sweep)


@router.get("/{sweep_id}")
def get_parameter_sweep(sweep_id: str, request: Request) -> dict[str, Any]:
    try:
        return _get_store(request).get_sweep(sweep_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
