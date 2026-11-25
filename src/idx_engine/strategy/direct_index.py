"""Direct indexing strategy: full/stratified S&P 500, TLH, wash-sale lockouts & sector substitutes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import ROUND_DOWN, Decimal

from idx_engine.config import StrategyConfig
from idx_engine.fetch.index_weights import SECTOR_ETFS
from idx_engine.ledger.lots import LotLedger
from idx_engine.ledger.wash_sale import WashSaleResolver
from idx_engine.models import ZERO
from idx_engine.strategy.base import DayMarketState, liquidate_pro_rata_for_cash
from idx_engine.strategy.sizing import (
    FRAC_QTY_QUANT,
    compute_cash_inflow_buys,
    compute_target_shares,
)

TRADING_DAYS_PER_YEAR = Decimal("252")
BPS_DIVISOR = Decimal("10000")
REBALANCE_STEP_DAYS: dict[str, int] = {
    "monthly": 21,
    "quarterly": 63,
    "annual": 252,
}


def select_stratified_weights(
    index_weights: Mapping[str, Decimal],
    sectors: Mapping[str, str],
    prices: Mapping[str, Decimal],
    sample_size: int | None,
) -> dict[str, Decimal]:
    """Select either full constituent weights or a sector-stratified top-K basket whose
    within-sector weights are renormalized to preserve exact GICS sector weights.
    """
    tradable = {t: w for t, w in index_weights.items() if w > ZERO and prices.get(t, ZERO) > ZERO}
    if not tradable:
        return {}

    total_w = sum(tradable.values(), ZERO)
    norm_all = {t: w / total_w for t, w in tradable.items()}

    if sample_size is None or sample_size >= len(norm_all):
        return norm_all

    by_sector: dict[str, list[tuple[Decimal, str]]] = {}
    sector_totals: dict[str, Decimal] = {}
    for ticker, w in norm_all.items():
        sec = sectors.get(ticker, "Unknown")
        by_sector.setdefault(sec, []).append((w, ticker))
        sector_totals[sec] = sector_totals.get(sec, ZERO) + w

    for sec_list in by_sector.values():
        sec_list.sort(key=lambda item: (-item[0], item[1]))

    # Allocate integer stock counts per sector proportional to sector weight
    active_sectors = sorted(sector_totals.keys())
    counts: dict[str, int] = {}
    remainders: list[tuple[Decimal, str]] = []

    if sample_size >= len(active_sectors):
        # Guarantee at least 1 representative per active sector when K >= #sectors
        for sec in active_sectors:
            counts[sec] = 1
        rem_slots = sample_size - len(active_sectors)
        for sec in active_sectors:
            cap = len(by_sector[sec]) - 1
            exact_extra = Decimal(rem_slots) * sector_totals[sec]
            add = min(cap, int(exact_extra))
            counts[sec] += add
            remainders.append((exact_extra - Decimal(add), sec))
    else:
        for sec in active_sectors:
            exact = Decimal(sample_size) * sector_totals[sec]
            c = min(len(by_sector[sec]), int(exact))
            counts[sec] = c
            remainders.append((exact - Decimal(c), sec))

    allocated = sum(counts.values())
    remainders.sort(key=lambda item: (-item[0], item[1]))
    while allocated < sample_size:
        progressed = False
        for _, sec in remainders:
            if allocated >= sample_size:
                break
            if counts[sec] < len(by_sector[sec]):
                counts[sec] += 1
                allocated += 1
                progressed = True
        if not progressed:
            break

    selected: dict[str, Decimal] = {}
    for sec, count in counts.items():
        if count <= 0:
            continue
        chosen = by_sector[sec][:count]
        chosen_sum = sum((w for w, _ in chosen), ZERO)
        target_sec_w = sector_totals[sec]
        if chosen_sum > ZERO:
            for w, ticker in chosen:
                selected[ticker] = target_sec_w * (w / chosen_sum)

    sel_sum = sum(selected.values(), ZERO)
    if sel_sum > ZERO:
        return {t: w / sel_sum for t, w in selected.items()}
    return selected


class DirectIndexStrategy:
    """Full or stratified S&P 500 direct indexing with TLH, 31-day wash-sale lockouts,
    sector substitute routing, and wash-sale-aware rebalancing.
    """

    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg
        # Maps locked-out ticker -> substitute ticker currently holding its sector exposure
        self._active_substitutes: dict[str, str] = {}
        self._resolver: WashSaleResolver | None = None
        self._accrued_fee: Decimal = ZERO

    @property
    def name(self) -> str:
        return self.cfg.name

    def _fee_reserve(self, total_equity: Decimal) -> Decimal:
        """Keep ~1 quarter of advisory fee cash buffer so whole-share portfolios don't
        liquidate single shares daily just to pay $1-$2 of advisory fee.
        """
        if self.cfg.direct_expense_ratio_bps <= ZERO:
            return ZERO
        quarterly_fee = (
            total_equity * (self.cfg.direct_expense_ratio_bps / BPS_DIVISOR) / Decimal("4")
        )
        return max(Decimal("25"), quarterly_fee)

    def on_day(
        self,
        state: DayMarketState,
        ledger: LotLedger,
        resolver: WashSaleResolver,
    ) -> None:
        self._resolver = resolver
        # 1. Accrue and settle daily platform/advisory fee if configured
        if self.cfg.direct_expense_ratio_bps > ZERO and state.day_idx > 0:
            total_eq = max(
                ZERO, ledger.market_value(state.prices) + ledger.cash - self._accrued_fee
            )
            daily_fee = (
                total_eq * (self.cfg.direct_expense_ratio_bps / BPS_DIVISOR) / TRADING_DAYS_PER_YEAR
            )
            self._accrued_fee += daily_fee
            if ledger.cash >= self._accrued_fee:
                ledger.cash -= self._accrued_fee
                self._accrued_fee = ZERO
            elif state.day_idx % 63 == 0:
                liquidate_pro_rata_for_cash(
                    trade_date=state.current_date,
                    amount_needed=self._accrued_fee,
                    prices=state.prices,
                    ledger=ledger,
                    allow_fractional=self.cfg.allow_fractional,
                    reason="advisory_fee",
                    resolver=resolver,
                )
                paid = min(ledger.cash, self._accrued_fee)
                ledger.cash -= paid
                self._accrued_fee -= paid

        # 2. Expire substitute mappings whose 30-day wash-sale lockout has elapsed
        if self._active_substitutes:
            expired = [
                orig
                for orig in self._active_substitutes
                if not resolver.is_locked_out(orig, state.current_date)
            ]
            for orig in expired:
                self._active_substitutes.pop(orig, None)

        if state.day_idx == 0:
            raw_targets = select_stratified_weights(
                index_weights=state.index_weights,
                sectors=state.sectors,
                prices=state.prices,
                sample_size=self.cfg.sample_size,
            )
            if raw_targets:
                self._rebalance_to_weights(
                    trade_date=state.current_date,
                    effective_weights=raw_targets,
                    prices=state.prices,
                    sectors=state.sectors,
                    ledger=ledger,
                    resolver=resolver,
                    reason="initial_alloc",
                )
            return

        tlh_today = self.cfg.tlh_enabled and state.day_idx % self.cfg.tlh_check_frequency_days == 0
        mode = self.cfg.rebalance_mode
        rebalance_check_today = (
            mode in REBALANCE_STEP_DAYS and state.day_idx % REBALANCE_STEP_DAYS[mode] == 0
        ) or (mode == "drift_band" and state.day_idx % 5 == 0)
        min_cash = Decimal("50") if self.cfg.allow_fractional else Decimal("200")

        if not tlh_today and not rebalance_check_today and ledger.cash < min_cash:
            return

        # 4. Scheduled Tax-Loss Harvesting scan
        if tlh_today:
            self._run_tlh_scan(state, ledger, resolver)

        if not rebalance_check_today and ledger.cash < min_cash:
            return

        raw_targets = select_stratified_weights(
            index_weights=state.index_weights,
            sectors=state.sectors,
            prices=state.prices,
            sample_size=self.cfg.sample_size,
        )
        if not raw_targets:
            return

        # 5. Build wash-sale-aware effective target weights (route locked-out weights to substitutes)
        effective_targets = self._build_effective_weights(
            raw_targets=raw_targets,
            state=state,
            resolver=resolver,
        )

        # 6. Check scheduled or drift-band rebalance trigger
        if rebalance_check_today and self._should_rebalance(state, effective_targets, ledger):
            self._rebalance_to_weights(
                trade_date=state.current_date,
                effective_weights=effective_targets,
                prices=state.prices,
                sectors=state.sectors,
                ledger=ledger,
                resolver=resolver,
                reason="rebalance",
            )
        else:
            # Deploy any meaningful idle cash (periodic deposits, dividends, reinvested tax refunds)
            self._deploy_idle_cash(state, effective_targets, ledger, resolver)

    def _has_recent_unharvestable_buy(
        self,
        ticker: str,
        current_date: date,
        prices: Mapping[str, Decimal],
        ledger: LotLedger,
        resolver: WashSaleResolver,
    ) -> bool:
        """Return True if there is an open lot in the same wash-sale group purchased within
        the last 30 calendar days that is NOT also eligible to be harvested today (which
        would otherwise absorb the harvested loss as a pre-sale wash sale).
        """
        for grp_ticker in resolver.group_members(ticker):
            for lot in ledger.open_lots(grp_ticker):
                buy_dt = lot.original_acquired_date or lot.acquired_date
                if abs((current_date - buy_dt).days) > resolver.window_days:
                    continue
                px = prices.get(lot.ticker, ZERO)
                if px <= ZERO or lot.cost_basis_total <= ZERO:
                    continue
                pnl = lot.quantity * px - lot.cost_basis_total
                loss_pct = (-pnl / lot.cost_basis_total) if pnl < ZERO else ZERO
                if not (
                    pnl < ZERO
                    and loss_pct >= self.cfg.tlh_loss_pct_threshold
                    and (-pnl) >= self.cfg.tlh_min_loss_dollars
                ):
                    return True
        return False

    def _choose_substitute(
        self,
        harvested_ticker: str,
        sector: str,
        state: DayMarketState,
        resolver: WashSaleResolver,
    ) -> str | None:
        if self.cfg.substitute_mode == "cash":
            return None

        if self.cfg.substitute_mode == "sector_etf":
            sec_etf = SECTOR_ETFS.get(sector)
            if (
                sec_etf
                and state.prices.get(sec_etf, ZERO) > ZERO
                and not resolver.is_locked_out(sec_etf, state.current_date)
                and not resolver.are_substantially_identical(sec_etf, harvested_ticker)
            ):
                return sec_etf

        # Find highest-index-weight non-locked-out peer in the same GICS sector
        same_sector_peers = [
            (state.index_weights.get(cand, ZERO), cand)
            for cand, cand_sec in state.sectors.items()
            if cand_sec == sector
            and cand != harvested_ticker
            and state.prices.get(cand, ZERO) > ZERO
            and not resolver.are_substantially_identical(cand, harvested_ticker)
            and not resolver.is_locked_out(cand, state.current_date)
        ]
        if same_sector_peers:
            same_sector_peers.sort(key=lambda item: (-item[0], item[1]))
            return same_sector_peers[0][1]

        # Fallback: highest-weight non-locked-out constituent across the broad index
        broad_peers = [
            (w, cand)
            for cand, w in state.index_weights.items()
            if cand != harvested_ticker
            and state.prices.get(cand, ZERO) > ZERO
            and not resolver.are_substantially_identical(cand, harvested_ticker)
            and not resolver.is_locked_out(cand, state.current_date)
        ]
        if broad_peers:
            broad_peers.sort(key=lambda item: (-item[0], item[1]))
            return broad_peers[0][1]

        return None

    def _run_tlh_scan(
        self,
        state: DayMarketState,
        ledger: LotLedger,
        resolver: WashSaleResolver,
    ) -> None:
        # Step 1: Pre-compute ALL eligible lots across the entire portfolio BEFORE executing
        # any sales. This guarantees that harvesting one class of a dual-class stock (e.g. GOOG)
        # cannot split a sibling class lot (e.g. GOOGL) mid-loop and cause its remainder to drop
        # below `tlh_min_loss_dollars`.
        candidate_tickers = sorted(ledger.all_positions().keys())
        planned_lot_ids_by_ticker: dict[str, list[str]] = {}
        for ticker in candidate_tickers:
            px = state.prices.get(ticker, ZERO)
            if px <= ZERO:
                continue
            if self._has_recent_unharvestable_buy(
                ticker, state.current_date, state.prices, ledger, resolver
            ):
                continue
            eligible_ids: list[str] = []
            for lot in ledger.open_lots(ticker):
                if lot.cost_basis_total <= ZERO:
                    continue
                pnl = lot.quantity * px - lot.cost_basis_total
                if pnl >= ZERO:
                    continue
                loss_dollars = -pnl
                loss_pct = loss_dollars / lot.cost_basis_total
                if (
                    loss_pct >= self.cfg.tlh_loss_pct_threshold
                    and loss_dollars >= self.cfg.tlh_min_loss_dollars
                ):
                    eligible_ids.append(lot.lot_id)
            if eligible_ids:
                planned_lot_ids_by_ticker[ticker] = eligible_ids

        if not planned_lot_ids_by_ticker:
            return

        # Step 2: Group sales by wash-sale canonical group. Within each group, pass all
        # planned lots per ticker into a single `ledger.sell` call (ordered OLDEST acquisition
        # first across dual-class tickers) so sibling lots being disposed in the same TLH batch
        # are already removed from `open_lots` before `WashSaleResolver` inspects open lots!
        groups_to_harvest: dict[str, list[str]] = {}
        for ticker in planned_lot_ids_by_ticker:
            grp = resolver.canonical_group(ticker)
            groups_to_harvest.setdefault(grp, []).append(ticker)

        harvested_proceeds_by_ticker: dict[str, tuple[Decimal, str]] = {}
        for _, grp_tickers in sorted(groups_to_harvest.items()):
            # Order tickers in a dual-class group by earliest open-lot buy date first so if
            # GOOG was bought Jan 2 and GOOGL was bought Jan 10, GOOG closes before GOOGL
            # (or better: if both are being harvested, neither remains open at the end).
            grp_tickers.sort(
                key=lambda t: (
                    min(
                        (lot.original_acquired_date or lot.acquired_date)
                        for lot in ledger.open_lots(t)
                    )
                    if ledger.open_lots(t)
                    else state.current_date
                )
            )
            # Temporarily suppress intra-batch sibling wash matching across dual-class legs
            # that are BOTH being closed in the exact same TLH transaction batch
            if len(grp_tickers) == 1:
                t = grp_tickers[0]
                planned_ids = set(planned_lot_ids_by_ticker[t])
                live_lots = [lot for lot in ledger.open_lots(t) if lot.lot_id in planned_ids]
                live_ids = [lot.lot_id for lot in live_lots]
                total_qty = sum((lot.quantity for lot in live_lots), ZERO)
                px = state.prices.get(t, ZERO)
                if live_ids and total_qty > ZERO and px > ZERO:
                    sec = live_lots[0].sector
                    closed_records, _ = ledger.sell(
                        trade_date=state.current_date,
                        ticker=t,
                        quantity=total_qty,
                        price=px,
                        policy="specific_lot",
                        specific_lot_ids=live_ids,
                        reason="tlh_harvest",
                    )
                    proceeds = sum((c.proceeds_total for c in closed_records), ZERO)
                    harvested_proceeds_by_ticker[t] = (proceeds, sec)
            else:
                # Dual-class simultaneous harvest: sell all planned lots in the group first,
                # then invoke the wash-sale hook once on the combined closed records so none of
                # the simultaneously liquidated sibling lots absorb a phantom wash rollover.
                saved_hook = ledger._wash_sale_hook
                ledger.set_wash_sale_hook(None)
                batch_closed = []
                try:
                    for t in grp_tickers:
                        planned_ids = set(planned_lot_ids_by_ticker[t])
                        live_lots = [
                            lot for lot in ledger.open_lots(t) if lot.lot_id in planned_ids
                        ]
                        live_ids = [lot.lot_id for lot in live_lots]
                        total_qty = sum((lot.quantity for lot in live_lots), ZERO)
                        px = state.prices.get(t, ZERO)
                        if live_ids and total_qty > ZERO and px > ZERO:
                            sec = live_lots[0].sector
                            closed_records, _ = ledger.sell(
                                trade_date=state.current_date,
                                ticker=t,
                                quantity=total_qty,
                                price=px,
                                policy="specific_lot",
                                specific_lot_ids=live_ids,
                                reason="tlh_harvest",
                            )
                            proceeds = sum((c.proceeds_total for c in closed_records), ZERO)
                            harvested_proceeds_by_ticker[t] = (proceeds, sec)
                            batch_closed.extend(closed_records)
                finally:
                    ledger.set_wash_sale_hook(saved_hook)
                if saved_hook is not None and batch_closed:
                    saved_hook(ledger, "SELL", batch_closed)

        # Reinvest harvested proceeds into sector substitutes
        for orig_ticker, (proceeds, sector) in harvested_proceeds_by_ticker.items():
            sub_ticker = self._choose_substitute(orig_ticker, sector, state, resolver)
            if sub_ticker is None:
                continue
            sub_px = state.prices.get(sub_ticker, ZERO)
            if sub_px <= ZERO:
                continue
            budget = min(proceeds, ledger.cash)
            if self.cfg.allow_fractional:
                qty = (budget / sub_px).quantize(FRAC_QTY_QUANT, rounding=ROUND_DOWN)
            else:
                qty = Decimal(int(budget // sub_px))
            if qty > ZERO:
                ledger.buy(
                    trade_date=state.current_date,
                    ticker=sub_ticker,
                    sector=state.sectors.get(sub_ticker, sector),
                    quantity=qty,
                    price=sub_px,
                    reason="tlh_substitute_buy",
                )
                self._active_substitutes[orig_ticker] = sub_ticker

    def _build_effective_weights(
        self,
        raw_targets: Mapping[str, Decimal],
        state: DayMarketState,
        resolver: WashSaleResolver,
    ) -> dict[str, Decimal]:
        effective: dict[str, Decimal] = {}
        for ticker, weight in raw_targets.items():
            if resolver.is_locked_out(ticker, state.current_date):
                sub = self._active_substitutes.get(ticker)
                if sub is None or resolver.is_locked_out(sub, state.current_date):
                    sub = self._choose_substitute(
                        ticker,
                        state.sectors.get(ticker, "Unknown"),
                        state,
                        resolver,
                    )
                if sub is not None:
                    effective[sub] = effective.get(sub, ZERO) + weight
            else:
                effective[ticker] = effective.get(ticker, ZERO) + weight

        w_sum = sum(effective.values(), ZERO)
        if w_sum > ZERO and self.cfg.substitute_mode != "cash":
            return {t: w / w_sum for t, w in effective.items()}
        return effective

    def _should_rebalance(
        self,
        state: DayMarketState,
        effective_targets: Mapping[str, Decimal],
        ledger: LotLedger,
    ) -> bool:
        mode = self.cfg.rebalance_mode
        if mode == "none":
            return False
        if mode in REBALANCE_STEP_DAYS:
            return state.day_idx % REBALANCE_STEP_DAYS[mode] == 0
        if mode == "drift_band":
            # Check drift every 5 trading days to keep turnover realistic
            if state.day_idx % 5 != 0:
                return False
            total_eq = ledger.market_value(state.prices) + ledger.cash
            if total_eq <= ZERO:
                return False
            positions = ledger.all_positions()
            universe = set(effective_targets.keys()) | set(positions.keys())
            for t in universe:
                cur_w = (positions.get(t, ZERO) * state.prices.get(t, ZERO)) / total_eq
                tgt_w = effective_targets.get(t, ZERO)
                if abs(cur_w - tgt_w) > self.cfg.drift_band_abs:
                    return True
        return False

    def _rebalance_to_weights(
        self,
        trade_date: date,
        effective_weights: Mapping[str, Decimal],
        prices: Mapping[str, Decimal],
        sectors: Mapping[str, str],
        ledger: LotLedger,
        resolver: WashSaleResolver,
        reason: str,
    ) -> None:
        total_eq = ledger.market_value(prices) + ledger.cash
        if total_eq <= ZERO:
            return
        fee_reserve = self._fee_reserve(total_eq) if reason != "initial_alloc" else ZERO
        investable_budget = max(ZERO, total_eq - fee_reserve)

        desired_shares = compute_target_shares(
            total_budget=investable_budget,
            target_weights=effective_weights,
            prices=prices,
            allow_fractional=self.cfg.allow_fractional,
        )
        current_positions = ledger.all_positions()

        # Execute SELLs first so proceeds fund BUYs cleanly
        all_tickers = sorted(set(current_positions.keys()) | set(desired_shares.keys()))
        for ticker in all_tickers:
            cur_qty = current_positions.get(ticker, ZERO)
            tgt_qty = desired_shares.get(ticker, ZERO)
            px = prices.get(ticker, ZERO)
            if cur_qty > tgt_qty and px > ZERO:
                sell_qty = cur_qty - tgt_qty
                # Skip tiny dust trades (< $25) during rebalance unless exiting completely
                if tgt_qty > ZERO and sell_qty * px < Decimal("25"):
                    continue
                # Avoid selling a position at a loss during routine rebalance if any lot in its
                # wash-sale group was acquired within the last 30 calendar days, or if selling
                # at a loss when we might keep/repurchase the group within 30 days.
                if reason == "rebalance":
                    group_lots = [
                        lot
                        for grp_t in resolver.group_members(ticker)
                        for lot in ledger.open_lots(grp_t)
                    ]
                    has_loss_lot = any(
                        (lot.quantity * prices.get(lot.ticker, ZERO)) < lot.cost_basis_total
                        for lot in ledger.open_lots(ticker)
                    )
                    if has_loss_lot:
                        recent_group_buy = any(
                            abs(
                                (
                                    trade_date - (lot.original_acquired_date or lot.acquired_date)
                                ).days
                            )
                            <= resolver.window_days
                            for lot in group_lots
                        )
                        # If there was a recent buy in this group (and we aren't exiting the whole
                        # group in a single lot), skip routine rebalance sale to avoid wash sales.
                        if recent_group_buy and (tgt_qty > ZERO or len(group_lots) > 1):
                            continue
                ledger.sell(
                    trade_date=trade_date,
                    ticker=ticker,
                    quantity=sell_qty,
                    price=px,
                    policy=self.cfg.lot_policy,
                    reason=reason,
                )

        # Execute BUYs on underweight, non-locked-out positions within available cash
        buy_requests: list[tuple[Decimal, str, Decimal, Decimal]] = []
        updated_positions = ledger.all_positions()
        for ticker in all_tickers:
            if resolver.is_locked_out(ticker, trade_date):
                continue
            cur_qty = updated_positions.get(ticker, ZERO)
            tgt_qty = desired_shares.get(ticker, ZERO)
            px = prices.get(ticker, ZERO)
            if tgt_qty > cur_qty and px > ZERO:
                diff_qty = tgt_qty - cur_qty
                dollar_val = diff_qty * px
                if reason == "rebalance" and dollar_val < Decimal("25"):
                    continue
                buy_requests.append((dollar_val, ticker, diff_qty, px))

        # Prioritize largest dollar underweights first
        buy_requests.sort(key=lambda item: (-item[0], item[1]))
        for _, ticker, diff_qty, px in buy_requests:
            spendable = ledger.cash - fee_reserve
            if spendable <= ZERO:
                break
            max_affordable = (
                (spendable / px).quantize(FRAC_QTY_QUANT, rounding=ROUND_DOWN)
                if self.cfg.allow_fractional
                else Decimal(int(spendable // px))
            )
            qty = min(diff_qty, max_affordable)
            if qty > ZERO:
                ledger.buy(
                    trade_date=trade_date,
                    ticker=ticker,
                    sector=sectors.get(ticker, "Unknown"),
                    quantity=qty,
                    price=px,
                    reason=reason,
                )

    def _deploy_idle_cash(
        self,
        state: DayMarketState,
        effective_targets: Mapping[str, Decimal],
        ledger: LotLedger,
        resolver: WashSaleResolver,
    ) -> None:
        total_eq = ledger.market_value(state.prices) + ledger.cash
        if total_eq <= ZERO:
            return

        allowed_targets = {
            t: w
            for t, w in effective_targets.items()
            if not resolver.is_locked_out(t, state.current_date)
        }
        w_sum = min(Decimal("1"), sum(allowed_targets.values(), ZERO))
        lockout_cash_target = total_eq * (Decimal("1") - w_sum)
        reserved_cash = max(lockout_cash_target, self._fee_reserve(total_eq))
        deployable_cash = ledger.cash - reserved_cash

        min_cash = Decimal("50") if self.cfg.allow_fractional else Decimal("200")
        if deployable_cash < min_cash:
            return

        buys = compute_cash_inflow_buys(
            available_cash=deployable_cash,
            current_quantities=ledger.all_positions(),
            target_weights=allowed_targets,
            prices=state.prices,
            allow_fractional=self.cfg.allow_fractional,
        )
        # Concentrate cash inflows into underweight positions with >= $25 order size
        # to avoid creating thousands of penny micro-lots on small dividend days
        ordered_buys = sorted(
            ((buys[t] * state.prices.get(t, ZERO), t) for t in buys),
            key=lambda item: (-item[0], item[1]),
        )
        for dollar_amt, ticker in ordered_buys:
            if dollar_amt < Decimal("25"):
                continue
            qty = buys[ticker]
            px = state.prices.get(ticker, ZERO)
            if qty <= ZERO or px <= ZERO:
                continue
            spendable = ledger.cash - reserved_cash
            if spendable <= ZERO:
                break
            max_affordable = (
                (spendable / px).quantize(FRAC_QTY_QUANT, rounding=ROUND_DOWN)
                if self.cfg.allow_fractional
                else Decimal(int(spendable // px))
            )
            actual_qty = min(qty, max_affordable)
            if actual_qty > ZERO:
                ledger.buy(
                    trade_date=state.current_date,
                    ticker=ticker,
                    sector=state.sectors.get(ticker, "Unknown"),
                    quantity=actual_qty,
                    price=px,
                    reason="cash_invest",
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
