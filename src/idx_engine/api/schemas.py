"""Pydantic request and response schemas for the FastAPI service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class JobStatusResponse(BaseModel):
    job_id: str
    job_type: Literal["run", "sweep"]
    status: Literal["queued", "running", "completed", "failed"]
    progress_completed: int
    progress_total: int
    result_id: str | None = None
    error: str | None = None
    created_at_iso: str
    updated_at_iso: str


class HealthResponse(BaseModel):
    status: str = "ok"
    dataset_count: int
    run_count: int
    sweep_count: int
    meta: dict[str, Any] = Field(default_factory=dict)
