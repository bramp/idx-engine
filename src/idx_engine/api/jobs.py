"""Background job coordinator backed by DataStore's thread-safe DuckDB locking."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from idx_engine.api.schemas import JobStatusResponse
from idx_engine.config import SimulationRunConfig, SweepConfig
from idx_engine.fetch.store import DataStore
from idx_engine.sim.engine import run_simulation_on_dataset
from idx_engine.sim.sweep import run_parameter_sweep_on_dataset

MAX_RETAINED_TERMINAL_JOBS = 500


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _JobCancelledError(RuntimeError):
    """Raised internally when a job is cancelled during coordinator shutdown."""


@dataclass
class _JobRecord:
    job_id: str
    job_type: str
    status: str
    progress_completed: int
    progress_total: int
    created_at_iso: str
    updated_at_iso: str
    result_id: str | None = None
    error: str | None = None

    def to_schema(self) -> JobStatusResponse:
        return JobStatusResponse(
            job_id=self.job_id,
            job_type=self.job_type,  # type: ignore[arg-type]
            status=self.status,  # type: ignore[arg-type]
            progress_completed=self.progress_completed,
            progress_total=self.progress_total,
            result_id=self.result_id,
            error=self.error,
            created_at_iso=self.created_at_iso,
            updated_at_iso=self.updated_at_iso,
        )


class JobCoordinator:
    """Executes multi-path simulation runs and parameter sweeps asynchronously."""

    def __init__(
        self,
        store: DataStore,
        max_workers: int = 2,
        max_retained_jobs: int = MAX_RETAINED_TERMINAL_JOBS,
    ) -> None:
        self.store = store
        self._max_retained_jobs = max_retained_jobs
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="idx-job")
        self._state_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._jobs: dict[str, _JobRecord] = {}

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _evict_old_terminal_jobs_unlocked(self) -> None:
        terminal = [r for r in self._jobs.values() if r.status in ("completed", "failed")]
        if len(terminal) <= self._max_retained_jobs:
            return
        terminal.sort(key=lambda r: r.updated_at_iso)
        excess = len(terminal) - self._max_retained_jobs
        for old in terminal[:excess]:
            self._jobs.pop(old.job_id, None)

    def get_job(self, job_id: str) -> JobStatusResponse:
        with self._state_lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                raise KeyError(f"Job '{job_id}' not found")
            return rec.to_schema()

    def list_jobs(self) -> list[JobStatusResponse]:
        with self._state_lock:
            ordered = sorted(self._jobs.values(), key=lambda r: r.created_at_iso, reverse=True)
            return [r.to_schema() for r in ordered]

    def submit_run(self, cfg: SimulationRunConfig) -> JobStatusResponse:
        if self._shutdown_event.is_set():
            raise RuntimeError("JobCoordinator is shutting down")
        now = _now_iso()
        est_total = 1 if cfg.path.mode == "historical" else max(1, cfg.path.num_paths)
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        rec = _JobRecord(
            job_id=job_id,
            job_type="run",
            status="queued",
            progress_completed=0,
            progress_total=est_total,
            created_at_iso=now,
            updated_at_iso=now,
        )
        with self._state_lock:
            self._jobs[job_id] = rec
            self._evict_old_terminal_jobs_unlocked()
            try:
                self._executor.submit(self._execute_run_job, job_id, cfg)
            except Exception:
                self._jobs.pop(job_id, None)
                raise
            return rec.to_schema()

    def _execute_run_job(self, job_id: str, cfg: SimulationRunConfig) -> None:
        if self._shutdown_event.is_set():
            with self._state_lock:
                rec = self._jobs.get(job_id)
                if rec is not None:
                    rec.status = "failed"
                    rec.error = "Server shutting down"
                    rec.updated_at_iso = _now_iso()
            return

        with self._state_lock:
            self._jobs[job_id].status = "running"
            self._jobs[job_id].updated_at_iso = _now_iso()

        def _on_progress(done: int, total: int) -> None:
            if self._shutdown_event.is_set():
                raise _JobCancelledError("Job cancelled during shutdown")
            with self._state_lock:
                rec = self._jobs[job_id]
                rec.progress_completed = done
                rec.progress_total = total
                rec.updated_at_iso = _now_iso()

        try:
            dataset = self.store.load_dataset(
                dataset_id=cfg.dataset.dataset_id,
                version=cfg.dataset.version,
                start_date=cfg.dataset.start_date,
                end_date=cfg.dataset.end_date,
            )
            res = run_simulation_on_dataset(dataset, cfg, progress_cb=_on_progress)
            if self._shutdown_event.is_set():
                raise _JobCancelledError("Job cancelled during shutdown")
            self.store.save_run_results(
                summary=res.summary,
                config_dict=cfg.model_dump(mode="json"),
                comparisons=res.comparisons,
                direct_snapshots_by_path=res.direct_snapshots_by_path or None,
                bench_snapshots_by_path=res.bench_snapshots_by_path or None,
                closed_lots_by_path=res.closed_lots_by_path or None,
                wash_sales_by_path=res.wash_sales_by_path or None,
            )
            with self._state_lock:
                rec = self._jobs[job_id]
                rec.status = "completed"
                rec.result_id = res.run_id
                rec.progress_completed = rec.progress_total
                rec.updated_at_iso = _now_iso()
                self._evict_old_terminal_jobs_unlocked()
        except Exception as exc:
            with self._state_lock:
                rec = self._jobs[job_id]
                rec.status = "failed"
                rec.error = str(exc)
                rec.updated_at_iso = _now_iso()
                self._evict_old_terminal_jobs_unlocked()

    def submit_sweep(self, sweep_cfg: SweepConfig) -> JobStatusResponse:
        if self._shutdown_event.is_set():
            raise RuntimeError("JobCoordinator is shutting down")
        now = _now_iso()
        total_pts = 1
        for vals in sweep_cfg.grid.values():
            total_pts *= max(1, len(vals))
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        rec = _JobRecord(
            job_id=job_id,
            job_type="sweep",
            status="queued",
            progress_completed=0,
            progress_total=total_pts,
            created_at_iso=now,
            updated_at_iso=now,
        )
        with self._state_lock:
            self._jobs[job_id] = rec
            self._evict_old_terminal_jobs_unlocked()
            try:
                self._executor.submit(self._execute_sweep_job, job_id, sweep_cfg)
            except Exception:
                self._jobs.pop(job_id, None)
                raise
            return rec.to_schema()

    def _execute_sweep_job(self, job_id: str, sweep_cfg: SweepConfig) -> None:
        if self._shutdown_event.is_set():
            with self._state_lock:
                rec = self._jobs.get(job_id)
                if rec is not None:
                    rec.status = "failed"
                    rec.error = "Server shutting down"
                    rec.updated_at_iso = _now_iso()
            return

        with self._state_lock:
            self._jobs[job_id].status = "running"
            self._jobs[job_id].updated_at_iso = _now_iso()

        def _on_progress(done: int, total: int) -> None:
            if self._shutdown_event.is_set():
                raise _JobCancelledError("Job cancelled during shutdown")
            with self._state_lock:
                rec = self._jobs[job_id]
                rec.progress_completed = done
                rec.progress_total = total
                rec.updated_at_iso = _now_iso()

        try:
            dataset = self.store.load_dataset(
                dataset_id=sweep_cfg.base.dataset.dataset_id,
                version=sweep_cfg.base.dataset.version,
                start_date=sweep_cfg.base.dataset.start_date,
                end_date=sweep_cfg.base.dataset.end_date,
            )
            res = run_parameter_sweep_on_dataset(dataset, sweep_cfg, progress_cb=_on_progress)
            if self._shutdown_event.is_set():
                raise _JobCancelledError("Job cancelled during shutdown")
            self.store.save_sweep(
                sweep_id=res.sweep_id,
                name=res.name,
                dataset_id=res.dataset_id,
                dataset_version=res.dataset_version,
                walk_forward_split_ratio=res.walk_forward_split_ratio,
                config_dict=sweep_cfg.model_dump(mode="json"),
                rows=res.rows,
            )
            with self._state_lock:
                rec = self._jobs[job_id]
                rec.status = "completed"
                rec.result_id = res.sweep_id
                rec.progress_completed = rec.progress_total
                rec.updated_at_iso = _now_iso()
                self._evict_old_terminal_jobs_unlocked()
        except Exception as exc:
            with self._state_lock:
                rec = self._jobs[job_id]
                rec.status = "failed"
                rec.error = str(exc)
                rec.updated_at_iso = _now_iso()
                self._evict_old_terminal_jobs_unlocked()
