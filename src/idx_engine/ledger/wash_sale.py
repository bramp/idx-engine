"""30-day IRS §1091 wash-sale detection, basis rollover, and holding-period tacking."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from idx_engine.ledger.lots import LotLedger, _quantize_basis, _quantize_qty
from idx_engine.models import (
    ZERO,
    ClosedLotRecord,
    CorporateAction,
    TaxLot,
    WashSaleAdjustment,
)

WASH_WINDOW_DAYS = 30


class WashSaleResolver:
    """Resolves 61-calendar-day (`[-30d, +30d]`) wash sales chronologically on a LotLedger.

    Implements IRS Treas. Reg. §1.1091-1 ordering:
    - Losses are matched in chronological order of disposition (earliest loss first).
    - Replacement acquisitions within `[sale_date - 30d, sale_date + 30d]` are matched
      in chronological order of actual acquisition date (earliest purchase first).
    - Each replacement share can disallow a loss at most once (`single-use`).
    - Partial replacement quantities split the replacement lot so basis rollover and
      §1223(3) holding-period tacking apply strictly to the matched shares.
    - Remaining shares from the exact same original acquisition lot do not trigger a wash
      sale when part of that single purchase is sold at a loss.
    """

    def __init__(
        self,
        substantially_identical_groups: Sequence[Sequence[str]] = (),
        window_days: int = WASH_WINDOW_DAYS,
    ) -> None:
        self.window_days = window_days
        self._canonical_map: dict[str, str] = {}
        for group in substantially_identical_groups:
            if not group:
                continue
            canonical = sorted(group)[0]
            for ticker in group:
                self._canonical_map[ticker] = canonical
        self._last_loss_sale_by_group: dict[str, date] = {}
        self._splits: list[tuple[str, date, Decimal]] = []

    def canonical_group(self, ticker: str) -> str:
        return self._canonical_map.get(ticker, ticker)

    def are_substantially_identical(self, ticker_a: str, ticker_b: str) -> bool:
        return self.canonical_group(ticker_a) == self.canonical_group(ticker_b)

    def group_members(self, ticker: str) -> list[str]:
        group = self.canonical_group(ticker)
        members = [sym for sym, grp in self._canonical_map.items() if grp == group]
        if ticker not in members:
            members.append(ticker)
        return members

    def attach(self, ledger: LotLedger) -> None:
        """Bind this resolver as the live BUY/SELL hook on `ledger`."""
        ledger.set_wash_sale_hook(self.on_ledger_event)

    def is_locked_out(self, ticker: str, check_date: date) -> bool:
        """Return True if buying `ticker` on `check_date` would fall within 30 calendar days
        after a prior loss sale of `ticker` (or a substantially identical security).
        """
        last_sale = self._last_loss_sale_by_group.get(self.canonical_group(ticker))
        if last_sale is None:
            return False
        delta = (check_date - last_sale).days
        return 0 <= delta <= self.window_days

    def record_ticker_rename(self, old_ticker: str, new_ticker: str) -> None:
        """Link old and new ticker symbols into the same substantially-identical group."""
        group = self.canonical_group(old_ticker)
        self._canonical_map[old_ticker] = group
        self._canonical_map[new_ticker] = group

    def _split_factor_between(self, ticker: str, from_date: date, to_date: date) -> Decimal:
        """Return the cumulative split multiplier mapping a share count as-of `from_date`
        into its equivalent share count as-of `to_date` (`>= from_date`).
        """
        if to_date <= from_date or not self._splits:
            return Decimal("1")
        factor = Decimal("1")
        for sp_ticker, eff_date, sp_factor in self._splits:
            if (
                self.are_substantially_identical(sp_ticker, ticker)
                and from_date < eff_date <= to_date
            ):
                factor *= sp_factor
        return factor

    def on_ledger_event(
        self,
        ledger: LotLedger,
        event_type: str,
        payload: list[ClosedLotRecord] | TaxLot | CorporateAction,
    ) -> None:
        if event_type == "SELL":
            assert isinstance(payload, list)
            for closed in payload:
                if closed.raw_realized_gain_loss < ZERO:
                    group = self.canonical_group(closed.ticker)
                    prev = self._last_loss_sale_by_group.get(group)
                    if prev is None or closed.sold_date > prev:
                        self._last_loss_sale_by_group[group] = closed.sold_date
                    self._match_loss_against_open_lots(ledger, closed.close_id)
        elif event_type == "BUY":
            assert isinstance(payload, TaxLot)
            self._match_new_lot_against_prior_losses(ledger, payload.lot_id)
        elif event_type == "SPLIT":
            assert isinstance(payload, CorporateAction)
            factor = payload.ratio_numerator / payload.ratio_denominator
            self._splits.append((payload.ticker, payload.effective_date, factor))
        elif event_type == "TICKER_CHANGE":
            assert isinstance(payload, CorporateAction)
            if payload.target_ticker:
                self.record_ticker_rename(payload.ticker, payload.target_ticker)

    def _matched_qty_for_closed(self, ledger: LotLedger, close_id: str) -> Decimal:
        return sum(
            (
                adj.matched_quantity
                for adj in ledger.wash_sale_adjustments
                if adj.closed_lot_id == close_id
            ),
            ZERO,
        )

    def _actual_buy_date(self, lot: TaxLot) -> date:
        return lot.original_acquired_date or lot.acquired_date

    def _is_within_window(self, sold_date: date, buy_date: date) -> bool:
        return abs((buy_date - sold_date).days) <= self.window_days

    def _apply_match(
        self,
        ledger: LotLedger,
        closed_idx: int,
        repl_lot_id: str,
        match_closed_qty: Decimal,
        match_repl_qty: Decimal,
    ) -> TaxLot | None:
        """Match `match_closed_qty` (in `closed.sold_date` share units) against
        `match_repl_qty` (in current `repl_lot` share units).
        Returns any split-off unmatched remainder lot, or None.
        """
        closed = ledger.closed_lots[closed_idx]
        already_matched = self._matched_qty_for_closed(ledger, closed.close_id)
        total_loss = -closed.raw_realized_gain_loss

        matched_lot, rem_lot = ledger.split_lot_for_wash_match(repl_lot_id, match_repl_qty)
        actual_repl_date = self._actual_buy_date(matched_lot)

        if already_matched + match_closed_qty >= closed.quantity:
            disallowed_piece = total_loss - closed.disallowed_wash_loss
        else:
            disallowed_piece = _quantize_basis(
                total_loss * (match_closed_qty / closed.quantity)
            )

        tacked_days = max(0, closed.holding_days)
        tacked_acquired_date = actual_repl_date - timedelta(days=tacked_days)

        updated_repl_lot = replace(
            matched_lot,
            acquired_date=tacked_acquired_date,
            original_acquired_date=actual_repl_date,
            cost_basis_total=matched_lot.cost_basis_total + disallowed_piece,
            wash_sale_disallowed=matched_lot.wash_sale_disallowed + disallowed_piece,
        )
        ledger.update_lot(updated_repl_lot)
        ledger.mark_lot_wash_matched(updated_repl_lot.lot_id)

        new_disallowed = closed.disallowed_wash_loss + disallowed_piece
        updated_closed = replace(
            closed,
            disallowed_wash_loss=new_disallowed,
            adjusted_realized_gain_loss=closed.raw_realized_gain_loss + new_disallowed,
            replacement_lot_ids=(*closed.replacement_lot_ids, updated_repl_lot.lot_id),
        )
        ledger.closed_lots[closed_idx] = updated_closed

        adj = WashSaleAdjustment(
            adjustment_id=ledger.next_wash_adjustment_id(),
            closed_lot_id=closed.close_id,
            loss_ticker=closed.ticker,
            replacement_ticker=updated_repl_lot.ticker,
            sold_date=closed.sold_date,
            replacement_date=actual_repl_date,
            replacement_lot_id=updated_repl_lot.lot_id,
            matched_quantity=match_closed_qty,
            disallowed_loss=disallowed_piece,
            tacked_holding_days=tacked_days,
        )
        ledger.wash_sale_adjustments.append(adj)
        return rem_lot

    def _match_loss_against_open_lots(self, ledger: LotLedger, close_id: str) -> None:
        closed_idx = next(
            (idx for idx, item in enumerate(ledger.closed_lots) if item.close_id == close_id),
            None,
        )
        if closed_idx is None:
            return

        closed = ledger.closed_lots[closed_idx]
        if closed.raw_realized_gain_loss >= ZERO:
            return

        sold_root_id = ledger.root_lot_id(closed.source_lot_id)
        unmatched_loss_qty = closed.quantity - self._matched_qty_for_closed(ledger, close_id)
        if unmatched_loss_qty <= ZERO:
            return

        # Eligible open lots acquired within [sold_date - 30d, sold_date + 30d]
        candidates: list[TaxLot] = []
        for grp_ticker in self.group_members(closed.ticker):
            for lot in ledger.open_lots(grp_ticker):
                if (
                    ledger.root_lot_id(lot.lot_id) != sold_root_id
                    and not ledger.is_lot_wash_matched(lot.lot_id)
                    and self._is_within_window(closed.sold_date, self._actual_buy_date(lot))
                ):
                    candidates.append(lot)
        candidates.sort(key=lambda lot: (self._actual_buy_date(lot), lot.lot_id))

        for candidate in candidates:
            if unmatched_loss_qty <= ZERO:
                break
            live_lot = ledger.get_lot(candidate.lot_id)
            if live_lot is None or ledger.is_lot_wash_matched(live_lot.lot_id):
                continue
            # Both closed.sold_date and open live_lot are currently valued at closed.sold_date units
            match_qty = min(unmatched_loss_qty, live_lot.quantity)
            if match_qty <= ZERO:
                continue
            self._apply_match(ledger, closed_idx, live_lot.lot_id, match_qty, match_qty)
            unmatched_loss_qty -= match_qty

    def _match_new_lot_against_prior_losses(self, ledger: LotLedger, new_lot_id: str) -> None:
        current_lot_id: str | None = new_lot_id
        while current_lot_id is not None:
            lot = ledger.get_lot(current_lot_id)
            if lot is None or ledger.is_lot_wash_matched(lot.lot_id) or lot.quantity <= ZERO:
                break

            buy_date = self._actual_buy_date(lot)
            lot_root = ledger.root_lot_id(lot.lot_id)

            # Find earliest prior loss sale within [buy_date - 30d, buy_date + 30d] with unmatched qty.
            # Scan backwards from recent sales first to skip ancient years in O(1), then pick earliest.
            matched_closed_idx: int | None = None
            unmatched_loss_qty = ZERO
            min_sold_date = buy_date - timedelta(days=self.window_days)
            for idx in range(len(ledger.closed_lots) - 1, -1, -1):
                closed = ledger.closed_lots[idx]
                if closed.sold_date < min_sold_date:
                    break
                if closed.raw_realized_gain_loss >= ZERO:
                    continue
                if not self.are_substantially_identical(closed.ticker, lot.ticker):
                    continue
                if ledger.root_lot_id(closed.source_lot_id) == lot_root:
                    continue
                if not self._is_within_window(closed.sold_date, buy_date):
                    continue
                rem_loss_qty = closed.quantity - self._matched_qty_for_closed(
                    ledger, closed.close_id
                )
                if rem_loss_qty > ZERO:
                    matched_closed_idx = idx
                    unmatched_loss_qty = rem_loss_qty

            if matched_closed_idx is None:
                break

            closed_match = ledger.closed_lots[matched_closed_idx]
            split_mult = self._split_factor_between(
                closed_match.ticker, closed_match.sold_date, buy_date
            )
            unmatched_in_buy_units = _quantize_qty(unmatched_loss_qty * split_mult)
            if unmatched_in_buy_units <= ZERO:
                break

            if lot.quantity >= unmatched_in_buy_units:
                match_repl_qty = unmatched_in_buy_units
                match_closed_qty = unmatched_loss_qty
            else:
                match_repl_qty = lot.quantity
                match_closed_qty = min(
                    unmatched_loss_qty,
                    _quantize_qty(match_repl_qty / split_mult),
                )
            if match_repl_qty <= ZERO or match_closed_qty <= ZERO:
                break

            rem_lot = self._apply_match(
                ledger, matched_closed_idx, lot.lot_id, match_closed_qty, match_repl_qty
            )
            current_lot_id = rem_lot.lot_id if rem_lot is not None else None


def is_in_wash_window(
    ticker: str,
    check_date: date,
    recent_loss_sales: Mapping[str, date],
    window_days: int = WASH_WINDOW_DAYS,
) -> bool:
    """Pure helper checking whether `check_date` is within `window_days` after a loss sale."""
    last_sale = recent_loss_sales.get(ticker)
    if last_sale is None:
        return False
    delta = (check_date - last_sale).days
    return 0 <= delta <= window_days
