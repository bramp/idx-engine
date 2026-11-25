"""Deterministic path generators: historical, rolling_window, and cross-sectional block_bootstrap."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from idx_engine.fetch.store import MarketDataset
from idx_engine.models import PathSpec, SimulationPath


def _synthetic_business_dates(anchor_start: date, count: int) -> list[date]:
    dates: list[date] = []
    cur = anchor_start
    while len(dates) < count:
        if cur.weekday() < 5:
            dates.append(cur)
        cur += timedelta(days=1)
    return dates


def _zero_first_row(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    if df.height == 0:
        return df
    first_date = df.get_column("date")[0]
    return df.with_columns(
        *[
            pl.when(pl.col("date") == pl.lit(first_date)).then(0.0).otherwise(pl.col(c)).alias(c)
            for c in cols
        ]
    )


def generate_paths(
    dataset: MarketDataset,
    spec: PathSpec,
    synthetic_anchor_date: date = date(2020, 1, 2),
) -> list[SimulationPath]:
    """Generate `SimulationPath` instances according to `spec.mode` with explicit dates/seeds."""
    n_total = len(dataset.trading_dates)
    if n_total < 2:
        raise ValueError(
            f"Dataset '{dataset.metadata.dataset_id}' must have at least 2 trading days"
        )

    tickers = dataset.tickers

    if spec.mode == "historical":
        horizon = min(spec.horizon_days, n_total)
        dates = dataset.trading_dates[:horizon]
        prices_df = dataset.prices_df.slice(0, horizon)
        returns_df = _zero_first_row(dataset.returns_df.slice(0, horizon), tickers)
        dividends_df = _zero_first_row(dataset.dividends_df.slice(0, horizon), tickers)
        weights_df = dataset.weights_df.slice(0, horizon)
        etf_df = _zero_first_row(dataset.etf_df.slice(0, horizon), ["return", "dividend"])
        actions = tuple(
            ca for ca in dataset.corporate_actions if dates[0] < ca.effective_date <= dates[-1]
        )
        return [
            SimulationPath(
                path_id="hist_0000",
                mode="historical",
                trading_dates=dates,
                returns_df=returns_df,
                weights_df=weights_df,
                prices_df=prices_df,
                dividends_df=dividends_df,
                etf_df=etf_df,
                sectors=dict(dataset.sectors),
                source_start=dates[0],
                source_end=dates[-1],
                corporate_actions=actions,
                survivorship_biased=dataset.metadata.survivorship_biased,
            )
        ]

    if spec.mode == "rolling_window":
        horizon = spec.horizon_days
        if n_total < horizon:
            raise ValueError(
                f"Dataset has {n_total} trading days, fewer than rolling_window horizon_days={horizon}"
            )
        paths: list[SimulationPath] = []
        max_start = n_total - horizon
        for win_idx, start_idx in enumerate(range(0, max_start + 1, spec.step_days)):
            if len(paths) >= spec.num_paths:
                break
            dates = dataset.trading_dates[start_idx : start_idx + horizon]
            prices_df = dataset.prices_df.slice(start_idx, horizon)
            returns_df = _zero_first_row(dataset.returns_df.slice(start_idx, horizon), tickers)
            dividends_df = _zero_first_row(dataset.dividends_df.slice(start_idx, horizon), tickers)
            weights_df = dataset.weights_df.slice(start_idx, horizon)
            etf_df = _zero_first_row(
                dataset.etf_df.slice(start_idx, horizon), ["return", "dividend"]
            )
            actions = tuple(
                ca for ca in dataset.corporate_actions if dates[0] < ca.effective_date <= dates[-1]
            )
            paths.append(
                SimulationPath(
                    path_id=f"roll_{win_idx:04d}_{dates[0].isoformat()}",
                    mode="rolling_window",
                    trading_dates=dates,
                    returns_df=returns_df,
                    weights_df=weights_df,
                    prices_df=prices_df,
                    dividends_df=dividends_df,
                    etf_df=etf_df,
                    sectors=dict(dataset.sectors),
                    source_start=dates[0],
                    source_end=dates[-1],
                    corporate_actions=actions,
                    survivorship_biased=dataset.metadata.survivorship_biased,
                )
            )
        return paths

    if spec.mode == "block_bootstrap":
        horizon = spec.horizon_days
        block_size = min(spec.block_size_days, max(1, n_total - 1))
        max_block_start = n_total - block_size
        if max_block_start < 1:
            max_block_start = 0

        rng = np.random.Generator(np.random.PCG64(spec.seed))
        syn_dates = _synthetic_business_dates(synthetic_anchor_date, horizon)

        src_prices = dataset.prices_df.select(tickers).to_numpy()
        src_returns = dataset.returns_df.select(tickers).to_numpy()
        src_divs = dataset.dividends_df.select(tickers).to_numpy()
        src_weights = dataset.weights_df.select(tickers).to_numpy()
        src_div_yields = src_divs / np.maximum(src_prices, 1e-8)

        # Anchor initial constituent prices to each ticker's first positive bar
        init_prices = np.full(len(tickers), 100.0, dtype=np.float64)
        for j in range(len(tickers)):
            pos_idx = np.flatnonzero(src_prices[:, j] > 0.0)
            if pos_idx.size > 0:
                init_prices[j] = float(src_prices[pos_idx[0], j])

        base_w_arr = src_weights[0, :].astype(np.float64).copy()
        if base_w_arr.sum() <= 0:
            base_w_arr = np.ones(len(tickers), dtype=np.float64) / len(tickers)
        else:
            base_w_arr /= base_w_arr.sum()

        # Detect whether constituent membership varies across the source window (PIT additions/delistings)
        has_pit_membership_changes = bool(
            np.any((src_weights > 0.0) != (src_weights[0:1, :] > 0.0))
        )

        src_etf_close = dataset.etf_df.get_column("close").to_numpy()
        src_etf_ret = dataset.etf_df.get_column("return").to_numpy()
        src_etf_div = dataset.etf_df.get_column("dividend").to_numpy()
        src_etf_div_yield = src_etf_div / np.maximum(src_etf_close, 1e-8)
        init_etf_price = float(src_etf_close[0]) if src_etf_close[0] > 0 else 400.0

        # Preserve any historical unmodeled-constituent residual while aligning cap weights
        lag_src_w = np.vstack([src_weights[0:1, :], src_weights[:-1, :]])
        src_modeled_ret = np.sum(lag_src_w * src_returns, axis=1)
        src_modeled_dy = np.sum(lag_src_w * src_div_yields, axis=1)
        src_etf_ret_resid = src_etf_ret - src_modeled_ret
        src_etf_dy_resid = src_etf_div_yield - src_modeled_dy

        boot_paths: list[SimulationPath] = []
        needed_return_days = horizon - 1

        for p_idx in range(spec.num_paths):
            sampled_indices: list[int] = []
            while len(sampled_indices) < needed_return_days:
                low = 1 if max_block_start >= 1 else 0
                b_start = int(rng.integers(low, max_block_start + 1))
                take = min(block_size, needed_return_days - len(sampled_indices))
                sampled_indices.extend(range(b_start, b_start + take))
            idx_arr = np.array(sampled_indices[:needed_return_days], dtype=np.int64)

            path_ret = np.zeros((horizon, len(tickers)), dtype=np.float64)
            path_div_yield = np.zeros((horizon, len(tickers)), dtype=np.float64)
            if needed_return_days > 0:
                path_ret[1:, :] = src_returns[idx_arr, :]
                path_div_yield[1:, :] = src_div_yields[idx_arr, :]

            path_prices = init_prices[None, :] * np.cumprod(1.0 + path_ret, axis=0)
            path_divs = np.where(
                path_div_yield > 0.0, np.round(path_prices * path_div_yield, 6), 0.0
            )

            if has_pit_membership_changes and needed_return_days > 0:
                # Sample active PIT weights alongside each block so added/delisted stocks
                # carry positive weight only on sampled days where they were active members.
                path_weights = np.zeros((horizon, len(tickers)), dtype=np.float64)
                first_w = src_weights[idx_arr[0], :]
                path_weights[0, :] = first_w if first_w.sum() > 0 else base_w_arr
                path_weights[1:, :] = src_weights[idx_arr, :]
                w_sums = path_weights.sum(axis=1, keepdims=True)
                path_weights = np.where(w_sums > 0.0, path_weights / np.maximum(w_sums, 1e-12), 0.0)
            else:
                # Evolve index cap weights smoothly along bootstrapped constituent returns
                rel_growth = path_prices / np.maximum(path_prices[0:1, :], 1e-8)
                mcap = rel_growth * base_w_arr[None, :]
                path_weights = mcap / np.maximum(mcap.sum(axis=1, keepdims=True), 1e-12)
            lag_path_w = np.vstack([path_weights[0:1, :], path_weights[:-1, :]])

            etf_ret = np.zeros(horizon, dtype=np.float64)
            etf_dy = np.zeros(horizon, dtype=np.float64)
            if needed_return_days > 0:
                etf_ret[1:] = (
                    np.sum(lag_path_w[1:, :] * path_ret[1:, :], axis=1) + src_etf_ret_resid[idx_arr]
                )
                etf_dy[1:] = np.maximum(
                    0.0,
                    np.sum(lag_path_w[1:, :] * path_div_yield[1:, :], axis=1)
                    + src_etf_dy_resid[idx_arr],
                )
            etf_close = init_etf_price * np.cumprod(1.0 + etf_ret)
            etf_div = np.where(etf_dy > 0.0, np.round(etf_close * etf_dy, 6), 0.0)

            n_tok = len(tickers)
            prices_df = pl.DataFrame(
                {"date": syn_dates, **{tickers[j]: path_prices[:, j] for j in range(n_tok)}}
            ).with_columns(pl.col("date").cast(pl.Date))
            returns_df = pl.DataFrame(
                {"date": syn_dates, **{tickers[j]: path_ret[:, j] for j in range(n_tok)}}
            ).with_columns(pl.col("date").cast(pl.Date))
            dividends_df = pl.DataFrame(
                {"date": syn_dates, **{tickers[j]: path_divs[:, j] for j in range(n_tok)}}
            ).with_columns(pl.col("date").cast(pl.Date))
            weights_df = pl.DataFrame(
                {"date": syn_dates, **{tickers[j]: path_weights[:, j] for j in range(n_tok)}}
            ).with_columns(pl.col("date").cast(pl.Date))
            etf_df = pl.DataFrame(
                {
                    "date": syn_dates,
                    "close": etf_close,
                    "return": etf_ret,
                    "dividend": etf_div,
                }
            ).with_columns(pl.col("date").cast(pl.Date))

            boot_paths.append(
                SimulationPath(
                    path_id=f"boot_{p_idx:04d}",
                    mode="block_bootstrap",
                    trading_dates=syn_dates,
                    returns_df=returns_df,
                    weights_df=weights_df,
                    prices_df=prices_df,
                    dividends_df=dividends_df,
                    etf_df=etf_df,
                    sectors=dict(dataset.sectors),
                    source_start=dataset.metadata.start_date,
                    source_end=dataset.metadata.end_date,
                    corporate_actions=(),
                    survivorship_biased=dataset.metadata.survivorship_biased,
                )
            )
        return boot_paths

    raise ValueError(f"Unsupported PathSpec mode: {spec.mode}")
