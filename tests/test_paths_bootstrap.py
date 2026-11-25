"""Unit tests for historical, rolling_window, and cross-sectional block_bootstrap path generators."""

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from idx_engine.fetch import DataStore, generate_synthetic_sp500_dataset
from idx_engine.models import PathSpec
from idx_engine.sim.paths import generate_paths


def test_rolling_window_and_deterministic_block_bootstrap(tmp_path: Path) -> None:
    store = DataStore(data_dir=tmp_path / "data")
    generate_synthetic_sp500_dataset(
        store,
        dataset_id="boot_ds",
        start_date=date(2021, 1, 4),
        trading_days=200,
        max_constituents=10,
        seed=7,
    )
    ds = store.load_dataset("boot_ds")

    # Rolling windows: 200 days, horizon=100, step=25 -> (200 - 100)//25 + 1 = 5 windows
    roll_spec = PathSpec(mode="rolling_window", horizon_days=100, step_days=25, num_paths=10)
    roll_paths = generate_paths(ds, roll_spec)
    assert len(roll_paths) == 5
    assert all(len(p.trading_dates) == 100 for p in roll_paths)
    # Day 0 return on every rolling slice is zeroed
    for p in roll_paths:
        assert p.returns_df.select(ds.tickers).row(0) == tuple([0.0] * len(ds.tickers))

    # Block bootstrap determinism & cross-sectional alignment
    boot_spec = PathSpec(
        mode="block_bootstrap",
        horizon_days=120,
        block_size_days=20,
        num_paths=8,
        seed=42,
    )
    paths_a = generate_paths(ds, boot_spec)
    paths_b = generate_paths(ds, boot_spec)
    assert len(paths_a) == 8

    for pa, pb in zip(paths_a, paths_b, strict=True):
        assert pa.path_id == pb.path_id
        assert pa.trading_dates == pb.trading_dates
        np.testing.assert_allclose(
            pa.prices_df.select(ds.tickers).to_numpy(),
            pb.prices_df.select(ds.tickers).to_numpy(),
        )
        # Weights sum to 1.0 on every day
        w_sums = pa.weights_df.select(ds.tickers).to_numpy().sum(axis=1)
        np.testing.assert_allclose(w_sums, np.ones(120), atol=1e-9)

    # Verify cross-sectional structure is preserved: every day > 0 on a bootstrap path
    # matches an exact historical cross-sectional return vector from `ds.returns_df`!
    src_vecs = ds.returns_df.select(ds.tickers).to_numpy()
    p0_vecs = paths_a[0].returns_df.select(ds.tickers).to_numpy()
    for day_idx in range(1, 120):
        diffs = np.max(np.abs(src_vecs - p0_vecs[day_idx : day_idx + 1, :]), axis=1)
        assert float(np.min(diffs)) == pytest.approx(0.0, abs=1e-12)


def test_rolling_window_raises_when_dataset_too_short(tmp_path: Path) -> None:
    store = DataStore(data_dir=tmp_path / "data")
    generate_synthetic_sp500_dataset(
        store, dataset_id="short_ds", trading_days=50, max_constituents=5
    )
    ds = store.load_dataset("short_ds")
    with pytest.raises(ValueError, match="fewer than rolling_window"):
        generate_paths(ds, PathSpec(mode="rolling_window", horizon_days=100))
