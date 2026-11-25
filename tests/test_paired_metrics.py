"""Tests for paired simulation engine, deterministic replay, TLH alpha, and bias guardrails."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from idx_engine.config import (
    CashFlowConfig,
    DatasetRefConfig,
    PathSpecConfig,
    SimulationRunConfig,
    StrategyConfig,
    TaxConfig,
)
from idx_engine.fetch import DataStore, generate_synthetic_sp500_dataset
from idx_engine.models import PathOutcome
from idx_engine.sim import (
    build_paired_comparison,
    compute_annualized_tracking_error_bps,
    execute_and_persist_run,
)


def test_tracking_error_needs_two_active_returns() -> None:
    assert compute_annualized_tracking_error_bps([0.0, 0.1], [0.0, 0.0]) == 0.0
    assert (
        compute_annualized_tracking_error_bps([0.0, 0.1, 0.2], [0.0, 0.0, 0.0])
        > 0.0
    )


def test_unfunded_withdrawal_records_only_cash_actually_withdrawn(tmp_path: Path) -> None:
    store = DataStore(data_dir=tmp_path / "data")
    generate_synthetic_sp500_dataset(
        store=store,
        dataset_id="withdrawal_ds",
        start_date=date(2020, 1, 2),
        trading_days=2,
        max_constituents=4,
        seed=15,
    )
    dataset = store.load_dataset("withdrawal_ds")
    etf_prices = dataset.etf_df.get_column("close").to_list()

    cfg = SimulationRunConfig(
        dataset=DatasetRefConfig(dataset_id="withdrawal_ds", version="v1"),
        path=PathSpecConfig(mode="historical", horizon_days=2),
        strategy=StrategyConfig(
            strategy_type="etf_baseline",
            allow_fractional=True,
            etf_expense_ratio_bps=Decimal("0"),
        ),
        cashflow=CashFlowConfig(
            initial_capital=Decimal("10000"),
            periodic_contribution=Decimal("-1000000"),
            contribution_frequency_days=1,
        ),
    )

    result = execute_and_persist_run(store, cfg)
    expected_withdrawal = Decimal("10000") * Decimal(str(etf_prices[1])) / Decimal(
        str(etf_prices[0])
    )
    assert result.comparisons[0].direct_outcome.net_contributions == pytest.approx(
        -expected_withdrawal, abs=Decimal("0.01")
    )
    assert result.comparisons[0].benchmark_outcome.net_contributions == pytest.approx(
        -expected_withdrawal, abs=Decimal("0.01")
    )


def test_unaligned_path_comparison_is_rejected() -> None:
    dummy_a = PathOutcome(
        path_id="p_1",
        strategy_name="direct",
        ending_market_value=Decimal("100"),
        ending_cash=Decimal("0"),
        after_tax_realized_wealth=Decimal("100"),
        after_tax_liquidation_wealth=Decimal("95"),
        after_tax_cagr=0.05,
        annualized_tracking_error_bps=10.0,
        annual_turnover=0.1,
        harvested_gross_losses=Decimal("0"),
        disallowed_wash_sale_losses=Decimal("0"),
        closed_lot_count=0,
    )
    dummy_b = PathOutcome(
        path_id="p_2",
        strategy_name="etf",
        ending_market_value=Decimal("100"),
        ending_cash=Decimal("0"),
        after_tax_realized_wealth=Decimal("100"),
        after_tax_liquidation_wealth=Decimal("95"),
        after_tax_cagr=0.05,
        annualized_tracking_error_bps=0.0,
        annual_turnover=0.0,
        harvested_gross_losses=Decimal("0"),
        disallowed_wash_sale_losses=Decimal("0"),
        closed_lot_count=0,
    )
    with pytest.raises(ValueError, match="Unaligned comparison forbidden"):
        build_paired_comparison(dummy_a, dummy_b)


def test_full_basket_tracks_index_tightly_and_tlh_with_external_gains_adds_liquidation_value(
    tmp_path: Path,
) -> None:
    store = DataStore(data_dir=tmp_path / "data")
    generate_synthetic_sp500_dataset(
        store=store,
        dataset_id="paired_syn",
        start_date=date(2020, 1, 2),
        trading_days=520,
        max_constituents=20,
        seed=2024,
    )

    # 1. Zero-fee, no-TLH cap-weighted buy-and-hold closely tracks 0-fee gross index ETF
    no_tlh_cfg = SimulationRunConfig(
        dataset=DatasetRefConfig(dataset_id="paired_syn", version="v1"),
        path=PathSpecConfig(mode="historical", horizon_days=520),
        strategy=StrategyConfig(
            name="cap_hold",
            allow_fractional=True,
            tlh_enabled=False,
            rebalance_mode="none",
            direct_expense_ratio_bps=Decimal("0"),
            etf_expense_ratio_bps=Decimal("0"),
        ),
        tax=TaxConfig(
            external_st_gains_annual=Decimal("15000"),
            external_lt_gains_annual=Decimal("25000"),
        ),
        cashflow=CashFlowConfig(initial_capital=Decimal("300000")),
    )
    res_no_tlh = execute_and_persist_run(store, no_tlh_cfg)
    assert res_no_tlh.summary.median_tracking_error_bps < 25.0
    assert res_no_tlh.summary.validation_status == "single_path"
    # Buy-and-hold ETF baseline with 3 bps NAV drag must NOT sell ETF shares daily to pay fees
    etf_fee_cfg = no_tlh_cfg.model_copy(
        update={
            "strategy": no_tlh_cfg.strategy.model_copy(
                update={"etf_expense_ratio_bps": Decimal("3")}
            ),
            "cashflow": CashFlowConfig(
                initial_capital=Decimal("300000"),
                periodic_contribution=Decimal("2500"),
                contribution_frequency_days=21,
            ),
        }
    )
    res_etf_nav = execute_and_persist_run(store, etf_fee_cfg)
    bench_lots = store.query_closed_lots(
        res_etf_nav.run_id, strategy_role="benchmark", limit=1000
    )
    assert all(lot["reason"] == "tax_settlement" for lot in bench_lots)
    assert len(bench_lots) <= 4
    assert res_etf_nav.comparisons[0].benchmark_outcome.wash_sale_count == 0

    # Deterministic replay produces identical run_id and median_paired_wealth_delta
    res_replay = execute_and_persist_run(store, no_tlh_cfg)
    assert res_replay.run_id == res_no_tlh.run_id
    assert (
        res_replay.summary.median_paired_wealth_delta
        == res_no_tlh.summary.median_paired_wealth_delta
    )

    # 2. Multi-path block bootstrap WITH TLH and external capital gains
    tlh_boot_cfg = SimulationRunConfig(
        dataset=DatasetRefConfig(dataset_id="paired_syn", version="v1"),
        path=PathSpecConfig(
            mode="block_bootstrap",
            horizon_days=504,
            block_size_days=21,
            num_paths=10,
            seed=77,
        ),
        strategy=StrategyConfig(
            name="direct_tlh",
            allow_fractional=True,
            tlh_enabled=True,
            tlh_loss_pct_threshold=Decimal("0.04"),
            tlh_min_loss_dollars=Decimal("150"),
            tlh_check_frequency_days=5,
            substitute_mode="sector_peer",
            rebalance_mode="quarterly",
            direct_expense_ratio_bps=Decimal("0"),
            etf_expense_ratio_bps=Decimal("3"),
        ),
        tax=TaxConfig(
            external_st_gains_annual=Decimal("15000"),
            external_lt_gains_annual=Decimal("25000"),
        ),
        cashflow=CashFlowConfig(initial_capital=Decimal("300000")),
    )
    res_tlh = execute_and_persist_run(store, tlh_boot_cfg)
    assert res_tlh.summary.validation_status == "validated"
    assert res_tlh.summary.path_count == 10
    assert res_tlh.summary.median_harvested_losses > Decimal("0")
    assert (
        res_tlh.summary.median_disallowed_wash_losses
        < res_tlh.summary.median_harvested_losses * Decimal("0.02")
    )
    assert res_tlh.summary.median_paired_wealth_delta > Decimal("0")
    assert res_tlh.summary.win_rate_vs_benchmark_liquidation >= 0.6

    # Stored run & equity percentile bands can be retrieved from DuckDB
    stored = store.get_run(res_tlh.run_id)
    assert len(stored["equity_bands"]) == 504
    assert len(stored["paths"]) == 10


def test_survivorship_biased_dataset_blocks_validated_status(tmp_path: Path) -> None:
    store = DataStore(data_dir=tmp_path / "data")
    generate_synthetic_sp500_dataset(
        store=store,
        dataset_id="pit_src",
        trading_days=120,
        max_constituents=8,
    )
    ds = store.load_dataset("pit_src")
    store.save_dataset(
        dataset_id="biased_ds",
        source="current_constituents",
        prices_df=ds.prices_df,
        returns_df=ds.returns_df,
        dividends_df=ds.dividends_df,
        weights_df=ds.weights_df,
        etf_df=ds.etf_df,
        sectors=ds.sectors,
        survivorship_biased=True,
    )

    cfg = SimulationRunConfig(
        dataset=DatasetRefConfig(dataset_id="biased_ds"),
        path=PathSpecConfig(mode="block_bootstrap", horizon_days=100, num_paths=4),
    )
    res = execute_and_persist_run(store, cfg)
    assert res.summary.survivorship_biased is True
    assert res.summary.validation_status == "exploratory_survivorship_biased"
