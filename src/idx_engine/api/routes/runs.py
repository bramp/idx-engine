"""Simulation run and background job polling routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from idx_engine.api.jobs import JobCoordinator
from idx_engine.api.schemas import JobStatusResponse
from idx_engine.config import SimulationRunConfig
from idx_engine.fetch import DataStore

router = APIRouter(tags=["runs"])


def _get_store(request: Request) -> DataStore:
    store: DataStore = request.app.state.store
    return store


def _get_jobs(request: Request) -> JobCoordinator:
    jobs: JobCoordinator = request.app.state.jobs
    return jobs


def _compute_notable_paths(paths: list[dict[str, Any]]) -> dict[str, str | None]:
    if not paths:
        return {"median_path_id": None, "p10_path_id": None, "max_te_path_id": None}
    by_delta = sorted(paths, key=lambda p: (p["paired_liquidation_delta"], p["path_id"]))
    n = len(by_delta)
    median_id = by_delta[n // 2]["path_id"]
    p10_idx = max(0, min(n - 1, round(0.10 * (n - 1))))
    p10_id = by_delta[p10_idx]["path_id"]
    by_te = sorted(paths, key=lambda p: (p["tracking_error_bps"], p["path_id"]), reverse=True)
    max_te_id = by_te[0]["path_id"]
    return {
        "median_path_id": median_id,
        "p10_path_id": p10_id,
        "max_te_path_id": max_te_id,
    }


@router.get("/api/runs")
def list_runs(request: Request) -> list[dict[str, Any]]:
    return _get_store(request).list_runs()


@router.post("/api/runs", status_code=202, response_model=JobStatusResponse)
def submit_simulation_run(
    cfg: SimulationRunConfig,
    request: Request,
) -> JobStatusResponse:
    store = _get_store(request)
    try:
        resolved_ver = store.resolve_dataset_version(cfg.dataset.dataset_id, cfg.dataset.version)
        ds_check = store.load_dataset(
            dataset_id=cfg.dataset.dataset_id,
            version=resolved_ver,
            start_date=cfg.dataset.start_date,
            end_date=cfg.dataset.end_date,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if cfg.path.mode == "rolling_window" and len(ds_check.trading_dates) < cfg.path.horizon_days:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Dataset has {len(ds_check.trading_dates)} trading days, fewer than "
                f"rolling_window horizon_days={cfg.path.horizon_days}"
            ),
        )

    pinned_cfg = cfg.model_copy(
        update={"dataset": cfg.dataset.model_copy(update={"version": resolved_ver})}
    )
    return _get_jobs(request).submit_run(pinned_cfg)


@router.get("/api/runs/{run_id}")
def get_simulation_run(run_id: str, request: Request) -> dict[str, Any]:
    try:
        data = _get_store(request).get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    data["notable_paths"] = _compute_notable_paths(data.get("paths", []))
    return data


@router.get("/api/jobs", response_model=list[JobStatusResponse])
def list_jobs(request: Request) -> list[JobStatusResponse]:
    return _get_jobs(request).list_jobs()


@router.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str, request: Request) -> JobStatusResponse:
    try:
        return _get_jobs(request).get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
