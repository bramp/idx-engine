"""Tests for multi-parameter sensitivity sweeps, plateau scoring, OOS split, and CLI commands."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from idx_engine.cli import fetch_app, sim_app
from idx_engine.config import (
    CashFlowConfig,
    DatasetRefConfig,
    PathSpecConfig,
    SimulationRunConfig,
    StrategyConfig,
    SweepConfig,
    TaxConfig,
)
from idx_engine.fetch import DataStore, generate_synthetic_sp500_dataset
from idx_engine.sim import execute_and_persist_sweep

runner = CliRunner()


def test_parameter_sweep_with_walk_forward_split_and_plateau_scores(tmp_path: Path) -> None:
    store = DataStore(data_dir=tmp_path / "data")
    generate_synthetic_sp500_dataset(
        store=store,
        dataset_id="sweep_ds",
        start_date=date(2021, 1, 4),
        trading_days=300,
        max_constituents=12,
        seed=55,
    )

    sweep_cfg = SweepConfig(
        name="mini_sweep",
        walk_forward_split_ratio=0.65,
        base=SimulationRunConfig(
            dataset=DatasetRefConfig(dataset_id="sweep_ds"),
            path=PathSpecConfig(
                mode="block_bootstrap",
                horizon_days=150,
                block_size_days=15,
                num_paths=4,
                seed=11,
            ),
            strategy=StrategyConfig(
                sample_size=10,
                tlh_enabled=True,
                tlh_min_loss_dollars=Decimal("100"),
            ),
            tax=TaxConfig(external_lt_gains_annual=Decimal("10000")),
            cashflow=CashFlowConfig(initial_capital=Decimal("100000")),
            save_equity_curves=False,
            save_lot_details=False,
        ),
        grid={
            "strategy.tlh_loss_pct_threshold": ["0.03", "0.06"],
            "strategy.allow_fractional": [True, False],
        },
    )

    res = execute_and_persist_sweep(store, sweep_cfg)
    assert len(res.rows) == 4
    for row in res.rows:
        assert row.in_sample_summary is not None
        assert row.out_of_sample_summary is not None
        assert row.plateau_score is not None

    loaded = store.get_sweep(res.sweep_id)
    assert len(loaded["rows"]) == 4
    # Block-bootstrap OOS runs preserve full 150-day horizon even though OOS slice is 105 days
    for r in res.rows:
        assert r.out_of_sample_summary is not None
        assert r.in_sample_summary is not None
        assert r.in_sample_summary.run_id != sweep_cfg.base.compute_run_id("v1")

    # Duplicate grid values are deduplicated & sorted cleanly on validation without PK collision
    dedup_cfg = SweepConfig.model_validate(
        {
            **sweep_cfg.model_dump(mode="json"),
            "grid": {"strategy.tlh_loss_pct_threshold": ["0.06", "0.03", "0.06"]},
        }
    )
    assert dedup_cfg.grid["strategy.tlh_loss_pct_threshold"] == ["0.03", "0.06"]


def test_cli_fetch_and_sim_commands(tmp_path: Path) -> None:
    data_dir = tmp_path / "cli_data"
    res_syn = runner.invoke(
        fetch_app,
        [
            "synthetic",
            "--dataset-id",
            "cli_demo",
            "--trading-days",
            "120",
            "--max-constituents",
            "8",
            "--data-dir",
            str(data_dir),
        ],
    )
    assert res_syn.exit_code == 0, res_syn.output
    assert "cli_demo@v1" in res_syn.output

    cfg_file = tmp_path / "run.toml"
    cfg_file.write_text(
        """
[dataset]
dataset_id = "cli_demo"

[path]
mode = "historical"
horizon_days = 120

[strategy]
name = "cli_strat"
tlh_enabled = true
tlh_loss_pct_threshold = "0.03"
tlh_min_loss_dollars = "50"

[cashflow]
initial_capital = "100000"
""",
        encoding="utf-8",
    )

    res_run = runner.invoke(sim_app, ["run", str(cfg_file), "--data-dir", str(data_dir)])
    assert res_run.exit_code == 0, res_run.output
    assert "Paired Simulation Summary" in res_run.output
