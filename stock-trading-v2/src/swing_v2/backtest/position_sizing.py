"""Pure entry-position sizing helpers."""

from decimal import Decimal, ROUND_FLOOR

from .engine import BPS_DENOMINATOR, ExecutionCostConfig, Side


def _require_finite(value: Decimal, name: str) -> None:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


def _require_nonnegative(value: Decimal, name: str) -> None:
    _require_finite(value, name)
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")


def _require_unit_interval(value: Decimal, name: str) -> None:
    _require_finite(value, name)
    if not Decimal("0") <= value <= Decimal("1"):
        raise ValueError(f"{name} must be between zero and one")


def validate_entry_sizing_inputs(
    *,
    nav: Decimal,
    available_cash: Decimal,
    costs: ExecutionCostConfig,
    risk_per_position: Decimal,
    max_position_notional_pct: Decimal,
    initial_stop_pct: Decimal,
) -> None:
    """Validate the portfolio-level input contract shared by entry sizing and planning."""
    _require_nonnegative(nav, "nav")
    _require_nonnegative(available_cash, "available_cash")
    _require_unit_interval(risk_per_position, "risk_per_position")
    _require_unit_interval(max_position_notional_pct, "max_position_notional_pct")
    _require_finite(initial_stop_pct, "initial_stop_pct")
    if not Decimal("0") < initial_stop_pct < Decimal("1"):
        raise ValueError("initial_stop_pct must be between zero and one, exclusive")
    _require_nonnegative(costs.buy_slippage_bps, "buy_slippage_bps")
    _require_nonnegative(costs.buy_commission_bps, "buy_commission_bps")
    _require_nonnegative(costs.fixed_fee_per_order, "fixed_fee_per_order")


def calculate_tick_rounded_buy_fill_price(
    expected_open_price: Decimal, costs: ExecutionCostConfig
) -> Decimal:
    """Return a validated slipped, tick-rounded BUY fill price."""
    _require_finite(expected_open_price, "expected_open_price")
    if expected_open_price <= 0:
        raise ValueError("expected_open_price must be positive")
    fill_price = costs.tick_rounder(
        expected_open_price * (Decimal("1") + costs.buy_slippage_bps / BPS_DENOMINATOR),
        Side.BUY,
    )
    _require_finite(fill_price, "tick_rounder return value")
    if fill_price <= 0:
        raise ValueError("tick_rounder return value must be positive")
    return fill_price


def calculate_entry_quantity(
    *,
    expected_open_price: Decimal,
    nav: Decimal,
    available_cash: Decimal,
    costs: ExecutionCostConfig,
    asset_type: str,
    risk_per_position: Decimal,
    max_position_notional_pct: Decimal,
    initial_stop_pct: Decimal,
) -> int:
    """Return the largest whole-share BUY quantity permitted by all limits.

    ``asset_type`` is accepted for the portfolio-sizing API and future
    asset-specific buy costs; the injected v0 cost configuration applies no
    buy tax.  The order-level fixed fee is deducted once before deriving the
    cash limit, rather than being incorrectly multiplied by the quantity.
    """
    validate_entry_sizing_inputs(
        nav=nav,
        available_cash=available_cash,
        costs=costs,
        risk_per_position=risk_per_position,
        max_position_notional_pct=max_position_notional_pct,
        initial_stop_pct=initial_stop_pct,
    )
    fill_price = calculate_tick_rounded_buy_fill_price(expected_open_price, costs)
    return calculate_entry_quantity_for_fill_price(
        fill_price=fill_price,
        nav=nav,
        available_cash=available_cash,
        costs=costs,
        asset_type=asset_type,
        risk_per_position=risk_per_position,
        max_position_notional_pct=max_position_notional_pct,
        initial_stop_pct=initial_stop_pct,
    )


def calculate_entry_quantity_for_fill_price(
    *,
    fill_price: Decimal,
    nav: Decimal,
    available_cash: Decimal,
    costs: ExecutionCostConfig,
    asset_type: str,
    risk_per_position: Decimal,
    max_position_notional_pct: Decimal,
    initial_stop_pct: Decimal,
) -> int:
    """Return the largest whole-share BUY quantity for a validated fill price.

    Callers that already calculated a slipped, tick-rounded fill can use this
    helper to keep sizing and planned cash costs based on that exact price.
    """
    del asset_type
    validate_entry_sizing_inputs(
        nav=nav,
        available_cash=available_cash,
        costs=costs,
        risk_per_position=risk_per_position,
        max_position_notional_pct=max_position_notional_pct,
        initial_stop_pct=initial_stop_pct,
    )
    _require_finite(fill_price, "fill_price")
    if fill_price <= 0:
        raise ValueError("fill_price must be positive")
    initial_stop_price = fill_price * (Decimal("1") - initial_stop_pct)
    risk_per_share = fill_price - initial_stop_price
    risk_quantity = int(
        (nav * risk_per_position / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)
    )
    notional_quantity = int(
        (nav * max_position_notional_pct / fill_price).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    cash_quantity = int(
        ((available_cash - costs.fixed_fee_per_order) /
         (fill_price + fill_price * costs.buy_commission_bps / BPS_DENOMINATOR))
        .to_integral_value(rounding=ROUND_FLOOR)
    )
    return max(0, min(risk_quantity, notional_quantity, cash_quantity))
