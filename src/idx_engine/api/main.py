"""FastAPI application factory serving `/api/*` and optional built React static UI."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from idx_engine.api.jobs import JobCoordinator
from idx_engine.api.routes import datasets, ledger, runs, sweeps
from idx_engine.api.schemas import HealthResponse
from idx_engine.fetch import DataStore


def create_app(
    data_dir: str | Path | None = None,
    web_dist_dir: str | Path | None = None,
) -> FastAPI:
    resolved_data = Path(data_dir or os.environ.get("IDX_DATA_DIR", "data"))
    store = DataStore(data_dir=resolved_data)
    jobs = JobCoordinator(store=store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            jobs.shutdown()

    app = FastAPI(
        title="Direct Indexing Simulator & Workbench API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.jobs = jobs

    cors_env = os.environ.get(
        "IDX_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    allowed_origins = [o.strip() for o in cors_env.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        s: DataStore = request.app.state.store
        return HealthResponse(
            status="ok",
            dataset_count=len(s.list_datasets()),
            run_count=len(s.list_runs()),
            sweep_count=len(s.list_sweeps()),
        )

    app.include_router(datasets.router)
    app.include_router(runs.router)
    app.include_router(sweeps.router)
    app.include_router(ledger.router)

    dist_root = Path(
        web_dist_dir
        or os.environ.get(
            "IDX_WEB_DIST",
            str(Path(__file__).resolve().parents[3] / "web" / "dist"),
        )
    ).resolve()
    index_file = dist_root / "index.html"
    if index_file.is_file():
        assets_dir = dist_root / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_spa(full_path: str) -> FileResponse:
            cleaned = full_path.lstrip("/")
            if cleaned == "api" or cleaned.startswith("api/"):
                raise HTTPException(status_code=404, detail="API route not found")
            if cleaned:
                candidate = (dist_root / cleaned).resolve()
                if not candidate.is_relative_to(dist_root):
                    raise HTTPException(status_code=404, detail="Not found")
                if candidate.is_file():
                    return FileResponse(candidate)
            return FileResponse(index_file)

    return app
