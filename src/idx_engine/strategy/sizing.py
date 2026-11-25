"""Fractional vs. integer whole-share sizing and underweight cash allocation."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal

from idx_engine.models import ZERO

FRAC_QTY_QUANT = Decimal("0.000001")


def compute_target_shares(
    total_budget: Decimal,
    target_weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    allow_fractional: bool = True,
) -> dict[str, Decimal]:
    """Convert normalized `target_weights` and `total_budget` into target share counts.

    - When `allow_fractional=True`, allocates fractional shares quantized down to 1e-6 shares.
    - When `allow_fractional=False`, computes `floor` integer shares first, then allocates
      remaining cash via greedy largest-dollar-remainder order so whole-share rounding
      minimizes active weight error without exceeding `total_budget`.
    """
    if total_budget <= ZERO:
        return {}

    valid = {t: w for t, w in target_weights.items() if w > ZERO and prices.get(t, ZERO) > ZERO}
    if not valid:
        return {}

    w_sum = sum(valid.values(), ZERO)
    norm_weights = {t: (w / w_sum) if w_sum > Decimal("1") else w for t, w in valid.items()}
    effective_w_sum = min(Decimal("1"), w_sum)

    if allow_fractional:
        shares: dict[str, Decimal] = {}
        for ticker, weight in norm_weights.items():
            px = prices[ticker]
            qty = (total_budget * weight / px).quantize(FRAC_QTY_QUANT, rounding=ROUND_DOWN)
            if qty > ZERO:
                shares[ticker] = qty
        return shares

    # Whole shares: floor pass + greedy largest-dollar-remainder pass within effective_w_sum budget
    floor_shares: dict[str, Decimal] = {}
    remainders: list[tuple[Decimal, Decimal, str]] = []
    spent = ZERO

    for ticker, weight in norm_weights.items():
        px = prices[ticker]
        target_dollars = total_budget * weight
        whole = Decimal(int(target_dollars // px))
        cost = whole * px
        rem_dollars = target_dollars - cost
        floor_shares[ticker] = whole
        spent += cost
        remainders.append((rem_dollars, weight, ticker))

    remaining_cash = max(ZERO, (total_budget * effective_w_sum) - spent)
    # Sort descending by dollar shortfall, then weight, then alphabetical ticker
    remainders.sort(key=lambda item: (-item[0], -item[1], item[2]))

    for _, _, ticker in remainders:
        px = prices[ticker]
        if px <= remaining_cash:
            floor_shares[ticker] = floor_shares.get(ticker, ZERO) + Decimal("1")
            remaining_cash -= px

    return {t: q for t, q in floor_shares.items() if q > ZERO}


def compute_cash_inflow_buys(
    available_cash: Decimal,
    current_quantities: Mapping[str, Decimal],
    target_weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    allow_fractional: bool = True,
) -> dict[str, Decimal]:
    """Allocate `available_cash` strictly to BUY orders on the most dollar-underweight
    tickers in `target_weights`, avoiding any unnecessary sales/realized gains.
    """
    if available_cash <= ZERO:
        return {}

    valid_targets = {
        t: w for t, w in target_weights.items() if w > ZERO and prices.get(t, ZERO) > ZERO
    }
    if not valid_targets:
        return {}

    current_mv = sum(
        (qty * prices.get(t, ZERO) for t, qty in current_quantities.items()),
        ZERO,
    )
    post_equity = current_mv + available_cash

    deficits: dict[str, Decimal] = {}
    for ticker, w in valid_targets.items():
        cur_val = current_quantities.get(ticker, ZERO) * prices[ticker]
        desired_val = post_equity * w
        if desired_val > cur_val:
            deficits[ticker] = desired_val - cur_val

    deficit_sum = sum(deficits.values(), ZERO)
    if deficit_sum <= ZERO:
        buy_weights = valid_targets
    else:
        buy_weights = {t: d / deficit_sum for t, d in deficits.items()}

    return compute_target_shares(
        total_budget=available_cash,
        target_weights=buy_weights,
        prices=prices,
        allow_fractional=allow_fractional,
    )
