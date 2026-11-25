"""Annual federal + state ST/LT capital gains, dividend, carryforward, and liquidation tax engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from idx_engine.config import TaxConfig
from idx_engine.ledger.lots import LotLedger, holding_term
from idx_engine.models import (
    ZERO,
    AnnualTaxSummary,
    ClosedLotRecord,
    DividendReceipt,
)

CENTS = Decimal("0.01")


def _round_cents(val: Decimal) -> Decimal:
    return val.quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class _ScheduleDResult:
    taxable_st: Decimal
    taxable_lt: Decimal
    ordinary_offset_used: Decimal
    st_carryforward_out: Decimal
    lt_carryforward_out: Decimal
    total_tax: Decimal


@dataclass(frozen=True)
class LiquidationTaxResult:
    """Hypothetical end-of-horizon full portfolio liquidation tax accounting."""

    unrealized_st_net: Decimal
    unrealized_lt_net: Decimal
    liquidation_tax_due: Decimal
    remaining_st_carryforward: Decimal
    remaining_lt_carryforward: Decimal
    terminal_carryforward_credit: Decimal

    @property
    def total_remaining_carryforward(self) -> Decimal:
        return self.remaining_st_carryforward + self.remaining_lt_carryforward


def _compute_schedule_d(
    st_gain_loss: Decimal,
    lt_gain_loss: Decimal,
    st_carryforward_in: Decimal,
    lt_carryforward_in: Decimal,
    qualified_dividends: Decimal,
    ordinary_dividends: Decimal,
    cfg: TaxConfig,
    allow_ordinary_offset: bool = True,
) -> _ScheduleDResult:
    """Apply IRS Schedule D short/long netting, $3k ordinary-income cap, and carryforwards."""
    net_st = st_gain_loss - st_carryforward_in
    net_lt = lt_gain_loss - lt_carryforward_in
    cap = cfg.ordinary_income_offset_cap if allow_ordinary_offset else ZERO

    taxable_st = ZERO
    taxable_lt = ZERO
    ord_offset = ZERO
    st_cf_out = ZERO
    lt_cf_out = ZERO

    if net_st >= ZERO and net_lt >= ZERO:
        taxable_st = net_st
        taxable_lt = net_lt
    elif net_st < ZERO and net_lt >= ZERO:
        combined = net_lt + net_st
        if combined >= ZERO:
            taxable_lt = combined
        else:
            excess_st_loss = -combined
            ord_offset = min(excess_st_loss, cap)
            st_cf_out = excess_st_loss - ord_offset
    elif net_st >= ZERO and net_lt < ZERO:
        combined = net_st + net_lt
        if combined >= ZERO:
            taxable_st = combined
        else:
            excess_lt_loss = -combined
            ord_offset = min(excess_lt_loss, cap)
            lt_cf_out = excess_lt_loss - ord_offset
    else:
        # Both net_st < 0 and net_lt < 0: ST losses consume the $3k ordinary deduction first
        st_loss = -net_st
        lt_loss = -net_lt
        st_ord = min(st_loss, cap)
        rem_cap = cap - st_ord
        lt_ord = min(lt_loss, rem_cap)
        ord_offset = st_ord + lt_ord
        st_cf_out = st_loss - st_ord
        lt_cf_out = lt_loss - lt_ord

    ordinary_offset_rate = min(Decimal("1"), cfg.fed_st_rate + cfg.state_st_rate)
    gross_tax = (
        taxable_st * cfg.combined_st_rate
        + taxable_lt * cfg.combined_lt_rate
        + qualified_dividends * cfg.combined_lt_rate
        + ordinary_dividends * cfg.combined_st_rate
        - ord_offset * ordinary_offset_rate
    )
    return _ScheduleDResult(
        taxable_st=taxable_st,
        taxable_lt=taxable_lt,
        ordinary_offset_used=ord_offset,
        st_carryforward_out=st_cf_out,
        lt_carryforward_out=lt_cf_out,
        total_tax=_round_cents(gross_tax),
    )


class TaxAccountant:
    """Tracks multi-year realized taxes, loss carryforwards, and end-of-horizon liquidation."""

    def __init__(self, cfg: TaxConfig) -> None:
        self.cfg = cfg
        self.st_carryforward: Decimal = ZERO
        self.lt_carryforward: Decimal = ZERO
        self._year_state: dict[
            int, tuple[Decimal, Decimal, Decimal, Decimal, Decimal]
        ] = {}
        self.settled_years: set[int] = set()
        self.annual_summaries: list[AnnualTaxSummary] = []
        self.cumulative_net_portfolio_tax_paid: Decimal = ZERO

    @property
    def total_loss_carryforward(self) -> Decimal:
        return self.st_carryforward + self.lt_carryforward

    def settle_tax_year(
        self,
        tax_year: int,
        closed_lots: Sequence[ClosedLotRecord],
        dividends: Sequence[DividendReceipt],
        year_fraction: Decimal = Decimal("1"),
    ) -> AnnualTaxSummary:
        """Compute annual Schedule D + dividend taxes for `tax_year` and update carryforwards.

        `net_portfolio_tax_bill` is positive when the portfolio owes net tax, and negative
        when harvested portfolio losses generate net tax savings relative to the investor's
        baseline external capital gains.
        """
        if tax_year in self.settled_years:
            raise ValueError(f"Tax year {tax_year} has already been settled")

        year_closed = [c for c in closed_lots if c.sold_date.year == tax_year]
        year_divs = [d for d in dividends if d.pay_date.year == tax_year]

        realized_st = sum(
            (c.adjusted_realized_gain_loss for c in year_closed if c.term == "ST"), ZERO
        )
        realized_lt = sum(
            (c.adjusted_realized_gain_loss for c in year_closed if c.term == "LT"), ZERO
        )
        qual_div = sum((d.qualified_amount for d in year_divs), ZERO)
        ord_div = sum((d.ordinary_amount for d in year_divs), ZERO)

        ext_st = _round_cents(self.cfg.external_st_gains_annual * year_fraction)
        ext_lt = _round_cents(self.cfg.external_lt_gains_annual * year_fraction)

        st_cf_in = self.st_carryforward
        lt_cf_in = self.lt_carryforward

        with_portfolio = _compute_schedule_d(
            st_gain_loss=realized_st + ext_st,
            lt_gain_loss=realized_lt + ext_lt,
            st_carryforward_in=st_cf_in,
            lt_carryforward_in=lt_cf_in,
            qualified_dividends=qual_div,
            ordinary_dividends=ord_div,
            cfg=self.cfg,
            allow_ordinary_offset=True,
        )
        external_only = _compute_schedule_d(
            st_gain_loss=ext_st,
            lt_gain_loss=ext_lt,
            st_carryforward_in=ZERO,
            lt_carryforward_in=ZERO,
            qualified_dividends=ZERO,
            ordinary_dividends=ZERO,
            cfg=self.cfg,
            allow_ordinary_offset=True,
        )

        net_portfolio_bill = with_portfolio.total_tax - external_only.total_tax

        self._year_state[tax_year] = (
            st_cf_in,
            lt_cf_in,
            year_fraction,
            external_only.total_tax,
            net_portfolio_bill,
        )
        self.st_carryforward = with_portfolio.st_carryforward_out
        self.lt_carryforward = with_portfolio.lt_carryforward_out
        self.settled_years.add(tax_year)
        self.cumulative_net_portfolio_tax_paid += net_portfolio_bill

        summary = AnnualTaxSummary(
            tax_year=tax_year,
            realized_st_net=_round_cents(realized_st),
            realized_lt_net=_round_cents(realized_lt),
            external_st_gains=ext_st,
            external_lt_gains=ext_lt,
            qualified_dividends=_round_cents(qual_div),
            ordinary_dividends=_round_cents(ord_div),
            ordinary_income_offset_used=_round_cents(with_portfolio.ordinary_offset_used),
            tax_liability_with_portfolio=with_portfolio.total_tax,
            tax_liability_external_only=external_only.total_tax,
            net_portfolio_tax_bill=net_portfolio_bill,
            st_loss_carryforward_out=_round_cents(self.st_carryforward),
            lt_loss_carryforward_out=_round_cents(self.lt_carryforward),
        )
        self.annual_summaries.append(summary)
        return summary

    def compute_hypothetical_liquidation(
        self,
        ledger: LotLedger,
        liquidation_date: date,
        prices: Mapping[str, Decimal],
        year_fraction: Decimal = Decimal("1"),
    ) -> LiquidationTaxResult:
        """Compute hypothetical end-of-horizon liquidation tax on all remaining open lots
        (plus any final-year disposals occurring after annual settlement) without mutating `ledger`.

        Evaluates `liquidation_date.year`'s complete Schedule D combining realized + unrealized
        + external ST/LT gains from the carryforwards entering `liquidation_date.year` and
        subtracts any net portfolio tax already settled for `liquidation_date.year`.
        """
        unrealized_st = ZERO
        unrealized_lt = ZERO

        for lot in ledger.open_lots():
            px = prices.get(lot.ticker, ZERO)
            proceeds = lot.quantity * px
            pnl = proceeds - lot.cost_basis_total
            _, term = holding_term(lot.acquired_date, liquidation_date)
            if term == "LT":
                unrealized_lt += pnl
            else:
                unrealized_st += pnl

        liq_year = liquidation_date.year
        year_closed = [c for c in ledger.closed_lots if c.sold_date.year == liq_year]
        year_divs = [d for d in ledger.dividends if d.pay_date.year == liq_year]

        realized_st = sum(
            (c.adjusted_realized_gain_loss for c in year_closed if c.term == "ST"), ZERO
        )
        realized_lt = sum(
            (c.adjusted_realized_gain_loss for c in year_closed if c.term == "LT"), ZERO
        )
        qual_div = sum((d.qualified_amount for d in year_divs), ZERO)
        ord_div = sum((d.ordinary_amount for d in year_divs), ZERO)

        if liq_year in self._year_state:
            st_cf_in, lt_cf_in, y_frac, ext_only_tax, already_settled_net_bill = self._year_state[
                liq_year
            ]
        else:
            st_cf_in = self.st_carryforward
            lt_cf_in = self.lt_carryforward
            y_frac = year_fraction
            ext_st_init = _round_cents(self.cfg.external_st_gains_annual * y_frac)
            ext_lt_init = _round_cents(self.cfg.external_lt_gains_annual * y_frac)
            ext_only_tax = _compute_schedule_d(
                st_gain_loss=ext_st_init,
                lt_gain_loss=ext_lt_init,
                st_carryforward_in=ZERO,
                lt_carryforward_in=ZERO,
                qualified_dividends=ZERO,
                ordinary_dividends=ZERO,
                cfg=self.cfg,
                allow_ordinary_offset=True,
            ).total_tax
            already_settled_net_bill = ZERO

        ext_st = _round_cents(self.cfg.external_st_gains_annual * y_frac)
        ext_lt = _round_cents(self.cfg.external_lt_gains_annual * y_frac)

        sched = _compute_schedule_d(
            st_gain_loss=realized_st + unrealized_st + ext_st,
            lt_gain_loss=realized_lt + unrealized_lt + ext_lt,
            st_carryforward_in=st_cf_in,
            lt_carryforward_in=lt_cf_in,
            qualified_dividends=qual_div,
            ordinary_dividends=ord_div,
            cfg=self.cfg,
            allow_ordinary_offset=True,
        )

        full_year_net_bill = sched.total_tax - ext_only_tax
        incremental_liq_tax = full_year_net_bill - already_settled_net_bill

        rem_cf = sched.st_carryforward_out + sched.lt_carryforward_out
        terminal_credit = _round_cents(rem_cf * self.cfg.carryforward_terminal_credit_rate)
        liq_tax_due = incremental_liq_tax - terminal_credit

        return LiquidationTaxResult(
            unrealized_st_net=_round_cents(unrealized_st),
            unrealized_lt_net=_round_cents(unrealized_lt),
            liquidation_tax_due=_round_cents(liq_tax_due),
            remaining_st_carryforward=_round_cents(sched.st_carryforward_out),
            remaining_lt_carryforward=_round_cents(sched.lt_carryforward_out),
            terminal_carryforward_credit=terminal_credit,
        )
