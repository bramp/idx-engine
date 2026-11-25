"""Integration tests for FastAPI dataset, async 202 run/sweep jobs, and tax ledger export."""

import time
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from idx_engine.api.main import create_app
from idx_engine.fetch import DataStore, generate_synthetic_sp500_dataset


def _wait_for_job(client: TestClient, job_id: str, timeout_s: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout_s}s")


def test_api_empty_store_lists_no_datasets_and_exposes_no_fetch_routes(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "empty_api_data")
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["dataset_count"] == 0
        assert client.get("/api/datasets").json() == []
        assert client.post("/api/datasets/synthetic", json={}).status_code in (404, 405)
        assert client.post("/api/datasets/pull", json={}).status_code in (404, 405)


def test_api_dataset_async_run_sweep_and_ledger_export(tmp_path: Path) -> None:
    data_dir = tmp_path / "api_data"
    store = DataStore(data_dir=data_dir)
    generate_synthetic_sp500_dataset(
        store=store,
        dataset_id="api_syn",
        start_date=date(2022, 1, 3),
        trading_days=140,
        max_constituents=10,
        seed=101,
    )

    app = create_app(data_dir=data_dir)
    with TestClient(app) as client:
        # 1. Health check sees pre-existing dataset without duplicating
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["dataset_count"] == 1

        # 2. Inspect dataset and ticker series
        detail = client.get("/api/datasets/api_syn")
        assert detail.status_code == 200
        first_ticker = detail.json()["constituents"][0]["ticker"]

        series = client.get(f"/api/datasets/api_syn/series/{first_ticker}")
        assert series.status_code == 200
        assert len(series.json()["points"]) == 140

        # 4. Submit async 202 simulation run and poll until completed
        run_submit = client.post(
            "/api/runs",
            json={
                "dataset": {"dataset_id": "api_syn", "version": "latest"},
                "path": {
                    "mode": "block_bootstrap",
                    "horizon_days": 120,
                    "block_size_days": 20,
                    "num_paths": 5,
                    "seed": 42,
                },
                "strategy": {
                    "name": "api_tlh_test",
                    "tlh_enabled": True,
                    "tlh_loss_pct_threshold": "0.02",
                    "tlh_min_loss_dollars": "50",
                    "tlh_check_frequency_days": 5,
                },
                "tax": {"external_lt_gains_annual": "10000"},
                "cashflow": {"initial_capital": "100000"},
            },
        )
        assert run_submit.status_code == 202
        job_done = _wait_for_job(client, run_submit.json()["job_id"])
        assert job_done["status"] == "completed", job_done
        run_id = job_done["result_id"]
        assert run_id is not None

        # 5. Fetch run summary, equity percentile bands, and notable path IDs
        run_detail = client.get(f"/api/runs/{run_id}")
        assert run_detail.status_code == 200
        rj = run_detail.json()
        assert len(rj["equity_bands"]) == 120
        assert rj["notable_paths"]["median_path_id"] is not None

        # 6. Query closed lots & CSV export
        lots_resp = client.get(f"/api/ledger/{run_id}/closed-lots")
        assert lots_resp.status_code == 200
        csv_resp = client.get(f"/api/ledger/{run_id}/export?format=csv")
        assert csv_resp.status_code == 200
        assert "text/csv" in csv_resp.headers["content-type"]
        assert "ticker" in csv_resp.text

        # 7. Submit async 202 parameter sweep and poll until completed
        sweep_submit = client.post(
            "/api/sweeps",
            json={
                "name": "api_sweep",
                "base": {
                    "dataset": {"dataset_id": "api_syn"},
                    "path": {"mode": "historical", "horizon_days": 140},
                    "cashflow": {"initial_capital": "50000"},
                    "save_equity_curves": False,
                    "save_lot_details": False,
                },
                "grid": {"strategy.allow_fractional": [True, False]},
            },
        )
        assert sweep_submit.status_code == 202
        sweep_done = _wait_for_job(client, sweep_submit.json()["job_id"])
        assert sweep_done["status"] == "completed", sweep_done
        sweep_detail = client.get(f"/api/sweeps/{sweep_done['result_id']}")
        assert sweep_detail.status_code == 200
        assert len(sweep_detail.json()["rows"]) == 2


def test_api_security_validation_404s_and_concurrent_duckdb_access(tmp_path: Path) -> None:
    import concurrent.futures

    data_dir = tmp_path / "sec_data"
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html><body>SPA</body></html>", encoding="utf-8")
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("TOP_SECRET", encoding="utf-8")

    store = DataStore(data_dir=data_dir)
    generate_synthetic_sp500_dataset(
        store=store,
        dataset_id="sec_ds",
        start_date=date(2023, 1, 3),
        trading_days=80,
        max_constituents=6,
        seed=9,
    )

    app = create_app(data_dir=data_dir, web_dist_dir=dist_dir)
    with TestClient(app) as client:
        # 1. SPA path traversal & /api/* fallback protection + strict CORS
        trav = client.get("/%2e%2e/secret.txt")
        assert trav.status_code == 404
        assert "TOP_SECRET" not in trav.text

        abs_trav = client.get("/%2fetc/hosts")
        assert abs_trav.status_code in (200, 404)
        assert "SPA" in abs_trav.text or abs_trav.status_code == 404

        unknown_api = client.get("/api/does-not-exist")
        assert unknown_api.status_code == 404
        assert unknown_api.headers["content-type"].startswith("application/json")

        cors_probe = client.get("/api/health", headers={"Origin": "https://evil.example"})
        assert cors_probe.headers.get("access-control-allow-origin") is None

        # 2. Explicit version validation returns 404 synchronously instead of 202
        bad_ver = client.post(
            "/api/runs",
            json={"dataset": {"dataset_id": "sec_ds", "version": "v999"}},
        )
        assert bad_ver.status_code == 404

        # 3. Invalid lot_policy or sweep grid keys/duplicates validated at 422 / deduplicated
        bad_policy = client.post(
            "/api/runs",
            json={
                "dataset": {"dataset_id": "sec_ds", "version": "v1"},
                "strategy": {"lot_policy": "specific_lot"},
            },
        )
        assert bad_policy.status_code == 422

        bad_sweep = client.post(
            "/api/sweeps",
            json={
                "name": "bad_grid",
                "base": {"dataset": {"dataset_id": "sec_ds", "version": "v1"}},
                "grid": {"strategy.unknown_field": [1, 2]},
            },
        )
        assert bad_sweep.status_code == 422

        # 4. Unknown run_id on /api/ledger/* returns 404
        assert client.get("/api/ledger/run_missing/closed-lots").status_code == 404
        assert client.get("/api/ledger/run_missing/wash-sales").status_code == 404
        assert client.get("/api/ledger/run_missing/export").status_code == 404

        # 5. Concurrent reads while background run job is executing & writing to DuckDB
        job_resp = client.post(
            "/api/runs",
            json={
                "dataset": {"dataset_id": "sec_ds", "version": "latest"},
                "path": {"mode": "block_bootstrap", "horizon_days": 60, "num_paths": 6},
                "cashflow": {"initial_capital": "50000"},
            },
        )
        assert job_resp.status_code == 202
        jid = job_resp.json()["job_id"]

        def _hammer_reads() -> list[int]:
            codes: list[int] = []
            for _ in range(15):
                codes.append(client.get("/api/health").status_code)
                codes.append(client.get("/api/runs").status_code)
                codes.append(client.get("/api/datasets").status_code)
            return codes

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futs = [pool.submit(_hammer_reads) for _ in range(3)]
            done = _wait_for_job(client, jid)
            for fut in futs:
                assert all(code == 200 for code in fut.result())

        assert done["status"] == "completed", done
        # Verify "latest" was pinned to "v1" in persisted config
        saved_run = client.get(f"/api/runs/{done['result_id']}").json()
        assert saved_run["config"]["dataset"]["version"] == "v1"
