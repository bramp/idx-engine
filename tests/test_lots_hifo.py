"""Unit tests for TaxLot, HIFO/FIFO/specific_lot disposal, and corporate actions."""

from datetime import date
from decimal import Decimal

import pytest

from idx_engine.ledger.lots import LotLedger
from idx_engine.models import CorporateAction


def test_hifo_sells_highest_unit_cost_first_and_preserves_basis() -> None:
    ledger = LotLedger(initial_cash=Decimal("50000"), default_policy="HIFO")
    lot_low, _ = ledger.buy(date(2023, 1, 10), "AAPL", "Tech", Decimal("10"), Decimal("130"))
    lot_high, _ = ledger.buy(date(2023, 3, 15), "AAPL", "Tech", Decimal("10"), Decimal("180"))
    lot_mid, _ = ledger.buy(date(2023, 6, 20), "AAPL", "Tech", Decimal("10"), Decimal("150"))

    initial_total_basis = ledger.position_cost_basis("AAPL")
    assert initial_total_basis == Decimal("4600")

    closed, fill = ledger.sell(date(2024, 2, 1), "AAPL", Decimal("15"), Decimal("140"))
    assert fill.gross_amount == Decimal("2100")
    assert len(closed) == 2

    # First closed lot is lot_high (10 shares @ $180 basis -> -$400 loss, ST)
    assert closed[0].source_lot_id == lot_high.lot_id
    assert closed[0].quantity == Decimal("10")
    assert closed[0].cost_basis_total == Decimal("1800")
    assert closed[0].raw_realized_gain_loss == Decimal("-400")
    assert closed[0].term == "ST"

    # Second closed lot is 5 shares of lot_mid (5 shares @ $150 basis -> -$50 loss)
    assert closed[1].source_lot_id == lot_mid.lot_id
    assert closed[1].quantity == Decimal("5")
    assert closed[1].cost_basis_total == Decimal("750")
    assert closed[1].raw_realized_gain_loss == Decimal("-50")

    # Remaining basis + sold basis == initial basis (exact conservation)
    sold_basis = sum(c.cost_basis_total for c in closed)
    rem_basis = ledger.position_cost_basis("AAPL")
    assert sold_basis + rem_basis == initial_total_basis
    assert ledger.position_quantity("AAPL") == Decimal("15")

    rem_mid = ledger.get_lot(lot_mid.lot_id)
    assert rem_mid is not None
    assert rem_mid.quantity == Decimal("5")
    assert rem_mid.unit_cost == Decimal("150")
    assert ledger.get_lot(lot_low.lot_id) is not None


def test_fifo_and_specific_lot_selection_and_lt_term() -> None:
    ledger = LotLedger(initial_cash=Decimal("20000"), default_policy="FIFO")
    lot1, _ = ledger.buy(date(2022, 1, 5), "MSFT", "Tech", Decimal("5"), Decimal("300"))
    lot2, _ = ledger.buy(date(2023, 6, 5), "MSFT", "Tech", Decimal("5"), Decimal("250"))

    # Specific lot sale of lot2
    closed_spec, _ = ledger.sell(
        date(2023, 8, 1),
        "MSFT",
        Decimal("2"),
        Decimal("320"),
        policy="specific_lot",
        specific_lot_id=lot2.lot_id,
    )
    assert len(closed_spec) == 1
    assert closed_spec[0].source_lot_id == lot2.lot_id
    assert closed_spec[0].term == "ST"

    # FIFO sale picks lot1 (>365 days -> LT)
    closed_fifo, _ = ledger.sell(date(2023, 8, 1), "MSFT", Decimal("5"), Decimal("320"))
    assert len(closed_fifo) == 1
    assert closed_fifo[0].source_lot_id == lot1.lot_id
    assert closed_fifo[0].term == "LT"
    assert closed_fifo[0].raw_realized_gain_loss == Decimal("100")


def test_holding_term_uses_calendar_anniversary() -> None:
    ledger = LotLedger(initial_cash=Decimal("1000"))
    ledger.buy(date(2023, 3, 1), "AAPL", "Tech", Decimal("2"), Decimal("100"))

    anniversary_sale, _ = ledger.sell(date(2024, 3, 1), "AAPL", Decimal("1"), Decimal("100"))
    after_anniversary_sale, _ = ledger.sell(date(2024, 3, 2), "AAPL", Decimal("1"), Decimal("100"))

    assert anniversary_sale[0].holding_days == 366
    assert anniversary_sale[0].term == "ST"
    assert after_anniversary_sale[0].term == "LT"

    leap_day_ledger = LotLedger(initial_cash=Decimal("1000"))
    leap_day_ledger.buy(date(2024, 2, 29), "AAPL", "Tech", Decimal("2"), Decimal("100"))
    february_sale, _ = leap_day_ledger.sell(date(2025, 2, 28), "AAPL", Decimal("1"), Decimal("100"))
    march_sale, _ = leap_day_ledger.sell(date(2025, 3, 1), "AAPL", Decimal("1"), Decimal("100"))

    assert february_sale[0].term == "ST"
    assert march_sale[0].term == "LT"


def test_duplicate_specific_lot_ids_are_rejected_without_selling() -> None:
    ledger = LotLedger(initial_cash=Decimal("1000"))
    lot, _ = ledger.buy(date(2024, 1, 2), "AAPL", "Tech", Decimal("10"), Decimal("90"))

    with pytest.raises(ValueError, match="Duplicate lot IDs"):
        ledger.sell(
            date(2024, 1, 5),
            "AAPL",
            Decimal("15"),
            Decimal("95"),
            policy="specific_lot",
            specific_lot_ids=[lot.lot_id, lot.lot_id],
        )

    assert ledger.position_quantity("AAPL") == Decimal("10")
    assert ledger.closed_lots == []


def test_corporate_actions_split_spinoff_rename_and_dividend() -> None:
    ledger = LotLedger(initial_cash=Decimal("10000"))
    lot, _ = ledger.buy(date(2023, 1, 10), "GE", "Industrials", Decimal("10"), Decimal("100"))
    assert lot.unit_cost == Decimal("100")

    # 4-for-1 split -> 40 shares, total basis unchanged ($1,000), unit cost $25
    ledger.apply_corporate_action(
        CorporateAction(
            action_id="ca_split",
            effective_date=date(2023, 4, 1),
            action_type="split",
            ticker="GE",
            ratio_numerator=Decimal("4"),
            ratio_denominator=Decimal("1"),
        )
    )
    split_lot = ledger.get_lot(lot.lot_id)
    assert split_lot is not None
    assert split_lot.quantity == Decimal("40")
    assert split_lot.cost_basis_total == Decimal("1000")
    assert split_lot.unit_cost == Decimal("25")

    # Spin-off: 1 GEHC share per 4 GE shares, 20% basis allocation -> 10 GEHC shares w/ $200 basis
    ledger.apply_corporate_action(
        CorporateAction(
            action_id="ca_spin",
            effective_date=date(2023, 6, 1),
            action_type="spin_off",
            ticker="GE",
            target_ticker="GEHC",
            target_sector="Health Care",
            ratio_numerator=Decimal("1"),
            ratio_denominator=Decimal("4"),
            basis_allocation_fraction=Decimal("0.20"),
        )
    )
    assert ledger.position_quantity("GEHC") == Decimal("10")
    assert ledger.position_cost_basis("GEHC") == Decimal("200")
    assert ledger.position_cost_basis("GE") == Decimal("800")
    # Spun-off shares inherit parent acquired_date
    gehc_lot = ledger.open_lots("GEHC")[0]
    assert gehc_lot.acquired_date == date(2023, 1, 10)

    # Ticker rename GE -> GEV
    ledger.apply_corporate_action(
        CorporateAction(
            action_id="ca_rename",
            effective_date=date(2023, 9, 1),
            action_type="ticker_change",
            ticker="GE",
            target_ticker="GEV",
        )
    )
    assert ledger.position_quantity("GE") == Decimal("0")
    assert ledger.position_quantity("GEV") == Decimal("40")

    # Cash dividend on GEV: $0.50/sh across 40 shares = $20 (80% qualified)
    cash_before = ledger.cash
    receipt = ledger.apply_corporate_action(
        CorporateAction(
            action_id="ca_div",
            effective_date=date(2023, 12, 15),
            action_type="dividend",
            ticker="GEV",
            cash_amount_per_share=Decimal("0.50"),
            qualified_fraction=Decimal("0.80"),
        )
    )
    assert receipt is not None
    assert receipt.gross_amount == Decimal("20")
    assert receipt.qualified_amount == Decimal("16")
    assert receipt.ordinary_amount == Decimal("4")
    assert ledger.cash == cash_before + Decimal("20")


def test_insufficient_shares_or_cash_raises() -> None:
    ledger = LotLedger(initial_cash=Decimal("100"))
    with pytest.raises(ValueError, match="Insufficient cash"):
        ledger.buy(date(2024, 1, 2), "AAPL", "Tech", Decimal("2"), Decimal("100"))

    ledger.buy(date(2024, 1, 2), "AAPL", "Tech", Decimal("1"), Decimal("90"))
    with pytest.raises(ValueError, match="Cannot sell"):
        ledger.sell(date(2024, 1, 5), "AAPL", Decimal("2"), Decimal("95"))
