"""Unit tests for 61-calendar-day IRS §1091 wash-sale matching, basis rollover, and tacking."""

from datetime import date
from decimal import Decimal

from idx_engine.ledger.lots import LotLedger
from idx_engine.ledger.wash_sale import WashSaleResolver
from idx_engine.models import CorporateAction


def test_post_sale_repurchase_within_30_calendar_days_rolls_basis_and_tacks_holding() -> None:
    resolver = WashSaleResolver()
    ledger = LotLedger(initial_cash=Decimal("50000"), default_policy="HIFO")
    resolver.attach(ledger)

    # Buy 100 AAPL on Jan 1 @ $100 ($10,000 basis), hold 200 days, sell Jul 20 @ $80 (-$2,000 loss)
    ledger.buy(date(2023, 1, 1), "AAPL", "Tech", Decimal("100"), Decimal("100"))
    closed, _ = ledger.sell(date(2023, 7, 20), "AAPL", Decimal("100"), Decimal("80"))
    assert closed[0].raw_realized_gain_loss == Decimal("-2000")
    assert closed[0].disallowed_wash_loss == Decimal("0")

    # Repurchase 100 AAPL 15 calendar days later on Aug 4 @ $82 ($8,200 purchase)
    repl_lot, _ = ledger.buy(date(2023, 8, 4), "AAPL", "Tech", Decimal("100"), Decimal("82"))

    # Entire $2,000 loss is disallowed and rolled into repl_lot ($8,200 + $2,000 = $10,200 basis)
    updated_closed = ledger.closed_lots[0]
    assert updated_closed.disallowed_wash_loss == Decimal("2000")
    assert updated_closed.adjusted_realized_gain_loss == Decimal("0")

    assert repl_lot.cost_basis_total == Decimal("10200")
    assert repl_lot.wash_sale_disallowed == Decimal("2000")
    assert repl_lot.unit_cost == Decimal("102")
    # Holding period tacked back by 200 days from Aug 4 -> Jan 16, 2023
    assert (date(2023, 8, 4) - repl_lot.acquired_date).days == 200
    assert repl_lot.original_acquired_date == date(2023, 8, 4)


def test_partial_replacement_splits_lot_and_preserves_partial_deductible_loss() -> None:
    resolver = WashSaleResolver()
    ledger = LotLedger(initial_cash=Decimal("50000"), default_policy="HIFO")
    resolver.attach(ledger)

    # Buy 100 NVDA @ $200, sell 100 @ $150 -> -$5,000 raw loss ($50/sh)
    ledger.buy(date(2023, 2, 1), "NVDA", "Tech", Decimal("100"), Decimal("200"))
    ledger.sell(date(2023, 4, 1), "NVDA", Decimal("100"), Decimal("150"))

    # Repurchase only 40 NVDA 10 days later @ $160 -> 40% ($2,000) disallowed, 60% (-$3,000) allowed
    ledger.buy(date(2023, 4, 11), "NVDA", "Tech", Decimal("40"), Decimal("160"))
    closed = ledger.closed_lots[0]
    assert closed.disallowed_wash_loss == Decimal("2000")
    assert closed.adjusted_realized_gain_loss == Decimal("-3000")

    # Now buy 100 shares on Apr 20 (still within 30 days of Apr 1!):
    # Only the remaining 60 unmatched loss shares match, splitting the 100-share purchase into 60 + 40!
    ledger.buy(date(2023, 4, 20), "NVDA", "Tech", Decimal("100"), Decimal("155"))
    closed_after = ledger.closed_lots[0]
    assert closed_after.disallowed_wash_loss == Decimal("5000")
    assert closed_after.adjusted_realized_gain_loss == Decimal("0")

    open_nvda = ledger.open_lots("NVDA")
    assert len(open_nvda) == 3  # 40 matched + 60 matched + 40 clean remainder
    total_qty = sum(lot.quantity for lot in open_nvda)
    assert total_qty == Decimal("140")

    # Clean remainder lot of 40 shares @ $155 has zero wash_sale_disallowed and Apr 20 date
    clean_lots = [lot for lot in open_nvda if lot.wash_sale_disallowed == Decimal("0")]
    assert len(clean_lots) == 1
    assert clean_lots[0].quantity == Decimal("40")
    assert clean_lots[0].unit_cost == Decimal("155")
    assert clean_lots[0].acquired_date == date(2023, 4, 20)


def test_pre_sale_purchase_within_30_days_and_substantially_identical_dual_class() -> None:
    resolver = WashSaleResolver(substantially_identical_groups=[["GOOG", "GOOGL"]])
    ledger = LotLedger(initial_cash=Decimal("100000"), default_policy="specific_lot")
    resolver.attach(ledger)

    # Hold GOOGL bought Jan 1 @ $120
    lot_old, _ = ledger.buy(date(2023, 1, 1), "GOOGL", "Comm", Decimal("50"), Decimal("120"))
    # Buy GOOG (substantially identical!) on May 10 @ $100
    lot_new, _ = ledger.buy(date(2023, 5, 10), "GOOG", "Comm", Decimal("50"), Decimal("100"))
    # Sell lot_old GOOGL 10 days later on May 20 @ $98 (-$1,100 loss)
    closed, _ = ledger.sell(
        date(2023, 5, 20),
        "GOOGL",
        Decimal("50"),
        Decimal("98"),
        policy="specific_lot",
        specific_lot_id=lot_old.lot_id,
    )
    assert closed[0].disallowed_wash_loss == Decimal("1100")
    assert closed[0].adjusted_realized_gain_loss == Decimal("0")

    goog_lot = ledger.get_lot(lot_new.lot_id)
    assert goog_lot is not None
    assert goog_lot.cost_basis_total == Decimal("6100")  # $5,000 + $1,100


def test_partial_sale_of_single_lot_does_not_wash_against_its_own_remainder() -> None:
    resolver = WashSaleResolver()
    ledger = LotLedger(initial_cash=Decimal("50000"))
    resolver.attach(ledger)

    # Single purchase of 100 shares, sell 40 shares 10 days later at a loss -> NOT a wash sale
    ledger.buy(date(2023, 3, 1), "AMZN", "Consumer", Decimal("100"), Decimal("100"))
    closed, _ = ledger.sell(date(2023, 3, 11), "AMZN", Decimal("40"), Decimal("90"))
    assert closed[0].raw_realized_gain_loss == Decimal("-400")
    assert closed[0].disallowed_wash_loss == Decimal("0")
    assert closed[0].adjusted_realized_gain_loss == Decimal("-400")


def test_repurchase_on_day_31_is_outside_wash_window() -> None:
    resolver = WashSaleResolver()
    ledger = LotLedger(initial_cash=Decimal("50000"))
    resolver.attach(ledger)

    ledger.buy(date(2023, 1, 1), "META", "Comm", Decimal("10"), Decimal("200"))
    ledger.sell(date(2023, 3, 1), "META", Decimal("10"), Decimal("150"))
    assert resolver.is_locked_out("META", date(2023, 3, 31)) is True  # Day 30
    assert resolver.is_locked_out("META", date(2023, 4, 1)) is False  # Day 31

    repl, _ = ledger.buy(date(2023, 4, 1), "META", "Comm", Decimal("10"), Decimal("155"))
    assert ledger.closed_lots[0].disallowed_wash_loss == Decimal("0")
    assert repl.cost_basis_total == Decimal("1550")


def test_wash_sale_scales_replacement_share_matching_across_stock_split_and_rename() -> None:
    resolver = WashSaleResolver()
    ledger = LotLedger(initial_cash=Decimal("100000"))
    resolver.attach(ledger)

    # Buy 100 pre-split shares @ $100 ($10,000), sell @ $60 (-$4,000 loss)
    ledger.buy(date(2023, 1, 2), "FB", "Comm", Decimal("100"), Decimal("100"))
    ledger.sell(date(2023, 3, 1), "FB", Decimal("100"), Decimal("60"))

    # Stock splits 4-for-1 on Mar 10 and renames FB -> META on Mar 12
    ledger.apply_corporate_action(
        CorporateAction(
            action_id="split_fb",
            effective_date=date(2023, 3, 10),
            action_type="split",
            ticker="FB",
            ratio_numerator=Decimal("4"),
            ratio_denominator=Decimal("1"),
        )
    )
    ledger.apply_corporate_action(
        CorporateAction(
            action_id="rename_fb",
            effective_date=date(2023, 3, 12),
            action_type="ticker_change",
            ticker="FB",
            target_ticker="META",
        )
    )

    # Repurchase 100 post-split META shares (= 25 pre-split shares = 25% replacement!) on Mar 20 @ $16
    repl, _ = ledger.buy(date(2023, 3, 20), "META", "Comm", Decimal("100"), Decimal("16"))
    closed = ledger.closed_lots[0]
    assert closed.disallowed_wash_loss == Decimal("1000")  # 25% of $4,000, NOT $4,000!
    assert closed.adjusted_realized_gain_loss == Decimal("-3000")
    assert repl.cost_basis_total == Decimal("2600")  # $1,600 + $1,000 rolled basis

