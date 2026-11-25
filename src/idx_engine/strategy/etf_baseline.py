"""Paired low-cost S&P 500 ETF baseline strategy (`etf_baseline`)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import ROUND_DOWN, Decimal

from idx_engine.config import StrategyConfig
from idx_engine.ledger.lots import LotLedger
from idx_engine.ledger.wash_sale import WashSaleResolver
from idx_engine.models import ZERO
from idx_engine.strategy.base import DayMarketState, liquidate_pro_rata_for_cash
from idx_engine.strategy.sizing import FRAC_QTY_QUANT

TRADING_DAYS_PER_YEAR = Decimal("252")
BPS_DIVISOR = Decimal("10000")


class EtfBaselineStrategy:
    """Buys and holds the low-cost S&P 500 ETF under identical cash flows, share-rounding
    constraints (`allow_fractional`), NAV expense-ratio drag, and tax rules.

    Note: An ETF's fund expense ratio (`etf_expense_ratio_bps`) is deducted internally from
    the ETF's daily NAV in `simulate_paired_path`—never by selling investor shares or
    triggering 1099-B disposals / wash-sale lockouts.
    """

    def __init__(
        self,
        cfg: StrategyConfig,
        benchmark_ticker: str = "VOO",
        apply_explicit_fee: bool = False,
        resolver: WashSaleResolver | None = None,
    ) -> None:
        self.cfg = cfg
        self.benchmark_ticker = benchmark_ticker
        self.apply_explicit_fee = apply_explicit_fee
        self._resolver = resolver

    @property
    def name(self) -> str:
        return f"etf_baseline_{self.benchmark_ticker}"

    def on_day(
        self,
        state: DayMarketState,
        ledger: LotLedger,
        resolver: WashSaleResolver,
    ) -> None:
        self._resolver = resolver
        px = state.etf_price
        if px <= ZERO:
            return

        # Invest available cash (initial capital, periodic contributions, dividends, tax refunds)
        if resolver.is_locked_out(self.benchmark_ticker, state.current_date):
            return

        if self.cfg.allow_fractional:
            if ledger.cash >= Decimal("1"):
                qty = (ledger.cash / px).quantize(FRAC_QTY_QUANT, rounding=ROUND_DOWN)
                if qty > ZERO:
                    reason = "initial_alloc" if state.day_idx == 0 else "cash_invest"
                    ledger.buy(
                        trade_date=state.current_date,
                        ticker=self.benchmark_ticker,
                        sector="Index ETF",
                        quantity=qty,
                        price=px,
                        reason=reason,
                    )
        else:
            whole_qty = Decimal(int(ledger.cash // px))
            if whole_qty >= Decimal("1"):
                reason = "initial_alloc" if state.day_idx == 0 else "cash_invest"
                ledger.buy(
                    trade_date=state.current_date,
                    ticker=self.benchmark_ticker,
                    sector="Index ETF",
                    quantity=whole_qty,
                    price=px,
                    reason=reason,
                )

    def raise_cash(
        self,
        trade_date: date,
        amount_needed: Decimal,
        prices: Mapping[str, Decimal],
        ledger: LotLedger,
    ) -> None:
        liquidate_pro_rata_for_cash(
            trade_date=trade_date,
            amount_needed=amount_needed,
            prices=prices,
            ledger=ledger,
            allow_fractional=self.cfg.allow_fractional,
            reason="tax_settlement",
            resolver=self._resolver,
        )
