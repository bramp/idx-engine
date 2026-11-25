"""Core domain types and immutable contracts for idx_engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

import polars as pl

PathMode = Literal["historical", "rolling_window", "block_bootstrap"]
LotPolicy = Literal["HIFO", "FIFO", "specific_lot"]
CorporateActionType = Literal["split", "dividend", "spin_off", "ticker_change"]
TaxTerm = Literal["ST", "LT"]
TaxSettlementMode = Literal["reinvest_and_pay_from_portfolio", "external_cash"]
SubstituteMode = Literal["sector_peer", "sector_etf", "cash"]
RebalanceMode = Literal["none", "monthly", "quarterly", "annual", "drift_band"]
ValidationStatus = Literal["validated", "exploratory_survivorship_biased", "single_path"]

ZERO = Decimal("0")


@dataclass(frozen=True)
class PathSpec:
    """Configuration contract for path generation."""

    mode: PathMode
    horizon_days: int = 252 * 10
    step_days: int = 21
    block_size_days: int = 21
    num_paths: int = 500
    seed: int = 42


@dataclass(frozen=True)
class CorporateAction:
    """Explicit corporate action applied to open lots or portfolio cash."""

    action_id: str
    effective_date: date
    action_type: CorporateActionType
    ticker: str
    ratio_numerator: Decimal = Decimal("1")
    ratio_denominator: Decimal = Decimal("1")
    cash_amount_per_share: Decimal = ZERO
    qualified_fraction: Decimal = Decimal("1")
    target_ticker: str | None = None
    target_sector: str | None = None
    basis_allocation_fraction: Decimal = ZERO


@dataclass(frozen=True)
class SimulationPath:
    """Single aligned market path consumed by both direct_index and etf_baseline.

    Wide Polars DataFrames (`prices_df`, `returns_df`, `dividends_df`, `weights_df`)
    have `"date"` (pl.Date) as their first column followed by one Float64 column per
    constituent ticker, aligned row-for-row with `trading_dates`.

    `etf_df` has columns: `"date"`, `"close"` (Float64), `"return"` (Float64),
    `"dividend"` (Float64).
    """

    path_id: str
    mode: PathMode
    trading_dates: list[date]
    returns_df: pl.DataFrame
    weights_df: pl.DataFrame
    prices_df: pl.DataFrame
    dividends_df: pl.DataFrame
    etf_df: pl.DataFrame
    sectors: dict[str, str]
    source_start: date
    source_end: date
    corporate_actions: tuple[CorporateAction, ...] = ()
    survivorship_biased: bool = False


@dataclass(frozen=True)
class TaxLot:
    """Open tax lot in the portfolio ledger."""

    lot_id: str
    ticker: str
    sector: str
    acquired_date: date
    quantity: Decimal
    cost_basis_total: Decimal
    wash_sale_disallowed: Decimal = ZERO
    original_acquired_date: date | None = None

    @property
    def unit_cost(self) -> Decimal:
        return self.cost_basis_total / self.quantity if self.quantity > ZERO else ZERO


@dataclass(frozen=True)
class TradeFill:
    """Executed buy or sell trade record."""

    fill_id: str
    trade_date: date
    ticker: str
    sector: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    price: Decimal
    gross_amount: Decimal
    reason: str = "rebalance"
    specific_lot_id: str | None = None


@dataclass(frozen=True)
class ClosedLotRecord:
    """Realized disposal of all or part of a TaxLot, before and after wash-sale rules."""

    close_id: str
    source_lot_id: str
    ticker: str
    sector: str
    acquired_date: date
    sold_date: date
    holding_days: int
    term: TaxTerm
    quantity: Decimal
    proceeds_total: Decimal
    cost_basis_total: Decimal
    raw_realized_gain_loss: Decimal
    disallowed_wash_loss: Decimal = ZERO
    adjusted_realized_gain_loss: Decimal = ZERO
    replacement_lot_ids: tuple[str, ...] = ()
    reason: str = "sale"


@dataclass(frozen=True)
class WashSaleAdjustment:
    """Audit trail record linking a disallowed loss sale to its replacement lot."""

    adjustment_id: str
    closed_lot_id: str
    loss_ticker: str
    replacement_ticker: str
    sold_date: date
    replacement_date: date
    replacement_lot_id: str
    matched_quantity: Decimal
    disallowed_loss: Decimal
    tacked_holding_days: int


@dataclass(frozen=True)
class DividendReceipt:
    """Cash dividend received on held shares."""

    receipt_id: str
    pay_date: date
    ticker: str
    shares_held: Decimal
    gross_amount: Decimal
    qualified_amount: Decimal
    ordinary_amount: Decimal


@dataclass(frozen=True)
class AnnualTaxSummary:
    """Annual federal + state tax computation with carryforwards and external gains."""

    tax_year: int
    realized_st_net: Decimal
    realized_lt_net: Decimal
    external_st_gains: Decimal
    external_lt_gains: Decimal
    qualified_dividends: Decimal
    ordinary_dividends: Decimal
    ordinary_income_offset_used: Decimal
    tax_liability_with_portfolio: Decimal
    tax_liability_external_only: Decimal
    net_portfolio_tax_bill: Decimal
    st_loss_carryforward_out: Decimal
    lt_loss_carryforward_out: Decimal


@dataclass(frozen=True)
class DailySnapshot:
    """End-of-day portfolio valuation snapshot."""

    date: date
    market_value: Decimal
    cash: Decimal
    total_equity: Decimal
    cumulative_net_tax_paid: Decimal
    after_tax_wealth_pre_liquidation: Decimal


@dataclass(frozen=True)
class DatasetMetadata:
    """Immutable metadata for a cached market dataset version."""

    dataset_id: str
    version: str
    source: str
    created_at_iso: str
    start_date: date
    end_date: date
    trading_days: int
    ticker_count: int
    benchmark_ticker: str
    survivorship_biased: bool
    missing_bar_fraction: float
    notes: str = ""


@dataclass(frozen=True)
class PathOutcome:
    """Summary outcome of one strategy on one SimulationPath."""

    path_id: str
    strategy_name: str
    ending_market_value: Decimal
    ending_cash: Decimal
    after_tax_realized_wealth: Decimal
    after_tax_liquidation_wealth: Decimal
    after_tax_cagr: float
    annualized_tracking_error_bps: float
    annual_turnover: float
    harvested_gross_losses: Decimal
    disallowed_wash_sale_losses: Decimal
    closed_lot_count: int
    initial_capital: Decimal = ZERO
    net_contributions: Decimal = ZERO
    cumulative_tax_paid: Decimal = ZERO
    liquidation_tax_due: Decimal = ZERO
    remaining_loss_carryforward: Decimal = ZERO
    pre_tax_cagr: float = 0.0
    wash_sale_count: int = 0
    trade_count: int = 0
    avg_cash_drag_bps: float = 0.0


@dataclass(frozen=True)
class PairedPathComparison:
    """Aligned comparison between direct_index and etf_baseline on the same path."""

    path_id: str
    direct_outcome: PathOutcome
    benchmark_outcome: PathOutcome
    paired_realized_wealth_delta: Decimal
    paired_liquidation_wealth_delta: Decimal
    paired_after_tax_cagr_delta_bps: float
    annualized_tracking_error_bps: float


@dataclass(frozen=True)
class DistributionSummary:
    """Distributional metrics across paths for a simulation run."""

    run_id: str
    dataset_id: str
    strategy_name: str
    benchmark_name: str
    path_count: int
    win_rate_vs_benchmark_liquidation: float
    median_paired_wealth_delta: Decimal
    p10_paired_wealth_delta: Decimal
    p25_paired_wealth_delta: Decimal
    p75_paired_wealth_delta: Decimal
    median_after_tax_cagr: float
    median_tracking_error_bps: float
    p90_tracking_error_bps: float
    median_closed_lots_per_year: float
    dataset_version: str = "v1"
    survivorship_biased: bool = False
    validation_status: ValidationStatus = "validated"
    path_mode: PathMode = "historical"
    p90_paired_wealth_delta: Decimal = ZERO
    median_strategy_liquidation_wealth: Decimal = ZERO
    median_benchmark_liquidation_wealth: Decimal = ZERO
    benchmark_median_after_tax_cagr: float = 0.0
    median_annual_turnover: float = 0.0
    median_harvested_losses: Decimal = ZERO
    median_disallowed_wash_losses: Decimal = ZERO


@dataclass(frozen=True)
class SweepRow:
    """Single parameter grid point result in a sweep."""

    sweep_id: str
    run_id: str
    params: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    in_sample_summary: DistributionSummary | None = None
    out_of_sample_summary: DistributionSummary | None = None
    plateau_score: float | None = None
