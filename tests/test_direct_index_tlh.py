"""Unit tests for DirectIndexStrategy TLH trigger, sector substitute routing, and lockout safety."""

from datetime import date, timedelta
from decimal import Decimal

from idx_engine.config import StrategyConfig
from idx_engine.ledger.lots import LotLedger
from idx_engine.ledger.wash_sale import WashSaleResolver
from idx_engine.strategy.base import DayMarketState
from idx_engine.strategy.direct_index import DirectIndexStrategy


def test_direct_index_harvests_loss_into_sector_peer_and_avoids_wash_sale_on_rebalance() -> None:
    cfg = StrategyConfig(
        name="tlh_test",
        allow_fractional=True,
        tlh_enabled=True,
        tlh_loss_pct_threshold=Decimal("0.05"),
        tlh_min_loss_dollars=Decimal("200"),
        tlh_check_frequency_days=1,
        substitute_mode="sector_peer",
        rebalance_mode="monthly",  # day_idx=21 triggers rebalance WHILE still in 30-day lockout!
    )
    strat = DirectIndexStrategy(cfg)
    resolver = WashSaleResolver()
    ledger = LotLedger(initial_cash=Decimal("10000"), default_policy="HIFO")
    resolver.attach(ledger)

    sectors = {"KO": "Staples", "PEP": "Staples", "AAPL": "Tech"}
    weights = {"KO": Decimal("0.50"), "AAPL": Decimal("0.50")}  # PEP is same-sector peer

    # Day 0: Buy $5,000 KO @ $100 (50 sh) and $5,000 AAPL @ $100 (50 sh)
    d0 = date(2024, 1, 2)
    strat.on_day(
        DayMarketState(
            day_idx=0,
            current_date=d0,
            prices={"KO": Decimal("100"), "PEP": Decimal("100"), "AAPL": Decimal("100")},
            dividends={},
            index_weights=weights,
            sectors=sectors,
            etf_ticker="VOO",
            etf_price=Decimal("400"),
            etf_dividend=Decimal("0"),
        ),
        ledger,
        resolver,
    )
    assert ledger.position_quantity("KO") == Decimal("50")
    assert ledger.position_quantity("AAPL") == Decimal("50")

    # Day 10: KO drops 10% to $90 (-$500 loss >= $200 min and 10% >= 5% trigger) -> harvest into PEP!
    d10 = d0 + timedelta(days=14)
    strat.on_day(
        DayMarketState(
            day_idx=10,
            current_date=d10,
            prices={"KO": Decimal("90"), "PEP": Decimal("95"), "AAPL": Decimal("102")},
            dividends={},
            index_weights={"KO": Decimal("0.45"), "PEP": Decimal("0.05"), "AAPL": Decimal("0.50")},
            sectors=sectors,
            etf_ticker="VOO",
            etf_price=Decimal("401"),
            etf_dividend=Decimal("0"),
        ),
        ledger,
        resolver,
    )
    assert ledger.position_quantity("KO") == Decimal("0")
    assert ledger.position_quantity("PEP") > Decimal("0")
    assert len(ledger.closed_lots) == 1
    assert ledger.closed_lots[0].raw_realized_gain_loss == Decimal("-500")
    assert ledger.closed_lots[0].disallowed_wash_loss == Decimal("0")

    # Day 21 (15 calendar days after d10, STILL inside 30-day lockout of KO!):
    # Monthly rebalance runs, MUST keep weight in PEP and NOT buy back KO!
    d21 = d10 + timedelta(days=15)
    assert resolver.is_locked_out("KO", d21) is True
    strat.on_day(
        DayMarketState(
            day_idx=21,
            current_date=d21,
            prices={"KO": Decimal("92"), "PEP": Decimal("97"), "AAPL": Decimal("105")},
            dividends={},
            index_weights=weights,
            sectors=sectors,
            etf_ticker="VOO",
            etf_price=Decimal("405"),
            etf_dividend=Decimal("0"),
        ),
        ledger,
        resolver,
    )
    assert ledger.position_quantity("KO") == Decimal("0")
    assert len(ledger.wash_sale_adjustments) == 0

    # Day 42 (35 calendar days after d10, lockout EXPIRED!):
    # Monthly rebalance rotates substitute PEP back into target constituent KO cleanly!
    d42 = d10 + timedelta(days=35)
    assert resolver.is_locked_out("KO", d42) is False
    strat.on_day(
        DayMarketState(
            day_idx=42,
            current_date=d42,
            prices={"KO": Decimal("93"), "PEP": Decimal("98"), "AAPL": Decimal("106")},
            dividends={},
            index_weights=weights,
            sectors=sectors,
            etf_ticker="VOO",
            etf_price=Decimal("408"),
            etf_dividend=Decimal("0"),
        ),
        ledger,
        resolver,
    )
    assert ledger.position_quantity("KO") > Decimal("0")
    assert len(ledger.wash_sale_adjustments) == 0


def test_simultaneous_dual_class_tlh_handles_live_wash_split_without_stale_qty() -> None:
    cfg = StrategyConfig(
        name="dual_class_tlh",
        allow_fractional=True,
        tlh_enabled=True,
        tlh_loss_pct_threshold=Decimal("0.05"),
        tlh_min_loss_dollars=Decimal("100"),
        tlh_check_frequency_days=1,
        substantially_identical_groups=[["GOOG", "GOOGL"]],
    )
    strat = DirectIndexStrategy(cfg)
    resolver = WashSaleResolver(substantially_identical_groups=[["GOOG", "GOOGL"]])
    ledger = LotLedger(initial_cash=Decimal("30000"), default_policy="HIFO")
    resolver.attach(ledger)

    # Hold older GOOG lot (50 sh @ $100) + recent larger GOOGL lot (100 sh @ $100, 10 days later)
    ledger.buy(date(2024, 1, 2), "GOOG", "Comm", Decimal("50"), Decimal("100"))
    ledger.buy(date(2024, 2, 10), "GOOGL", "Comm", Decimal("100"), Decimal("100"))

    # Both drop to $85 on Feb 20 (within 30d of GOOGL buy); selling GOOG splits GOOGL lot live!
    strat.on_day(
        DayMarketState(
            day_idx=5,
            current_date=date(2024, 2, 20),
            prices={"GOOG": Decimal("85"), "GOOGL": Decimal("85"), "META": Decimal("100")},
            dividends={},
            index_weights={
                "GOOG": Decimal("0.3"),
                "GOOGL": Decimal("0.3"),
                "META": Decimal("0.4"),
            },
            sectors={"GOOG": "Comm", "GOOGL": "Comm", "META": "Comm"},
            etf_ticker="VOO",
            etf_price=Decimal("400"),
            etf_dividend=Decimal("0"),
        ),
        ledger,
        resolver,
    )
    assert ledger.position_quantity("GOOG") == Decimal("0")
    assert ledger.position_quantity("GOOGL") == Decimal("0")
    assert ledger.position_quantity("META") > Decimal("0")
    assert all(c.disallowed_wash_loss == Decimal("0") for c in ledger.closed_lots)


def test_tlh_does_not_sell_ineligible_wash_split_sibling() -> None:
    cfg = StrategyConfig(
        name="split_lot_tlh",
        allow_fractional=True,
        tlh_enabled=True,
        tlh_loss_pct_threshold=Decimal("0.05"),
        tlh_min_loss_dollars=Decimal("75"),
        tlh_check_frequency_days=1,
        substitute_mode="cash",
        rebalance_mode="none",
    )
    strat = DirectIndexStrategy(cfg)
    resolver = WashSaleResolver()
    ledger = LotLedger(initial_cash=Decimal("1500"), default_policy="HIFO")
    resolver.attach(ledger)

    original_lot, _ = ledger.buy(date(2024, 1, 1), "XYZ", "Tech", Decimal("5"), Decimal("100"))
    ledger.buy(date(2024, 1, 10), "XYZ", "Tech", Decimal("10"), Decimal("100"))
    ledger.sell(
        trade_date=date(2024, 1, 15),
        ticker="XYZ",
        quantity=Decimal("5"),
        price=Decimal("80"),
        policy="specific_lot",
        specific_lot_id=original_lot.lot_id,
    )

    split_lots = ledger.open_lots("XYZ")
    eligible_lot = next(lot for lot in split_lots if lot.wash_sale_disallowed > Decimal("0"))
    ineligible_lot = next(lot for lot in split_lots if lot.lot_id != eligible_lot.lot_id)
    assert ledger.root_lot_id(eligible_lot.lot_id) == ledger.root_lot_id(ineligible_lot.lot_id)
    assert eligible_lot.cost_basis_total == Decimal("600")
    assert ineligible_lot.cost_basis_total == Decimal("500")

    strat._run_tlh_scan(
        DayMarketState(
            day_idx=1,
            current_date=date(2024, 2, 15),
            prices={"XYZ": Decimal("105")},
            dividends={},
            index_weights={"XYZ": Decimal("1")},
            sectors={"XYZ": "Tech"},
            etf_ticker="VOO",
            etf_price=Decimal("400"),
            etf_dividend=Decimal("0"),
        ),
        ledger,
        resolver,
    )

    remaining_lots = ledger.open_lots("XYZ")
    assert [lot.lot_id for lot in remaining_lots] == [ineligible_lot.lot_id]
    tlh_closes = [closed for closed in ledger.closed_lots if closed.reason == "tlh_harvest"]
    assert len(tlh_closes) == 1
    assert tlh_closes[0].source_lot_id == eligible_lot.lot_id
    assert tlh_closes[0].raw_realized_gain_loss == Decimal("-75")


def test_substitute_mode_cash_retains_harvested_cash_during_30d_lockout_and_sector_etf_routes() -> (
    None
):
    # 1. substitute_mode="cash" keeps harvested proceeds in cash during 30-day lockout
    cash_cfg = StrategyConfig(
        name="cash_sub_test",
        allow_fractional=True,
        tlh_enabled=True,
        tlh_loss_pct_threshold=Decimal("0.05"),
        tlh_min_loss_dollars=Decimal("100"),
        tlh_check_frequency_days=1,
        substitute_mode="cash",
        rebalance_mode="none",
    )
    strat_cash = DirectIndexStrategy(cash_cfg)
    res_cash = WashSaleResolver()
    led_cash = LotLedger(initial_cash=Decimal("10000"))
    res_cash.attach(led_cash)

    d0 = date(2024, 1, 2)
    strat_cash.on_day(
        DayMarketState(
            day_idx=0,
            current_date=d0,
            prices={"KO": Decimal("100"), "AAPL": Decimal("100")},
            dividends={},
            index_weights={"KO": Decimal("0.5"), "AAPL": Decimal("0.5")},
            sectors={"KO": "Consumer Staples", "AAPL": "Information Technology"},
            etf_ticker="VOO",
            etf_price=Decimal("400"),
            etf_dividend=Decimal("0"),
        ),
        led_cash,
        res_cash,
    )
    # Day 5: KO drops to $90 -> harvested into $4,500 cash; AAPL must NOT absorb the $4,500!
    strat_cash.on_day(
        DayMarketState(
            day_idx=5,
            current_date=d0 + timedelta(days=7),
            prices={"KO": Decimal("90"), "AAPL": Decimal("100")},
            dividends={},
            index_weights={"KO": Decimal("0.5"), "AAPL": Decimal("0.5")},
            sectors={"KO": "Consumer Staples", "AAPL": "Information Technology"},
            etf_ticker="VOO",
            etf_price=Decimal("395"),
            etf_dividend=Decimal("0"),
        ),
        led_cash,
        res_cash,
    )
    assert led_cash.position_quantity("KO") == Decimal("0")
    assert led_cash.cash >= Decimal("4450")

    # 2. substitute_mode="sector_etf" routes Consumer Staples into XLP when priced
    etf_cfg = cash_cfg.model_copy(update={"substitute_mode": "sector_etf"})
    strat_etf = DirectIndexStrategy(etf_cfg)
    res_etf = WashSaleResolver()
    led_etf = LotLedger(initial_cash=Decimal("10000"))
    res_etf.attach(led_etf)

    strat_etf.on_day(
        DayMarketState(
            day_idx=0,
            current_date=d0,
            prices={"KO": Decimal("100"), "AAPL": Decimal("100"), "XLP": Decimal("75")},
            dividends={},
            index_weights={"KO": Decimal("0.5"), "AAPL": Decimal("0.5"), "XLP": Decimal("0")},
            sectors={
                "KO": "Consumer Staples",
                "AAPL": "Information Technology",
                "XLP": "Consumer Staples",
            },
            etf_ticker="VOO",
            etf_price=Decimal("400"),
            etf_dividend=Decimal("0"),
        ),
        led_etf,
        res_etf,
    )
    strat_etf.on_day(
        DayMarketState(
            day_idx=5,
            current_date=d0 + timedelta(days=7),
            prices={"KO": Decimal("90"), "AAPL": Decimal("100"), "XLP": Decimal("74")},
            dividends={},
            index_weights={"KO": Decimal("0.5"), "AAPL": Decimal("0.5"), "XLP": Decimal("0")},
            sectors={
                "KO": "Consumer Staples",
                "AAPL": "Information Technology",
                "XLP": "Consumer Staples",
            },
            etf_ticker="VOO",
            etf_price=Decimal("395"),
            etf_dividend=Decimal("0"),
        ),
        led_etf,
        res_etf,
    )
    assert led_etf.position_quantity("KO") == Decimal("0")
    assert led_etf.position_quantity("XLP") > Decimal("0")
