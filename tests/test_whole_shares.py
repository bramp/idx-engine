"""Unit tests for fractional vs. whole-share sizing and stratified sector sampling."""

from decimal import Decimal

import pytest

from idx_engine.strategy.direct_index import select_stratified_weights
from idx_engine.strategy.sizing import compute_target_shares


def test_whole_share_floor_plus_largest_remainder_never_exceeds_budget() -> None:
    budget = Decimal("1000")
    weights = {
        "AAPL": Decimal("0.40"),  # $400 @ $180 -> 2 sh ($360), rem $40
        "MSFT": Decimal("0.35"),  # $350 @ $300 -> 1 sh ($300), rem $50
        "KO": Decimal("0.25"),  # $250 @ $60  -> 4 sh ($240), rem $10
    }
    prices = {
        "AAPL": Decimal("180"),
        "MSFT": Decimal("300"),
        "KO": Decimal("60"),
    }
    # Floor spends $360 + $300 + $240 = $900, leaving $100 cash.
    # Remainders: MSFT ($50, $300 > $100), AAPL ($40, $180 > $100), KO ($10, $60 <= $100 -> +1 KO!)
    whole = compute_target_shares(budget, weights, prices, allow_fractional=False)
    assert whole == {"AAPL": Decimal("2"), "MSFT": Decimal("1"), "KO": Decimal("5")}
    assert all(q == int(q) for q in whole.values())
    spent = sum(whole[t] * prices[t] for t in whole)
    assert spent == Decimal("960")
    assert spent <= budget

    # Fractional sizing invests nearly 100% of budget
    frac = compute_target_shares(budget, weights, prices, allow_fractional=True)
    frac_spent = sum(frac[t] * prices[t] for t in frac)
    assert float(frac_spent) == pytest.approx(1000.0, abs=0.01)


def test_stratified_sampling_preserves_exact_sector_weights() -> None:
    weights = {
        "AAPL": Decimal("0.30"),
        "MSFT": Decimal("0.20"),
        "NVDA": Decimal("0.10"),  # Tech total = 0.60
        "JPM": Decimal("0.25"),
        "BAC": Decimal("0.15"),  # Financials total = 0.40
    }
    sectors = {
        "AAPL": "Tech",
        "MSFT": "Tech",
        "NVDA": "Tech",
        "JPM": "Financials",
        "BAC": "Financials",
    }
    prices = {t: Decimal("100") for t in weights}

    sampled = select_stratified_weights(weights, sectors, prices, sample_size=2)
    assert set(sampled.keys()) == {"AAPL", "JPM"}
    assert float(sampled["AAPL"]) == pytest.approx(0.60)
    assert float(sampled["JPM"]) == pytest.approx(0.40)
