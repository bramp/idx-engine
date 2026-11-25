"""Deterministic tax-lot ledger supporting HIFO/FIFO/specific_lot and corporate actions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from idx_engine.models import (
    ZERO,
    ClosedLotRecord,
    CorporateAction,
    DividendReceipt,
    LotPolicy,
    TaxLot,
    TaxTerm,
    TradeFill,
    WashSaleAdjustment,
)

BASIS_QUANT = Decimal("0.00000001")
QTY_QUANT = Decimal("0.00000001")

WashSaleHook = Callable[
    ["LotLedger", str, list[ClosedLotRecord] | TaxLot | CorporateAction], None
]


def _quantize_basis(val: Decimal) -> Decimal:
    return val.quantize(BASIS_QUANT, rounding=ROUND_HALF_UP)


def _quantize_qty(val: Decimal) -> Decimal:
    return val.quantize(QTY_QUANT, rounding=ROUND_HALF_UP)


def holding_term(acquired_date: date, sold_date: date) -> tuple[int, TaxTerm]:
    """Compute holding days and classify sales after the one-year anniversary as LT."""
    days = (sold_date - acquired_date).days
    if acquired_date.year == date.max.year:
        anniversary = date.max
    else:
        try:
            anniversary = acquired_date.replace(year=acquired_date.year + 1)
        except ValueError:
            anniversary = acquired_date.replace(year=acquired_date.year + 1, day=28)
    term: TaxTerm = "LT" if sold_date > anniversary else "ST"
    return days, term


class LotLedger:
    """Auditable, deterministic tax-lot and cash ledger for a single portfolio."""

    def __init__(
        self,
        initial_cash: Decimal = ZERO,
        default_policy: LotPolicy = "HIFO",
        prefix: str = "p",
        wash_sale_hook: WashSaleHook | None = None,
    ) -> None:
        if initial_cash < ZERO:
            raise ValueError("initial_cash must be non-negative")
        self.cash: Decimal = initial_cash
        self.default_policy: LotPolicy = default_policy
        self.prefix: str = prefix
        self._wash_sale_hook: WashSaleHook | None = wash_sale_hook

        self._lots: dict[str, TaxLot] = {}
        self._ticker_lot_ids: dict[str, set[str]] = {}
        self._positions_cache: dict[str, Decimal] | None = None
        self._lot_root_ids: dict[str, str] = {}
        self._wash_matched_lot_ids: set[str] = set()
        self.fills: list[TradeFill] = []
        self.closed_lots: list[ClosedLotRecord] = []
        self.dividends: list[DividendReceipt] = []
        self.wash_sale_adjustments: list[WashSaleAdjustment] = []

        self._lot_seq = 0
        self._fill_seq = 0
        self._close_seq = 0
        self._div_seq = 0
        self._ws_seq = 0

    def next_wash_adjustment_id(self) -> str:
        self._ws_seq += 1
        return f"{self.prefix}_ws_{self._ws_seq:06d}"

    def _next_lot_id(self) -> str:
        self._lot_seq += 1
        return f"{self.prefix}_lot_{self._lot_seq:06d}"

    def _next_fill_id(self) -> str:
        self._fill_seq += 1
        return f"{self.prefix}_fill_{self._fill_seq:06d}"

    def _next_close_id(self) -> str:
        self._close_seq += 1
        return f"{self.prefix}_close_{self._close_seq:06d}"

    def _next_div_id(self) -> str:
        self._div_seq += 1
        return f"{self.prefix}_div_{self._div_seq:06d}"

    def set_wash_sale_hook(self, hook: WashSaleHook | None) -> None:
        self._wash_sale_hook = hook

    def is_lot_wash_matched(self, lot_id: str) -> bool:
        """True if all shares in `lot_id` have already served as wash-sale replacements."""
        return lot_id in self._wash_matched_lot_ids

    def mark_lot_wash_matched(self, lot_id: str) -> None:
        self._wash_matched_lot_ids.add(lot_id)

    def get_lot(self, lot_id: str) -> TaxLot | None:
        return self._lots.get(lot_id)

    def root_lot_id(self, lot_id: str) -> str:
        """Return the original acquisition lot_id from which `lot_id` originated."""
        return self._lot_root_ids.get(lot_id, lot_id)

    def _index_put(self, lot: TaxLot) -> None:
        old = self._lots.get(lot.lot_id)
        if old is not None and old.ticker != lot.ticker:
            s_old = self._ticker_lot_ids.get(old.ticker)
            if s_old is not None:
                s_old.discard(lot.lot_id)
        self._lots[lot.lot_id] = lot
        self._ticker_lot_ids.setdefault(lot.ticker, set()).add(lot.lot_id)
        self._positions_cache = None

    def _index_pop(self, lot_id: str) -> TaxLot | None:
        old = self._lots.pop(lot_id, None)
        if old is not None:
            s_old = self._ticker_lot_ids.get(old.ticker)
            if s_old is not None:
                s_old.discard(lot_id)
                if not s_old:
                    self._ticker_lot_ids.pop(old.ticker, None)
            self._positions_cache = None
        return old

    def update_lot(self, lot: TaxLot) -> None:
        if lot.quantity <= ZERO:
            self._index_pop(lot.lot_id)
        else:
            self._index_put(lot)
            self._lot_root_ids.setdefault(lot.lot_id, lot.lot_id)

    def split_lot_for_wash_match(self, lot_id: str, matched_qty: Decimal) -> tuple[TaxLot, TaxLot | None]:
        """Split an open lot into `(matched_lot, remainder_lot)` so partial wash-sale basis
        and holding-period tacking apply strictly to the matched replacement shares.
        """
        lot = self._lots[lot_id]
        if matched_qty <= ZERO or matched_qty > lot.quantity:
            raise ValueError(f"Invalid matched_qty {matched_qty} for lot {lot_id} ({lot.quantity})")
        if matched_qty == lot.quantity:
            return lot, None

        ratio = matched_qty / lot.quantity
        matched_basis = _quantize_basis(lot.cost_basis_total * ratio)
        matched_ws = _quantize_basis(lot.wash_sale_disallowed * ratio)

        rem_qty = lot.quantity - matched_qty
        rem_basis = lot.cost_basis_total - matched_basis
        rem_ws = lot.wash_sale_disallowed - matched_ws

        matched_lot = replace(
            lot,
            quantity=matched_qty,
            cost_basis_total=matched_basis,
            wash_sale_disallowed=matched_ws,
        )
        rem_lot = TaxLot(
            lot_id=self._next_lot_id(),
            ticker=lot.ticker,
            sector=lot.sector,
            acquired_date=lot.acquired_date,
            original_acquired_date=lot.original_acquired_date,
            quantity=rem_qty,
            cost_basis_total=rem_basis,
            wash_sale_disallowed=rem_ws,
        )
        self._index_put(matched_lot)
        self._index_put(rem_lot)
        self._lot_root_ids[rem_lot.lot_id] = self.root_lot_id(lot.lot_id)
        return matched_lot, rem_lot

    def open_lots(self, ticker: str | None = None) -> list[TaxLot]:
        if ticker is not None:
            ids = self._ticker_lot_ids.get(ticker)
            if not ids:
                return []
            lots = [self._lots[lid] for lid in ids]
            lots.sort(key=lambda item: (item.acquired_date, item.lot_id))
            return lots
        lots = list(self._lots.values())
        lots.sort(key=lambda item: (item.ticker, item.acquired_date, item.lot_id))
        return lots

    def position_quantity(self, ticker: str) -> Decimal:
        return self.all_positions().get(ticker, ZERO)

    def position_cost_basis(self, ticker: str) -> Decimal:
        ids = self._ticker_lot_ids.get(ticker)
        if not ids:
            return ZERO
        return sum((self._lots[lid].cost_basis_total for lid in ids), ZERO)

    def all_positions(self) -> dict[str, Decimal]:
        if self._positions_cache is not None:
            return self._positions_cache
        positions: dict[str, Decimal] = {}
        for lot in self._lots.values():
            positions[lot.ticker] = positions.get(lot.ticker, ZERO) + lot.quantity
        self._positions_cache = positions
        return positions

    def market_value(self, prices: Mapping[str, Decimal]) -> Decimal:
        total = ZERO
        for ticker, qty in self.all_positions().items():
            price = prices.get(ticker, ZERO)
            total += qty * price
        return total

    def deposit_cash(self, amount: Decimal) -> None:
        self.cash += amount

    def buy(
        self,
        trade_date: date,
        ticker: str,
        sector: str,
        quantity: Decimal,
        price: Decimal,
        reason: str = "buy",
        deduct_cash: bool = True,
    ) -> tuple[TaxLot, TradeFill]:
        """Acquire shares as a new TaxLot and optionally deduct cost from cash."""
        if quantity <= ZERO:
            raise ValueError(f"Buy quantity must be positive, got {quantity}")
        if price <= ZERO:
            raise ValueError(f"Buy price must be positive, got {price}")

        gross = _quantize_basis(quantity * price)
        if deduct_cash:
            if gross > self.cash + Decimal("0.0001"):
                raise ValueError(
                    f"Insufficient cash to buy {quantity} {ticker} @ {price}: "
                    f"cost={gross}, cash={self.cash}"
                )
            self.cash = max(ZERO, self.cash - gross)

        lot = TaxLot(
            lot_id=self._next_lot_id(),
            ticker=ticker,
            sector=sector,
            acquired_date=trade_date,
            original_acquired_date=trade_date,
            quantity=quantity,
            cost_basis_total=gross,
            wash_sale_disallowed=ZERO,
        )
        self._index_put(lot)
        self._lot_root_ids[lot.lot_id] = lot.lot_id

        fill = TradeFill(
            fill_id=self._next_fill_id(),
            trade_date=trade_date,
            ticker=ticker,
            sector=sector,
            side="BUY",
            quantity=quantity,
            price=price,
            gross_amount=gross,
            reason=reason,
        )
        self.fills.append(fill)

        if self._wash_sale_hook is not None:
            self._wash_sale_hook(self, "BUY", lot)

        return self._lots.get(lot.lot_id, lot), fill

    def _select_lots_for_sale(
        self,
        ticker: str,
        policy: LotPolicy,
        specific_lot_id: str | None = None,
        specific_lot_ids: list[str] | None = None,
    ) -> list[TaxLot]:
        t_ids = self._ticker_lot_ids.get(ticker)
        if not t_ids:
            return []
        if policy == "specific_lot":
            ids = specific_lot_ids or ([specific_lot_id] if specific_lot_id else [])
            if not ids:
                raise ValueError("specific_lot_id(s) required when policy='specific_lot'")
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate lot IDs are not allowed for specific-lot sales")
            selected: list[TaxLot] = []
            for lid in ids:
                lot = self._lots.get(lid)
                if lot is None or lot.ticker != ticker:
                    raise ValueError(f"Lot '{lid}' not found for ticker '{ticker}'")
                selected.append(lot)
            return selected
        candidates = [self._lots[lid] for lid in t_ids]
        if policy == "HIFO":
            return sorted(
                candidates,
                key=lambda item: (-item.unit_cost, item.acquired_date, item.lot_id),
            )
        if policy == "FIFO":
            return sorted(
                candidates,
                key=lambda item: (item.acquired_date, item.lot_id),
            )
        raise ValueError(f"Unsupported lot policy: {policy}")

    def sell(
        self,
        trade_date: date,
        ticker: str,
        quantity: Decimal,
        price: Decimal,
        policy: LotPolicy | None = None,
        specific_lot_id: str | None = None,
        specific_lot_ids: list[str] | None = None,
        reason: str = "sell",
        credit_cash: bool = True,
    ) -> tuple[list[ClosedLotRecord], TradeFill]:
        """Dispose of `quantity` shares of `ticker` according to `policy`."""
        if quantity <= ZERO:
            raise ValueError(f"Sell quantity must be positive, got {quantity}")
        if price < ZERO:
            raise ValueError(f"Sell price must be non-negative, got {price}")

        effective_policy = policy or self.default_policy
        ordered_lots = self._select_lots_for_sale(
            ticker, effective_policy, specific_lot_id, specific_lot_ids
        )
        available = sum((lot.quantity for lot in ordered_lots), ZERO)
        if quantity > available:
            raise ValueError(
                f"Cannot sell {quantity} shares of {ticker}: only {available} available"
            )

        remaining_to_sell = quantity
        sector = ordered_lots[0].sector
        newly_closed: list[ClosedLotRecord] = []

        for lot in ordered_lots:
            if remaining_to_sell <= ZERO:
                break
            sell_qty = min(lot.quantity, remaining_to_sell)
            if sell_qty == lot.quantity:
                sold_basis = lot.cost_basis_total
                self._index_pop(lot.lot_id)
                self._wash_matched_lot_ids.discard(lot.lot_id)
            else:
                ratio = sell_qty / lot.quantity
                sold_basis = _quantize_basis(lot.cost_basis_total * ratio)
                sold_ws = _quantize_basis(lot.wash_sale_disallowed * ratio)
                updated_lot = replace(
                    lot,
                    quantity=lot.quantity - sell_qty,
                    cost_basis_total=lot.cost_basis_total - sold_basis,
                    wash_sale_disallowed=lot.wash_sale_disallowed - sold_ws,
                )
                self._index_put(updated_lot)

            proceeds = _quantize_basis(sell_qty * price)
            raw_pnl = proceeds - sold_basis
            days, term = holding_term(lot.acquired_date, trade_date)

            closed = ClosedLotRecord(
                close_id=self._next_close_id(),
                source_lot_id=lot.lot_id,
                ticker=ticker,
                sector=lot.sector,
                acquired_date=lot.acquired_date,
                sold_date=trade_date,
                holding_days=days,
                term=term,
                quantity=sell_qty,
                proceeds_total=proceeds,
                cost_basis_total=sold_basis,
                raw_realized_gain_loss=raw_pnl,
                disallowed_wash_loss=ZERO,
                adjusted_realized_gain_loss=raw_pnl,
                replacement_lot_ids=(),
                reason=reason,
            )
            self.closed_lots.append(closed)
            newly_closed.append(closed)
            remaining_to_sell -= sell_qty

        total_proceeds = sum((c.proceeds_total for c in newly_closed), ZERO)
        self._positions_cache = None
        if credit_cash:
            self.cash += total_proceeds

        fill = TradeFill(
            fill_id=self._next_fill_id(),
            trade_date=trade_date,
            ticker=ticker,
            sector=sector,
            side="SELL",
            quantity=quantity,
            price=price,
            gross_amount=total_proceeds,
            reason=reason,
            specific_lot_id=specific_lot_id,
        )
        self.fills.append(fill)

        if self._wash_sale_hook is not None:
            self._wash_sale_hook(self, "SELL", newly_closed)

        # Refresh references in case the wash-sale hook updated the records
        refreshed = [
            next(c for c in self.closed_lots if c.close_id == item.close_id)
            for item in newly_closed
        ]
        return refreshed, fill

    def receive_dividend(
        self,
        pay_date: date,
        ticker: str,
        cash_per_share: Decimal,
        qualified_fraction: Decimal = Decimal("1"),
    ) -> DividendReceipt | None:
        """Credit cash dividend across all open lots of `ticker` on `pay_date`."""
        if cash_per_share <= ZERO:
            return None
        shares = self.position_quantity(ticker)
        if shares <= ZERO:
            return None

        gross = _quantize_basis(shares * cash_per_share)
        qualified = _quantize_basis(gross * qualified_fraction)
        ordinary = gross - qualified
        self.cash += gross

        receipt = DividendReceipt(
            receipt_id=self._next_div_id(),
            pay_date=pay_date,
            ticker=ticker,
            shares_held=shares,
            gross_amount=gross,
            qualified_amount=qualified,
            ordinary_amount=ordinary,
        )
        self.dividends.append(receipt)
        return receipt

    def apply_corporate_action(self, action: CorporateAction) -> DividendReceipt | None:
        """Apply split, dividend, ticker_change, or spin_off to open lots."""
        if action.action_type == "dividend":
            return self.receive_dividend(
                pay_date=action.effective_date,
                ticker=action.ticker,
                cash_per_share=action.cash_amount_per_share,
                qualified_fraction=action.qualified_fraction,
            )

        if action.action_type == "split":
            if action.ratio_numerator <= ZERO or action.ratio_denominator <= ZERO:
                raise ValueError(f"Invalid split ratio on {action.action_id}")
            factor = action.ratio_numerator / action.ratio_denominator
            for lot in self.open_lots(action.ticker):
                new_qty = _quantize_qty(lot.quantity * factor)
                self._index_put(replace(lot, quantity=new_qty))
            if self._wash_sale_hook is not None:
                self._wash_sale_hook(self, "SPLIT", action)
            return None

        if action.action_type == "ticker_change":
            if not action.target_ticker:
                raise ValueError(f"ticker_change requires target_ticker on {action.action_id}")
            for lot in self.open_lots(action.ticker):
                self._index_put(
                    replace(
                        lot,
                        ticker=action.target_ticker,
                        sector=action.target_sector or lot.sector,
                    )
                )
            if self._wash_sale_hook is not None:
                self._wash_sale_hook(self, "TICKER_CHANGE", action)
            return None

        matching_lots = self.open_lots(action.ticker)
        if not matching_lots:
            return None
        self._positions_cache = None

        if action.action_type == "spin_off":
            if not action.target_ticker:
                raise ValueError(f"spin_off requires target_ticker on {action.action_id}")
            if not (ZERO <= action.basis_allocation_fraction <= Decimal("1")):
                raise ValueError(
                    f"spin_off basis_allocation_fraction must be in [0, 1] on {action.action_id}"
                )
            ratio = action.ratio_numerator / action.ratio_denominator
            for lot in matching_lots:
                spin_qty = _quantize_qty(lot.quantity * ratio)
                if spin_qty <= ZERO:
                    continue
                spin_basis = _quantize_basis(
                    lot.cost_basis_total * action.basis_allocation_fraction
                )
                spin_ws = _quantize_basis(
                    lot.wash_sale_disallowed * action.basis_allocation_fraction
                )
                parent_updated = replace(
                    lot,
                    cost_basis_total=lot.cost_basis_total - spin_basis,
                    wash_sale_disallowed=lot.wash_sale_disallowed - spin_ws,
                )
                self._index_put(parent_updated)

                child_lot = TaxLot(
                    lot_id=self._next_lot_id(),
                    ticker=action.target_ticker,
                    sector=action.target_sector or lot.sector,
                    acquired_date=lot.acquired_date,
                    original_acquired_date=lot.original_acquired_date,
                    quantity=spin_qty,
                    cost_basis_total=spin_basis,
                    wash_sale_disallowed=spin_ws,
                )
                self._index_put(child_lot)
                self._lot_root_ids[child_lot.lot_id] = self.root_lot_id(lot.lot_id)
            return None

        raise ValueError(f"Unsupported corporate action type: {action.action_type}")
