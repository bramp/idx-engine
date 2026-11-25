"""Unit tests for annual ST/LT taxes, external capital gains, $3k cap, carryforwards, and liquidation."""

from datetime import date
from decimal import Decimal

from idx_engine.config import TaxConfig
from idx_engine.ledger.lots import LotLedger
from idx_engine.ledger.taxes import TaxAccountant


def test_st_and_lt_loss_offset_cap_and_carryforward_priority() -> None:
    cfg = TaxConfig(
        fed_st_rate=Decimal("0.35"),
        fed_lt_rate=Decimal("0.20"),
        niit_rate=Decimal("0.00"),
        state_st_rate=Decimal("0.05"),
        state_lt_rate=Decimal("0.05"),
        ordinary_income_offset_cap=Decimal("3000"),
    )
    # Combined ST = 40%, Combined LT = 25%, Ordinary offset rate = 40%
    ledger = LotLedger(initial_cash=Decimal("100000"))
    accountant = TaxAccountant(cfg)

    # Realize -$2,000 ST loss and -$5,000 LT loss in 2023
    ledger.buy(date(2021, 1, 1), "AAPL", "Tech", Decimal("100"), Decimal("200"))
    ledger.buy(date(2023, 2, 1), "MSFT", "Tech", Decimal("100"), Decimal("300"))
    ledger.sell(date(2023, 6, 1), "AAPL", Decimal("100"), Decimal("150"))  # -$5,000 LT
    ledger.sell(date(2023, 6, 1), "MSFT", Decimal("100"), Decimal("280"))  # -$2,000 ST

    summary_2023 = accountant.settle_tax_year(2023, ledger.closed_lots, ledger.dividends)
    # $3,000 ordinary cap uses ALL $2,000 ST loss first + $1,000 of LT loss -> $4,000 LT carryforward!
    assert summary_2023.ordinary_income_offset_used == Decimal("3000")
    assert summary_2023.st_loss_carryforward_out == Decimal("0")
    assert summary_2023.lt_loss_carryforward_out == Decimal("4000")
    # $3,000 * 40% ordinary tax saved = -$1,200 net tax bill
    assert summary_2023.net_portfolio_tax_bill == Decimal("-1200.00")

    # Next year (2024): realize +$6,000 LT gain -> offset by $4,000 LT carryforward -> $2,000 taxed @ 25% = $500
    ledger.buy(date(2022, 1, 1), "NVDA", "Tech", Decimal("50"), Decimal("100"))
    ledger.sell(date(2024, 3, 1), "NVDA", Decimal("50"), Decimal("220"))  # +$6,000 LT
    summary_2024 = accountant.settle_tax_year(2024, ledger.closed_lots, ledger.dividends)
    assert summary_2024.net_portfolio_tax_bill == Decimal("500.00")
    assert summary_2024.lt_loss_carryforward_out == Decimal("0")


def test_external_capital_gains_offset_and_hypothetical_liquidation() -> None:
    cfg = TaxConfig(
        fed_st_rate=Decimal("0.37"),
        fed_lt_rate=Decimal("0.20"),
        niit_rate=Decimal("0.038"),
        state_st_rate=Decimal("0.093"),
        state_lt_rate=Decimal("0.093"),
        ordinary_income_offset_cap=Decimal("3000"),
        external_st_gains_annual=Decimal("10000"),
        external_lt_gains_annual=Decimal("0"),
    )
    # Combined ST = 0.501 (50.1%), Combined LT = 0.331 (33.1%)
    ledger = LotLedger(initial_cash=Decimal("100000"))
    accountant = TaxAccountant(cfg)

    # Harvest -$10,000 ST loss in 2023, replacing with a sector peer that appreciates +$10,000 LT by 2025
    ledger.buy(date(2023, 1, 5), "KO", "Staples", Decimal("200"), Decimal("250"))  # $50k
    ledger.sell(date(2023, 6, 5), "KO", Decimal("200"), Decimal("200"))  # -$10k ST
    ledger.buy(date(2023, 6, 5), "PEP", "Staples", Decimal("200"), Decimal("200"))  # $40k

    s2023 = accountant.settle_tax_year(2023, ledger.closed_lots, ledger.dividends)
    # Offsets $10,000 external ST gains taxed at 50.1% -> saves $5,010 in 2023!
    assert s2023.tax_liability_external_only == Decimal("5010.00")
    assert s2023.tax_liability_with_portfolio == Decimal("0.00")
    assert s2023.net_portfolio_tax_bill == Decimal("-5010.00")

    # End-of-horizon liquidation on 2025-07-01 when PEP is at $250 (+$10k unrealized LT gain @ 33.1% = $3,310)
    liq = accountant.compute_hypothetical_liquidation(
        ledger,
        liquidation_date=date(2025, 7, 1),
        prices={"PEP": Decimal("250")},
    )
    assert liq.unrealized_lt_net == Decimal("10000.00")
    assert liq.liquidation_tax_due == Decimal("3310.00")
    # Net permanent ST->LT rate-arbitrage + deferral tax savings even after full liquidation!
    assert -(s2023.net_portfolio_tax_bill) > liq.liquidation_tax_due


def test_terminal_liquidation_is_symmetric_on_same_year_losses_and_external_gains() -> None:
    cfg = TaxConfig(
        fed_st_rate=Decimal("0.37"),
        fed_lt_rate=Decimal("0.20"),
        niit_rate=Decimal("0.038"),
        state_st_rate=Decimal("0.093"),
        state_lt_rate=Decimal("0.093"),
        ordinary_income_offset_cap=Decimal("3000"),
        external_st_gains_annual=Decimal("10000"),
        external_lt_gains_annual=Decimal("25000"),
    )
    # Portfolio A harvests -$50k mid-year into a flat substitute; Portfolio B holds -$50k unrealized
    ledger_a = LotLedger(initial_cash=Decimal("500000"))
    tax_a = TaxAccountant(cfg)
    ledger_a.buy(date(2024, 1, 2), "AAPL", "Tech", Decimal("5000"), Decimal("100"))
    ledger_a.sell(date(2024, 6, 3), "AAPL", Decimal("5000"), Decimal("90"))  # -$50k realized ST
    ledger_a.buy(date(2024, 6, 3), "MSFT", "Tech", Decimal("4500"), Decimal("100"))

    ledger_b = LotLedger(initial_cash=Decimal("500000"))
    tax_b = TaxAccountant(cfg)
    ledger_b.buy(date(2024, 1, 2), "VOO", "ETF", Decimal("5000"), Decimal("100"))

    s_a = tax_a.settle_tax_year(2024, ledger_a.closed_lots, ledger_a.dividends)
    s_b = tax_b.settle_tax_year(2024, ledger_b.closed_lots, ledger_b.dividends)

    liq_a = tax_a.compute_hypothetical_liquidation(
        ledger_a, date(2024, 12, 31), {"MSFT": Decimal("100")}
    )
    liq_b = tax_b.compute_hypothetical_liquidation(
        ledger_b, date(2024, 12, 31), {"VOO": Decimal("90")}
    )

    total_tax_a = s_a.net_portfolio_tax_bill + liq_a.liquidation_tax_due
    total_tax_b = s_b.net_portfolio_tax_bill + liq_b.liquidation_tax_due
    assert total_tax_a == total_tax_b
    assert liq_a.total_remaining_carryforward == liq_b.total_remaining_carryforward
