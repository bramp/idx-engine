"""Paired comparison metrics, percentile distributions, tracking error, and validation guardrails."""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from idx_engine.models import (
    ZERO,
    DistributionSummary,
    PairedPathComparison,
    PathMode,
    PathOutcome,
    ValidationStatus,
)

CENTS = Decimal("0.01")


def _dec_cents(val: float | Decimal) -> Decimal:
    return Decimal(str(val)).quantize(CENTS, rounding=ROUND_HALF_UP)


def compute_annualized_tracking_error_bps(
    direct_daily_returns: Sequence[float],
    bench_daily_returns: Sequence[float],
) -> float:
    """Compute annualized active return standard deviation in basis points (`bps`)."""
    if len(direct_daily_returns) < 2 or len(direct_daily_returns) != len(bench_daily_returns):
        return 0.0
    diff = np.asarray(direct_daily_returns, dtype=np.float64) - np.asarray(
        bench_daily_returns, dtype=np.float64
    )
    # Exclude day 0 (deployment day with 0 return).
    active = diff[1:]
    if active.size < 2:
        return 0.0
    te = float(np.std(active, ddof=1) * math.sqrt(252.0) * 10000.0)
    return round(te, 2)


def compute_cagr(
    starting_wealth: Decimal,
    ending_wealth: Decimal,
    net_contributions: Decimal,
    trading_days: int,
) -> float:
    """Compute annualized compound return adjusted for net capital contributions."""
    years = max(trading_days / 252.0, 1.0 / 252.0)
    invested_base = float(starting_wealth) + 0.5 * float(net_contributions)
    if invested_base <= 0.0:
        return 0.0
    terminal_adj = float(ending_wealth) - 0.5 * float(net_contributions)
    if terminal_adj <= 0.0:
        return -1.0
    ratio = terminal_adj / invested_base
    return round(float(ratio ** (1.0 / years) - 1.0), 6)


def build_paired_comparison(
    direct: PathOutcome,
    benchmark: PathOutcome,
) -> PairedPathComparison:
    """Construct an aligned paired comparison between direct indexing and ETF baseline."""
    if direct.path_id != benchmark.path_id:
        raise ValueError(
            f"Unaligned comparison forbidden (Rule #1): {direct.path_id} != {benchmark.path_id}"
        )
    realized_delta = direct.after_tax_realized_wealth - benchmark.after_tax_realized_wealth
    liq_delta = direct.after_tax_liquidation_wealth - benchmark.after_tax_liquidation_wealth
    cagr_delta_bps = round((direct.after_tax_cagr - benchmark.after_tax_cagr) * 10000.0, 2)
    return PairedPathComparison(
        path_id=direct.path_id,
        direct_outcome=direct,
        benchmark_outcome=benchmark,
        paired_realized_wealth_delta=_dec_cents(realized_delta),
        paired_liquidation_wealth_delta=_dec_cents(liq_delta),
        paired_after_tax_cagr_delta_bps=cagr_delta_bps,
        annualized_tracking_error_bps=direct.annualized_tracking_error_bps,
    )


def summarize_paired_distribution(
    run_id: str,
    dataset_id: str,
    dataset_version: str,
    survivorship_biased: bool,
    strategy_name: str,
    benchmark_name: str,
    path_mode: PathMode,
    comparisons: Sequence[PairedPathComparison],
    horizon_days: int,
) -> DistributionSummary:
    """Aggregate path-level paired outcomes into percentile distribution metrics (Rule #3 & #4)."""
    if not comparisons:
        raise ValueError("Cannot summarize empty comparisons list")

    n = len(comparisons)
    years = max(horizon_days / 252.0, 1.0 / 252.0)

    # Rule #4: Block 'validated' claims if dataset is survivorship-biased or only 1 path
    if survivorship_biased:
        val_status: ValidationStatus = "exploratory_survivorship_biased"
    elif n == 1:
        val_status = "single_path"
    else:
        val_status = "validated"

    liq_deltas = np.array(
        [float(c.paired_liquidation_wealth_delta) for c in comparisons], dtype=np.float64
    )
    strat_liq = np.array(
        [float(c.direct_outcome.after_tax_liquidation_wealth) for c in comparisons],
        dtype=np.float64,
    )
    bench_liq = np.array(
        [float(c.benchmark_outcome.after_tax_liquidation_wealth) for c in comparisons],
        dtype=np.float64,
    )
    strat_cagrs = np.array(
        [c.direct_outcome.after_tax_cagr for c in comparisons], dtype=np.float64
    )
    bench_cagrs = np.array(
        [c.benchmark_outcome.after_tax_cagr for c in comparisons], dtype=np.float64
    )
    te_bps = np.array([c.annualized_tracking_error_bps for c in comparisons], dtype=np.float64)
    turnovers = np.array(
        [c.direct_outcome.annual_turnover for c in comparisons], dtype=np.float64
    )
    harv_losses = np.array(
        [float(c.direct_outcome.harvested_gross_losses) for c in comparisons], dtype=np.float64
    )
    wash_losses = np.array(
        [float(c.direct_outcome.disallowed_wash_sale_losses) for c in comparisons],
        dtype=np.float64,
    )
    lots_per_yr = np.array(
        [c.direct_outcome.closed_lot_count / years for c in comparisons], dtype=np.float64
    )

    win_rate = float(np.mean(liq_deltas > 0.0))
    p10, p25, p50, p75, p90 = np.percentile(liq_deltas, [10, 25, 50, 75, 90])

    return DistributionSummary(
        run_id=run_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        survivorship_biased=survivorship_biased,
        validation_status=val_status,
        strategy_name=strategy_name,
        benchmark_name=benchmark_name,
        path_mode=path_mode,
        path_count=n,
        win_rate_vs_benchmark_liquidation=round(win_rate, 4),
        median_paired_wealth_delta=_dec_cents(p50),
        p10_paired_wealth_delta=_dec_cents(p10),
        p25_paired_wealth_delta=_dec_cents(p25),
        p75_paired_wealth_delta=_dec_cents(p75),
        p90_paired_wealth_delta=_dec_cents(p90),
        median_strategy_liquidation_wealth=_dec_cents(float(np.median(strat_liq))),
        median_benchmark_liquidation_wealth=_dec_cents(float(np.median(bench_liq))),
        median_after_tax_cagr=round(float(np.median(strat_cagrs)), 6),
        benchmark_median_after_tax_cagr=round(float(np.median(bench_cagrs)), 6),
        median_tracking_error_bps=round(float(np.median(te_bps)), 2),
        p90_tracking_error_bps=round(float(np.percentile(te_bps, 90)), 2),
        median_annual_turnover=round(float(np.median(turnovers)), 4),
        median_harvested_losses=_dec_cents(
            float(np.median(harv_losses)) if harv_losses.size else ZERO
        ),
        median_disallowed_wash_losses=_dec_cents(
            float(np.median(wash_losses)) if wash_losses.size else ZERO
        ),
        median_closed_lots_per_year=round(float(np.median(lots_per_yr)), 2),
    )
