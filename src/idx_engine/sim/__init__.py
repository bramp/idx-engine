"""Path generation, paired daily simulation loop, distributional metrics, and parameter sweeps."""

from idx_engine.sim.engine import (
    SimulationRunResult,
    SinglePathRunArtifacts,
    execute_and_persist_run,
    run_simulation_on_dataset,
    simulate_paired_path,
)
from idx_engine.sim.metrics import (
    build_paired_comparison,
    compute_annualized_tracking_error_bps,
    compute_cagr,
    summarize_paired_distribution,
)
from idx_engine.sim.paths import generate_paths
from idx_engine.sim.sweep import (
    SweepExecutionResult,
    execute_and_persist_sweep,
    run_parameter_sweep_on_dataset,
)

__all__ = [
    "SimulationRunResult",
    "SinglePathRunArtifacts",
    "SweepExecutionResult",
    "build_paired_comparison",
    "compute_annualized_tracking_error_bps",
    "compute_cagr",
    "execute_and_persist_run",
    "execute_and_persist_sweep",
    "generate_paths",
    "run_parameter_sweep_on_dataset",
    "run_simulation_on_dataset",
    "simulate_paired_path",
    "summarize_paired_distribution",
]
