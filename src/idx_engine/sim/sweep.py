"""Multi-parameter sensitivity sweeps, plateau scoring, and walk-forward OOS evaluation."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from idx_engine.config import SimulationRunConfig, SweepConfig
from idx_engine.fetch.store import DataStore, MarketDataset
from idx_engine.models import SweepRow
from idx_engine.sim.engine import run_simulation_on_dataset

SweepProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class SweepExecutionResult:
    sweep_id: str
    name: str
    dataset_id: str
    dataset_version: str
    walk_forward_split_ratio: float | None
    rows: list[SweepRow]


def _apply_dot_overrides(
    base_dict: dict[str, Any],
    overrides: dict[str, Any],
) -> SimulationRunConfig:
    mutated = copy.deepcopy(base_dict)
    for dot_key, val in overrides.items():
        section, field_name = dot_key.split(".", 1)
        mutated[section][field_name] = val
    # Give each parameter point a descriptive strategy label
    param_suffix = "_".join(f"{k.split('.')[-1]}={v}" for k, v in overrides.items())
    if param_suffix:
        mutated["strategy"]["name"] = f"{mutated['strategy']['name']}[{param_suffix}]"
    return SimulationRunConfig.model_validate(mutated)


def _is_ordered_numeric_axis(vals: list[Any]) -> bool:
    if len(vals) < 2:
        return False
    for v in vals:
        if isinstance(v, bool) or v is None:
            return False
        try:
            float(v)
        except (TypeError, ValueError):
            return False
    return True


def _compute_plateau_scores(
    grid_keys: list[str],
    grid_values: list[list[Any]],
    coord_list: list[tuple[int, ...]],
    raw_deltas: list[float],
) -> list[float]:
    """Compute a local neighborhood robustness score (`plateau_score`, Rule #6).

    For each grid point, averages its median paired liquidation wealth delta with all
    immediate Manhattan-distance-1 neighbors along ordered numeric axes in parameter space,
    penalizing isolated spikes whose neighboring numeric parameter choices collapse.
    """
    if not raw_deltas:
        return []
    if not grid_keys:
        return [round(raw_deltas[0], 2)]

    delta_by_coord = dict(zip(coord_list, raw_deltas, strict=True))
    dims = [len(v) for v in grid_values]
    numeric_axes = [_is_ordered_numeric_axis(v) for v in grid_values]
    scores: list[float] = []

    for coord in coord_list:
        neighborhood = [delta_by_coord[coord]]
        for axis, idx in enumerate(coord):
            if not numeric_axes[axis]:
                continue
            for step in (-1, 1):
                nbr_idx = idx + step
                if 0 <= nbr_idx < dims[axis]:
                    nbr_coord = list(coord)
                    nbr_coord[axis] = nbr_idx
                    neighborhood.append(delta_by_coord[tuple(nbr_coord)])
        mean_val = float(np.mean(neighborhood))
        dispersion_penalty = float(np.std(neighborhood)) * 0.25
        scores.append(round(mean_val - dispersion_penalty, 2))

    return scores


def run_parameter_sweep_on_dataset(
    dataset: MarketDataset,
    sweep_cfg: SweepConfig,
    progress_cb: SweepProgressCallback | None = None,
) -> SweepExecutionResult:
    """Execute a Cartesian parameter grid on `dataset` (plus optional walk-forward OOS split)."""
    sweep_id = sweep_cfg.compute_sweep_id(dataset.metadata.version)
    base_dict = sweep_cfg.base.model_dump(mode="json")

    grid_keys = sorted(sweep_cfg.grid.keys())
    grid_values = [sweep_cfg.grid[k] for k in grid_keys]

    if grid_keys:
        index_ranges = [range(len(vals)) for vals in grid_values]
        coord_list = list(itertools.product(*index_ranges))
    else:
        coord_list = [()]

    if sweep_cfg.walk_forward_split_ratio is not None:
        if len(dataset.trading_dates) < 20:
            raise ValueError(
                f"Dataset has {len(dataset.trading_dates)} trading days; "
                "walk_forward_split_ratio requires at least 20 trading days"
            )
        split_idx = max(
            10,
            min(
                len(dataset.trading_dates) - 10,
                int(len(dataset.trading_dates) * sweep_cfg.walk_forward_split_ratio),
            ),
        )
        split_date = dataset.trading_dates[split_idx - 1]
        oos_start = dataset.trading_dates[split_idx]
        is_dataset = dataset.slice_dates(end_date=split_date)
        oos_dataset: MarketDataset | None = dataset.slice_dates(start_date=oos_start)
    else:
        is_dataset = dataset
        oos_dataset = None

    preliminary_rows: list[tuple[str, dict[str, Any], Any, Any]] = []
    is_deltas: list[float] = []
    total_points = len(coord_list)

    for pt_idx, coord in enumerate(coord_list):
        overrides = {
            grid_keys[axis]: grid_values[axis][val_i]
            for axis, val_i in enumerate(coord)
        }
        run_cfg = _apply_dot_overrides(base_dict, overrides)
        is_cfg = run_cfg.model_copy(deep=True)
        if oos_dataset is not None:
            is_cfg.dataset.start_date = is_dataset.metadata.start_date
            is_cfg.dataset.end_date = is_dataset.metadata.end_date
        if (
            is_cfg.path.mode != "block_bootstrap"
            and is_cfg.path.horizon_days > len(is_dataset.trading_dates)
        ):
            is_cfg.path.horizon_days = len(is_dataset.trading_dates)

        is_res = run_simulation_on_dataset(is_dataset, is_cfg)
        oos_sum = None
        if oos_dataset is not None:
            oos_cfg = run_cfg.model_copy(deep=True)
            oos_cfg.dataset.start_date = oos_dataset.metadata.start_date
            oos_cfg.dataset.end_date = oos_dataset.metadata.end_date
            if (
                oos_cfg.path.mode != "block_bootstrap"
                and oos_cfg.path.horizon_days > len(oos_dataset.trading_dates)
            ):
                oos_cfg.path.horizon_days = len(oos_dataset.trading_dates)
            # Distinct deterministic OOS seed so bootstrap paths don't reuse IS seed draws
            oos_cfg.path.seed = run_cfg.path.seed + 10000
            oos_res = run_simulation_on_dataset(oos_dataset, oos_cfg)
            oos_sum = oos_res.summary

        preliminary_rows.append((is_res.run_id, overrides, is_res.summary, oos_sum))
        is_deltas.append(float(is_res.summary.median_paired_wealth_delta))

        if progress_cb is not None:
            progress_cb(pt_idx + 1, total_points)

    plateau_scores = _compute_plateau_scores(grid_keys, grid_values, coord_list, is_deltas)
    final_rows = [
        SweepRow(
            sweep_id=sweep_id,
            run_id=r_id,
            params=params,
            in_sample_summary=is_sum,
            out_of_sample_summary=oos_sum,
            plateau_score=p_score,
        )
        for (r_id, params, is_sum, oos_sum), p_score in zip(
            preliminary_rows, plateau_scores, strict=True
        )
    ]

    return SweepExecutionResult(
        sweep_id=sweep_id,
        name=sweep_cfg.name,
        dataset_id=dataset.metadata.dataset_id,
        dataset_version=dataset.metadata.version,
        walk_forward_split_ratio=sweep_cfg.walk_forward_split_ratio,
        rows=final_rows,
    )


def execute_and_persist_sweep(
    store: DataStore,
    sweep_cfg: SweepConfig,
    persist: bool = True,
    progress_cb: SweepProgressCallback | None = None,
) -> SweepExecutionResult:
    dataset = store.load_dataset(
        dataset_id=sweep_cfg.base.dataset.dataset_id,
        version=sweep_cfg.base.dataset.version,
        start_date=sweep_cfg.base.dataset.start_date,
        end_date=sweep_cfg.base.dataset.end_date,
    )
    res = run_parameter_sweep_on_dataset(dataset, sweep_cfg, progress_cb=progress_cb)
    if persist:
        store.save_sweep(
            sweep_id=res.sweep_id,
            name=res.name,
            dataset_id=res.dataset_id,
            dataset_version=res.dataset_version,
            walk_forward_split_ratio=res.walk_forward_split_ratio,
            config_dict=sweep_cfg.model_dump(mode="json"),
            rows=res.rows,
        )
    return res
