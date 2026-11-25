"""Base strategy contract and daily market state passed by the simulation engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from idx_engine.ledger.lots import LotLedger
from idx_engine.ledger.wash_sale import WashSaleResolver
from idx_engine.models import ZERO
from idx_engine.strategy.sizing import FRAC_QTY_QUANT


@dataclass(frozen=True)
class DayMarketState:
    """Point-in-time market state on a single simulation trading day."""

    day_idx: int
    current_date: date
    prices: Mapping[str, Decimal]
    dividends: Mapping[str, Decimal]
    index_weights: Mapping[str, Decimal]
    sectors: Mapping[str, str]
    etf_ticker: str
    etf_price: Decimal
    etf_dividend: Decimal


class PortfolioStrategy(Protocol):
    """Strategy interface driven by the explicit-clock daily simulation engine."""

    @property
    def name(self) -> str: ...

    def on_day(
        self,
        state: DayMarketState,
        ledger: LotLedger,
        resolver: WashSaleResolver,
    ) -> None: ...

    def raise_cash(
        self,
        trade_date: date,
        amount_needed: Decimal,
        prices: Mapping[str, Decimal],
        ledger: LotLedger,
    ) -> None: ...


def liquidate_pro_rata_for_cash(
    trade_date: date,
    amount_needed: Decimal,
    prices: Mapping[str, Decimal],
    ledger: LotLedger,
    allow_fractional: bool,
    reason: str = "tax_settlement",
    resolver: WashSaleResolver | None = None,
) -> None:
    """Raise up to `amount_needed` in `ledger.cash` by selling open lots with the lowest
    realized gain ratio (`(px - unit_cost) / px`), preferring wash-clean lots first so
    routine fee or tax cash-raising avoids unnecessary 30-day wash sales.
    """
    shortfall = amount_needed - ledger.cash
    if shortfall <= ZERO:
        return

    open_lots = [
        lot
        for lot in ledger.open_lots()
        if lot.quantity > ZERO and prices.get(lot.ticker, ZERO) > ZERO
    ]
    if not open_lots:
        return

    def _lot_would_wash(lot_ticker: str, lot_id: str, is_loss: bool) -> bool:
        if not is_loss or resolver is None:
            return False
        root = ledger.root_lot_id(lot_id)
        for grp_t in resolver.group_members(lot_ticker):
            for other in ledger.open_lots(grp_t):
                if ledger.root_lot_id(other.lot_id) == root:
                    continue
                if ledger.is_lot_wash_matched(other.lot_id):
                    continue
                buy_dt = other.original_acquired_date or other.acquired_date
                if abs((trade_date - buy_dt).days) <= resolver.window_days:
                    return True
        return False

    scored_lots: list[tuple[int, Decimal, date, str, str]] = []
    for lot in open_lots:
        px = prices[lot.ticker]
        gain_ratio = (px - lot.unit_cost) / px
        is_loss = px < lot.unit_cost
        wash_penalty = 1 if _lot_would_wash(lot.ticker, lot.lot_id, is_loss) else 0
        scored_lots.append((wash_penalty, gain_ratio, lot.acquired_date, lot.lot_id, lot.ticker))

    # Wash-clean lots first (0 before 1), then lowest gain ratio (largest loss / smallest gain)
    scored_lots.sort()

    for _, _, _, lot_id, ticker in scored_lots:
        shortfall = amount_needed - ledger.cash
        if shortfall <= ZERO:
            break
        live_lot = ledger.get_lot(lot_id)
        if live_lot is None or live_lot.quantity <= ZERO:
            continue
        px = prices.get(ticker, ZERO)
        if px <= ZERO:
            continue
        if allow_fractional:
            need_qty = (shortfall / px).quantize(FRAC_QTY_QUANT, rounding=ROUND_DOWN)
            if need_qty * px < shortfall:
                need_qty = min(live_lot.quantity, need_qty + FRAC_QTY_QUANT)
            sell_qty = min(live_lot.quantity, need_qty)
        else:
            whole_need = int((shortfall // px) + (1 if shortfall % px > ZERO else 0))
            sell_qty = min(live_lot.quantity, Decimal(max(1, whole_need)))

        if sell_qty > ZERO:
            ledger.sell(
                trade_date=trade_date,
                ticker=ticker,
                quantity=sell_qty,
                price=px,
                policy="specific_lot",
                specific_lot_id=lot_id,
                reason=reason,
            )
