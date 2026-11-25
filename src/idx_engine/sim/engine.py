"""Deterministic single-path and multi-path paired simulation engine."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from idx_engine.config import SimulationRunConfig
from idx_engine.fetch.store import DataStore, MarketDataset
from idx_engine.ledger.lots import LotLedger
from idx_engine.ledger.taxes import TaxAccountant
from idx_engine.ledger.wash_sale import WashSaleResolver
from idx_engine.models import (
    ZERO,
    ClosedLotRecord,
    CorporateAction,
    DailySnapshot,
    DistributionSummary,
    PairedPathComparison,
    PathOutcome,
    SimulationPath,
    WashSaleAdjustment,
)
from idx_engine.sim.metrics import (
    build_paired_comparison,
    compute_annualized_tracking_error_bps,
    compute_cagr,
    summarize_paired_distribution,
)
from idx_engine.sim.paths import generate_paths
from idx_engine.strategy.base import DayMarketState, PortfolioStrategy
from idx_engine.strategy.direct_index import DirectIndexStrategy
from idx_engine.strategy.etf_baseline import EtfBaselineStrategy

CENTS = Decimal("0.01")
ProgressCallback = Callable[[int, int], None]


def _dec_from_float(val: float) -> Decimal:
    return Decimal(f"{val:.8f}")


def _round_cents(val: Decimal) -> Decimal:
    return val.quantize(CENTS, rounding=ROUND_HALF_UP)


class _LazyDecimalRowMap(Mapping[str, Decimal]):
    """Read-only lazy Mapping[str, Decimal] over a 1D numpy float64 vector."""

    __slots__ = ("_cache", "_index_map", "_keys", "_row")

    def __init__(
        self,
        keys: list[str],
        index_map: dict[str, int],
        row: np.ndarray,
    ) -> None:
        self._keys = keys
        self._index_map = index_map
        self._row = row
        self._cache: dict[str, Decimal] = {}

    def __getitem__(self, key: str) -> Decimal:
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        idx = self._index_map[key]
        val = _dec_from_float(float(self._row[idx]))
        self._cache[key] = val
        return val

    def get(self, key: str, default: Decimal = ZERO) -> Decimal:  # type: ignore[override]
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        idx = self._index_map.get(key)
        if idx is None:
            return default
        val = _dec_from_float(float(self._row[idx]))
        self._cache[key] = val
        return val

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


@dataclass
class SinglePathRunArtifacts:
    """Detailed artifacts produced for one paired SimulationPath."""

    comparison: PairedPathComparison
    direct_snapshots: list[DailySnapshot]
    benchmark_snapshots: list[DailySnapshot]
    direct_closed_lots: list[ClosedLotRecord]
    benchmark_closed_lots: list[ClosedLotRecord]
    direct_wash_sales: list[WashSaleAdjustment]


@dataclass
class SimulationRunResult:
    """Complete result bundle of a multi-path paired simulation run."""

    run_id: str
    summary: DistributionSummary
    comparisons: list[PairedPathComparison]
    direct_snapshots_by_path: dict[str, list[DailySnapshot]]
    bench_snapshots_by_path: dict[str, list[DailySnapshot]]
    closed_lots_by_path: dict[str, tuple[list[ClosedLotRecord], list[ClosedLotRecord]]]
    wash_sales_by_path: dict[str, list[WashSaleAdjustment]]


def _apply_tax_settlement_cashflow(
    trade_date: date,
    net_tax_bill: Decimal,
    settlement_mode: str,
    strategy: PortfolioStrategy,
    prices: Mapping[str, Decimal],
    ledger: LotLedger,
) -> None:
    if settlement_mode != "reinvest_and_pay_from_portfolio":
        return
    if net_tax_bill < ZERO:
        # Portfolio losses generated net tax savings -> reinvest tax alpha into portfolio cash
        ledger.deposit_cash(-net_tax_bill)
    elif net_tax_bill > ZERO:
        strategy.raise_cash(trade_date, net_tax_bill, prices, ledger)
        paid = min(ledger.cash, net_tax_bill)
        ledger.cash -= paid


def _year_fraction(trading_days_in_year: int) -> Decimal:
    if trading_days_in_year >= 240:
        return Decimal("1")
    return Decimal(str(min(1.0, round(trading_days_in_year / 252.0, 6))))


def simulate_paired_path(
    path: SimulationPath,
    cfg: SimulationRunConfig,
    benchmark_ticker: str = "VOO",
    benchmark_embeds_fee: bool = False,
) -> SinglePathRunArtifacts:
    """Execute `DirectIndexStrategy` and `EtfBaselineStrategy` side-by-side on `path`
    under identical dates, starting capital, cash-flow schedule, and tax rules.
    """
    tickers = [c for c in path.prices_df.columns if c != "date"]
    tok_idx = {t: i for i, t in enumerate(tickers)}
    n_days = len(path.trading_dates)

    price_mat = path.prices_df.select(tickers).to_numpy()
    div_mat = path.dividends_df.select(tickers).to_numpy()
    weight_mat = path.weights_df.select(tickers).to_numpy()
    raw_etf_close_arr = path.etf_df.get_column("close").to_numpy()
    raw_etf_div_arr = path.etf_df.get_column("dividend").to_numpy()

    # Apply ETF fund expense ratio internally to the daily NAV & dividend curve when
    # benchmarking against a gross index series (never by selling investor ETF shares).
    if not benchmark_embeds_fee and cfg.strategy.etf_expense_ratio_bps > ZERO and n_days > 1:
        daily_er = float(cfg.strategy.etf_expense_ratio_bps) / 10000.0 / 252.0
        decay = (1.0 - daily_er) ** np.arange(n_days, dtype=np.float64)
        etf_close_arr = raw_etf_close_arr * decay
        etf_div_arr = raw_etf_div_arr * decay
    else:
        etf_close_arr = raw_etf_close_arr
        etf_div_arr = raw_etf_div_arr

    ca_by_date: dict[date, list[CorporateAction]] = {}
    for ca in path.corporate_actions:
        ca_by_date.setdefault(ca.effective_date, []).append(ca)

    days_per_year: dict[int, int] = {}
    for dt in path.trading_dates:
        days_per_year[dt.year] = days_per_year.get(dt.year, 0) + 1
    start_year = path.trading_dates[0].year

    # Initialize paired portfolios
    direct_ledger = LotLedger(
        initial_cash=cfg.cashflow.initial_capital,
        default_policy=cfg.strategy.lot_policy,
        prefix=f"{path.path_id}_dir",
    )
    direct_resolver = WashSaleResolver(
        substantially_identical_groups=cfg.strategy.substantially_identical_groups
    )
    direct_resolver.attach(direct_ledger)
    direct_taxes = TaxAccountant(cfg.tax)
    direct_strategy: PortfolioStrategy = (
        EtfBaselineStrategy(
            cfg.strategy,
            benchmark_ticker=benchmark_ticker,
            resolver=direct_resolver,
        )
        if cfg.strategy.strategy_type == "etf_baseline"
        else DirectIndexStrategy(cfg.strategy)
    )

    bench_ledger = LotLedger(
        initial_cash=cfg.cashflow.initial_capital,
        default_policy=cfg.strategy.lot_policy,
        prefix=f"{path.path_id}_etf",
    )
    bench_resolver = WashSaleResolver()
    bench_resolver.attach(bench_ledger)
    bench_taxes = TaxAccountant(cfg.tax)
    bench_strategy = EtfBaselineStrategy(
        cfg.strategy,
        benchmark_ticker=benchmark_ticker,
        resolver=bench_resolver,
    )

    direct_snapshots: list[DailySnapshot] = []
    bench_snapshots: list[DailySnapshot] = []
    direct_daily_rets: list[float] = []
    bench_daily_rets: list[float] = []
    direct_cash_ratios: list[float] = []
    bench_cash_ratios: list[float] = []

    direct_net_contributions = ZERO
    bench_net_contributions = ZERO
    prev_direct_eq = float(cfg.cashflow.initial_capital)
    prev_bench_eq = float(cfg.cashflow.initial_capital)

    for day_idx, current_date in enumerate(path.trading_dates):
        prices_map = _LazyDecimalRowMap(tickers, tok_idx, price_mat[day_idx])
        weights_map = _LazyDecimalRowMap(tickers, tok_idx, weight_mat[day_idx])
        divs_map = _LazyDecimalRowMap(tickers, tok_idx, div_mat[day_idx])
        etf_px = _dec_from_float(float(etf_close_arr[day_idx]))
        etf_div = _dec_from_float(float(etf_div_arr[day_idx]))
        bench_price_map = {benchmark_ticker: etf_px}

        # 1. Corporate actions effective on `current_date` (LotLedger hook notifies WashSaleResolver)
        for ca in ca_by_date.get(current_date, ()):
            direct_ledger.apply_corporate_action(ca)

        # 2. Periodic cash contributions / withdrawals (aligned across both strategies)
        direct_strat_prices = (
            {benchmark_ticker: etf_px}
            if cfg.strategy.strategy_type == "etf_baseline"
            else prices_map
        )
        direct_ext_cf_today = ZERO
        bench_ext_cf_today = ZERO
        if (
            day_idx > 0
            and cfg.cashflow.periodic_contribution != ZERO
            and day_idx % cfg.cashflow.contribution_frequency_days == 0
        ):
            cf = cfg.cashflow.periodic_contribution
            if cf > ZERO:
                direct_ledger.deposit_cash(cf)
                bench_ledger.deposit_cash(cf)
                direct_net_contributions += cf
                bench_net_contributions += cf
                direct_ext_cf_today = cf
                bench_ext_cf_today = cf
            else:
                need = -cf
                direct_strategy.raise_cash(current_date, need, direct_strat_prices, direct_ledger)
                bench_strategy.raise_cash(current_date, need, bench_price_map, bench_ledger)
                withdrawn_d = min(direct_ledger.cash, need)
                withdrawn_b = min(bench_ledger.cash, need)
                direct_ledger.cash -= withdrawn_d
                bench_ledger.cash -= withdrawn_b
                direct_net_contributions -= withdrawn_d
                bench_net_contributions -= withdrawn_b
                direct_ext_cf_today = -withdrawn_d
                bench_ext_cf_today = -withdrawn_b

        # 3. Credit cash dividends on open positions
        if cfg.strategy.strategy_type == "etf_baseline":
            if etf_div > ZERO:
                direct_ledger.receive_dividend(
                    pay_date=current_date,
                    ticker=benchmark_ticker,
                    cash_per_share=etf_div,
                    qualified_fraction=cfg.tax.qualified_dividend_fraction_default,
                )
        else:
            div_row = div_mat[day_idx]
            if np.any(div_row > 0.0):
                for held_ticker in list(direct_ledger.all_positions().keys()):
                    col_i = tok_idx.get(held_ticker)
                    if col_i is not None and div_row[col_i] > 0.0:
                        direct_ledger.receive_dividend(
                            pay_date=current_date,
                            ticker=held_ticker,
                            cash_per_share=_dec_from_float(float(div_row[col_i])),
                            qualified_fraction=cfg.tax.qualified_dividend_fraction_default,
                        )
        if etf_div > ZERO:
            bench_ledger.receive_dividend(
                pay_date=current_date,
                ticker=benchmark_ticker,
                cash_per_share=etf_div,
                qualified_fraction=cfg.tax.qualified_dividend_fraction_default,
            )

        # 4. Annual US Tax Day settlement (first trading day on/after April 15 for prior year)
        prior_year = current_date.year - 1
        direct_tax_cf = ZERO
        bench_tax_cf = ZERO
        if (
            prior_year >= start_year
            and prior_year not in direct_taxes.settled_years
            and (current_date.month > 4 or (current_date.month == 4 and current_date.day >= 15))
        ):
            y_frac = _year_fraction(days_per_year.get(prior_year, 252))
            d_sum = direct_taxes.settle_tax_year(
                prior_year, direct_ledger.closed_lots, direct_ledger.dividends, y_frac
            )
            b_sum = bench_taxes.settle_tax_year(
                prior_year, bench_ledger.closed_lots, bench_ledger.dividends, y_frac
            )
            if cfg.tax.tax_settlement_mode == "reinvest_and_pay_from_portfolio":
                _apply_tax_settlement_cashflow(
                    current_date,
                    d_sum.net_portfolio_tax_bill,
                    cfg.tax.tax_settlement_mode,
                    direct_strategy,
                    direct_strat_prices,
                    direct_ledger,
                )
                _apply_tax_settlement_cashflow(
                    current_date,
                    b_sum.net_portfolio_tax_bill,
                    cfg.tax.tax_settlement_mode,
                    bench_strategy,
                    bench_price_map,
                    bench_ledger,
                )
                direct_tax_cf = -d_sum.net_portfolio_tax_bill
                bench_tax_cf = -b_sum.net_portfolio_tax_bill

        # 5. Execute daily strategy logic
        state = DayMarketState(
            day_idx=day_idx,
            current_date=current_date,
            prices=prices_map,
            dividends=divs_map,
            index_weights=weights_map,
            sectors=path.sectors,
            etf_ticker=benchmark_ticker,
            etf_price=etf_px,
            etf_dividend=etf_div,
        )
        direct_strategy.on_day(state, direct_ledger, direct_resolver)
        bench_strategy.on_day(state, bench_ledger, bench_resolver)

        # 6. End-of-day valuation & cash-flow-adjusted daily return for tracking error
        d_mv = _round_cents(direct_ledger.market_value(direct_strat_prices))
        d_cash = _round_cents(direct_ledger.cash)
        d_eq = d_mv + d_cash
        d_after_tax = (
            d_eq
            if cfg.tax.tax_settlement_mode == "reinvest_and_pay_from_portfolio"
            else d_eq - direct_taxes.cumulative_net_portfolio_tax_paid
        )

        b_mv = _round_cents(bench_ledger.market_value(bench_price_map))
        b_cash = _round_cents(bench_ledger.cash)
        b_eq = b_mv + b_cash
        b_after_tax = (
            b_eq
            if cfg.tax.tax_settlement_mode == "reinvest_and_pay_from_portfolio"
            else b_eq - bench_taxes.cumulative_net_portfolio_tax_paid
        )

        if day_idx == 0:
            direct_daily_rets.append(0.0)
            bench_daily_rets.append(0.0)
        else:
            d_cf = float(direct_ext_cf_today + direct_tax_cf)
            b_cf = float(bench_ext_cf_today + bench_tax_cf)
            d_ret = ((float(d_eq) - d_cf) / max(prev_direct_eq, 1e-8)) - 1.0
            b_ret = ((float(b_eq) - b_cf) / max(prev_bench_eq, 1e-8)) - 1.0
            direct_daily_rets.append(d_ret)
            bench_daily_rets.append(b_ret)

        prev_direct_eq = float(d_eq)
        prev_bench_eq = float(b_eq)
        if d_eq > ZERO:
            direct_cash_ratios.append(float(d_cash / d_eq))
        if b_eq > ZERO:
            bench_cash_ratios.append(float(b_cash / b_eq))

        direct_snapshots.append(
            DailySnapshot(
                date=current_date,
                market_value=d_mv,
                cash=d_cash,
                total_equity=d_eq,
                cumulative_net_tax_paid=direct_taxes.cumulative_net_portfolio_tax_paid,
                after_tax_wealth_pre_liquidation=d_after_tax,
            )
        )
        bench_snapshots.append(
            DailySnapshot(
                date=current_date,
                market_value=b_mv,
                cash=b_cash,
                total_equity=b_eq,
                cumulative_net_tax_paid=bench_taxes.cumulative_net_portfolio_tax_paid,
                after_tax_wealth_pre_liquidation=b_after_tax,
            )
        )

    # 7. Final-horizon settlement of any unsettled tax years + hypothetical liquidation tax
    final_date = path.trading_dates[-1]
    final_prices = _LazyDecimalRowMap(tickers, tok_idx, price_mat[-1])
    final_bench_prices = {benchmark_ticker: _dec_from_float(float(etf_close_arr[-1]))}
    final_direct_prices = (
        final_bench_prices if cfg.strategy.strategy_type == "etf_baseline" else final_prices
    )
    final_year_frac = _year_fraction(days_per_year.get(final_date.year, 252))

    # Settle any remaining unpaid DirectIndexStrategy advisory fee accrual at horizon end
    if isinstance(direct_strategy, DirectIndexStrategy) and direct_strategy._accrued_fee > ZERO:
        direct_strategy.raise_cash(
            final_date, direct_strategy._accrued_fee, final_direct_prices, direct_ledger
        )
        paid_fee = min(direct_ledger.cash, direct_strategy._accrued_fee)
        direct_ledger.cash -= paid_fee
        direct_strategy._accrued_fee -= paid_fee

    for yr in sorted(days_per_year.keys()):
        if yr not in direct_taxes.settled_years:
            y_frac = _year_fraction(days_per_year[yr])
            d_sum = direct_taxes.settle_tax_year(
                yr, direct_ledger.closed_lots, direct_ledger.dividends, y_frac
            )
            b_sum = bench_taxes.settle_tax_year(
                yr, bench_ledger.closed_lots, bench_ledger.dividends, y_frac
            )
            _apply_tax_settlement_cashflow(
                final_date,
                d_sum.net_portfolio_tax_bill,
                cfg.tax.tax_settlement_mode,
                direct_strategy,
                final_direct_prices,
                direct_ledger,
            )
            _apply_tax_settlement_cashflow(
                final_date,
                b_sum.net_portfolio_tax_bill,
                cfg.tax.tax_settlement_mode,
                bench_strategy,
                final_bench_prices,
                bench_ledger,
            )

    d_end_mv = _round_cents(direct_ledger.market_value(final_direct_prices))
    d_end_cash = _round_cents(direct_ledger.cash)
    d_end_eq = d_end_mv + d_end_cash
    d_realized_wealth = (
        d_end_eq
        if cfg.tax.tax_settlement_mode == "reinvest_and_pay_from_portfolio"
        else d_end_eq - direct_taxes.cumulative_net_portfolio_tax_paid
    )
    d_liq = direct_taxes.compute_hypothetical_liquidation(
        direct_ledger, final_date, final_direct_prices, year_fraction=final_year_frac
    )
    d_liq_wealth = (
        d_realized_wealth - d_liq.liquidation_tax_due
        if cfg.tax.liquidate_at_end
        else d_realized_wealth
    )

    b_end_mv = _round_cents(bench_ledger.market_value(final_bench_prices))
    b_end_cash = _round_cents(bench_ledger.cash)
    b_end_eq = b_end_mv + b_end_cash
    b_realized_wealth = (
        b_end_eq
        if cfg.tax.tax_settlement_mode == "reinvest_and_pay_from_portfolio"
        else b_end_eq - bench_taxes.cumulative_net_portfolio_tax_paid
    )
    b_liq = bench_taxes.compute_hypothetical_liquidation(
        bench_ledger, final_date, final_bench_prices, year_fraction=final_year_frac
    )
    b_liq_wealth = (
        b_realized_wealth - b_liq.liquidation_tax_due
        if cfg.tax.liquidate_at_end
        else b_realized_wealth
    )

    # Refresh final snapshot with post-final-year-tax state
    direct_snapshots[-1] = DailySnapshot(
        date=final_date,
        market_value=d_end_mv,
        cash=d_end_cash,
        total_equity=d_end_eq,
        cumulative_net_tax_paid=direct_taxes.cumulative_net_portfolio_tax_paid,
        after_tax_wealth_pre_liquidation=d_realized_wealth,
    )
    bench_snapshots[-1] = DailySnapshot(
        date=final_date,
        market_value=b_end_mv,
        cash=b_end_cash,
        total_equity=b_end_eq,
        cumulative_net_tax_paid=bench_taxes.cumulative_net_portfolio_tax_paid,
        after_tax_wealth_pre_liquidation=b_realized_wealth,
    )

    te_bps = compute_annualized_tracking_error_bps(direct_daily_rets, bench_daily_rets)
    years = max(n_days / 252.0, 1.0 / 252.0)

    def _annual_turnover(ledger: LotLedger, snaps: list[DailySnapshot]) -> float:
        sells = sum(
            (float(f.gross_amount) for f in ledger.fills if f.side == "SELL"),
            0.0,
        )
        avg_eq = float(np.mean([float(s.total_equity) for s in snaps])) if snaps else 1.0
        return round((sells / max(avg_eq, 1.0)) / years, 4)

    def _harvested_and_disallowed(ledger: LotLedger) -> tuple[Decimal, Decimal]:
        # Sum net deductible losses (`-adjusted_realized_gain_loss`) plus wash-disallowed losses
        # still carried in open replacement lots so intra-period wash basis rolled into a lot
        # that is later sold is never double-counted.
        deductible_losses = sum(
            (
                -c.adjusted_realized_gain_loss
                for c in ledger.closed_lots
                if c.adjusted_realized_gain_loss < ZERO
            ),
            ZERO,
        )
        still_deferred = sum((lot.wash_sale_disallowed for lot in ledger.open_lots()), ZERO)
        disallowed_total = sum((c.disallowed_wash_loss for c in ledger.closed_lots), ZERO)
        return deductible_losses + still_deferred, disallowed_total

    d_harv, d_disallowed = _harvested_and_disallowed(direct_ledger)
    b_harv, b_disallowed = _harvested_and_disallowed(bench_ledger)
    avg_cash_bps = (
        round(float(np.mean(direct_cash_ratios)) * 10000.0, 2) if direct_cash_ratios else 0.0
    )
    bench_avg_cash_bps = (
        round(float(np.mean(bench_cash_ratios)) * 10000.0, 2) if bench_cash_ratios else 0.0
    )

    direct_outcome = PathOutcome(
        path_id=path.path_id,
        strategy_name=direct_strategy.name,
        initial_capital=cfg.cashflow.initial_capital,
        net_contributions=direct_net_contributions,
        ending_market_value=d_end_mv,
        ending_cash=d_end_cash,
        cumulative_tax_paid=direct_taxes.cumulative_net_portfolio_tax_paid,
        liquidation_tax_due=d_liq.liquidation_tax_due,
        remaining_loss_carryforward=d_liq.total_remaining_carryforward,
        after_tax_realized_wealth=_round_cents(d_realized_wealth),
        after_tax_liquidation_wealth=_round_cents(d_liq_wealth),
        pre_tax_cagr=compute_cagr(
            cfg.cashflow.initial_capital, d_end_eq, direct_net_contributions, n_days
        ),
        after_tax_cagr=compute_cagr(
            cfg.cashflow.initial_capital, d_liq_wealth, direct_net_contributions, n_days
        ),
        annualized_tracking_error_bps=te_bps,
        annual_turnover=_annual_turnover(direct_ledger, direct_snapshots),
        harvested_gross_losses=_round_cents(d_harv),
        disallowed_wash_sale_losses=_round_cents(d_disallowed),
        closed_lot_count=len(direct_ledger.closed_lots),
        wash_sale_count=len(direct_ledger.wash_sale_adjustments),
        trade_count=len(direct_ledger.fills),
        avg_cash_drag_bps=avg_cash_bps,
    )

    bench_outcome = PathOutcome(
        path_id=path.path_id,
        strategy_name=bench_strategy.name,
        initial_capital=cfg.cashflow.initial_capital,
        net_contributions=bench_net_contributions,
        ending_market_value=b_end_mv,
        ending_cash=b_end_cash,
        cumulative_tax_paid=bench_taxes.cumulative_net_portfolio_tax_paid,
        liquidation_tax_due=b_liq.liquidation_tax_due,
        remaining_loss_carryforward=b_liq.total_remaining_carryforward,
        after_tax_realized_wealth=_round_cents(b_realized_wealth),
        after_tax_liquidation_wealth=_round_cents(b_liq_wealth),
        pre_tax_cagr=compute_cagr(
            cfg.cashflow.initial_capital, b_end_eq, bench_net_contributions, n_days
        ),
        after_tax_cagr=compute_cagr(
            cfg.cashflow.initial_capital, b_liq_wealth, bench_net_contributions, n_days
        ),
        annualized_tracking_error_bps=0.0,
        annual_turnover=_annual_turnover(bench_ledger, bench_snapshots),
        harvested_gross_losses=_round_cents(b_harv),
        disallowed_wash_sale_losses=_round_cents(b_disallowed),
        closed_lot_count=len(bench_ledger.closed_lots),
        wash_sale_count=len(bench_ledger.wash_sale_adjustments),
        trade_count=len(bench_ledger.fills),
        avg_cash_drag_bps=bench_avg_cash_bps,
    )

    comparison = build_paired_comparison(direct_outcome, bench_outcome)
    return SinglePathRunArtifacts(
        comparison=comparison,
        direct_snapshots=direct_snapshots,
        benchmark_snapshots=bench_snapshots,
        direct_closed_lots=direct_ledger.closed_lots,
        benchmark_closed_lots=bench_ledger.closed_lots,
        direct_wash_sales=direct_ledger.wash_sale_adjustments,
    )


def run_simulation_on_dataset(
    dataset: MarketDataset,
    cfg: SimulationRunConfig,
    progress_cb: ProgressCallback | None = None,
) -> SimulationRunResult:
    """Pure in-memory execution of all paths for `cfg` on `dataset` (worker-process safe)."""
    run_id = cfg.compute_run_id(dataset.metadata.version)
    paths = generate_paths(dataset, cfg.path.to_path_spec())
    benchmark_ticker = dataset.metadata.benchmark_ticker
    embeds_fee = dataset.metadata.source.startswith("yfinance")

    comparisons: list[PairedPathComparison] = []
    direct_snaps: dict[str, list[DailySnapshot]] = {}
    bench_snaps: dict[str, list[DailySnapshot]] = {}
    lots_by_path: dict[str, tuple[list[ClosedLotRecord], list[ClosedLotRecord]]] = {}
    ws_by_path: dict[str, list[WashSaleAdjustment]] = {}

    total = len(paths)
    for idx, p in enumerate(paths):
        art = simulate_paired_path(
            path=p,
            cfg=cfg,
            benchmark_ticker=benchmark_ticker,
            benchmark_embeds_fee=embeds_fee,
        )
        comparisons.append(art.comparison)
        if cfg.save_equity_curves:
            direct_snaps[p.path_id] = art.direct_snapshots
            bench_snaps[p.path_id] = art.benchmark_snapshots
        if cfg.save_lot_details:
            lots_by_path[p.path_id] = (art.direct_closed_lots, art.benchmark_closed_lots)
            ws_by_path[p.path_id] = art.direct_wash_sales
        if progress_cb is not None:
            progress_cb(idx + 1, total)

    horizon = len(paths[0].trading_dates)
    summary = summarize_paired_distribution(
        run_id=run_id,
        dataset_id=dataset.metadata.dataset_id,
        dataset_version=dataset.metadata.version,
        survivorship_biased=dataset.metadata.survivorship_biased,
        strategy_name=cfg.strategy.name,
        benchmark_name=f"etf_baseline_{benchmark_ticker}",
        path_mode=cfg.path.mode,
        comparisons=comparisons,
        horizon_days=horizon,
    )
    return SimulationRunResult(
        run_id=run_id,
        summary=summary,
        comparisons=comparisons,
        direct_snapshots_by_path=direct_snaps,
        bench_snapshots_by_path=bench_snaps,
        closed_lots_by_path=lots_by_path,
        wash_sales_by_path=ws_by_path,
    )


def execute_and_persist_run(
    store: DataStore,
    cfg: SimulationRunConfig,
    persist: bool = True,
    progress_cb: ProgressCallback | None = None,
) -> SimulationRunResult:
    """Load dataset from `store`, run paired simulation, and optionally persist to DuckDB."""
    dataset = store.load_dataset(
        dataset_id=cfg.dataset.dataset_id,
        version=cfg.dataset.version,
        start_date=cfg.dataset.start_date,
        end_date=cfg.dataset.end_date,
    )
    result = run_simulation_on_dataset(dataset, cfg, progress_cb=progress_cb)
    if persist:
        store.save_run_results(
            summary=result.summary,
            config_dict=cfg.model_dump(mode="json"),
            comparisons=result.comparisons,
            direct_snapshots_by_path=result.direct_snapshots_by_path or None,
            bench_snapshots_by_path=result.bench_snapshots_by_path or None,
            closed_lots_by_path=result.closed_lots_by_path or None,
            wash_sales_by_path=result.wash_sales_by_path or None,
        )
    return result
