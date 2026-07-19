"""Immutable multi-position portfolio state and execution-ledger composition."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from .engine import Fill, Order, Position, RunResult, Side


@dataclass(frozen=True)
class PortfolioState:
    """A validated immutable portfolio snapshot and its append-only execution ledger."""

    cash: Decimal
    positions: tuple[Position, ...]
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]

    def __post_init__(self) -> None:
        _validate_state(self)


def create_portfolio_state(initial_cash: Decimal) -> PortfolioState:
    """Create an empty portfolio with a non-negative finite Decimal cash balance."""
    return PortfolioState(cash=initial_cash, positions=(), orders=(), fills=())


def apply_entry_execution(state: PortfolioState, entry_run_result: RunResult) -> PortfolioState:
    """Append one entry IOC result to a portfolio after validating its continuity."""
    _validate_state_value(state)
    result = _validate_run_result(entry_run_result)
    _validate_new_ledger_ids(state, result)
    _validate_execution_ledger(result)
    _validate_entry_ledger(result)
    _validate_cash_continuity(state.cash, result)

    positions = _validate_entry_positions(state, result)
    return PortfolioState(
        cash=result.cash,
        positions=state.positions + positions,
        orders=state.orders + result.orders,
        fills=state.fills + result.fills,
    )


def apply_exit_execution(state: PortfolioState, exit_run_result: RunResult) -> PortfolioState:
    """Replace a complete existing-position snapshot and append its exit ledger."""
    _validate_state_value(state)
    result = _validate_run_result(exit_run_result)
    _validate_new_ledger_ids(state, result)
    _validate_execution_ledger(result)
    _validate_exit_ledger(result)
    _validate_cash_continuity(state.cash, result)
    _validate_exit_snapshot(state.positions, result)

    return PortfolioState(
        cash=result.cash,
        positions=result.positions,
        orders=state.orders + result.orders,
        fills=state.fills + result.fills,
    )


def _validate_state_value(state: PortfolioState) -> None:
    if not isinstance(state, PortfolioState):
        raise ValueError("state must be a PortfolioState")
    _validate_state(state)


def _validate_state(state: PortfolioState) -> None:
    _validate_cash(state.cash, "cash")
    _validate_items(state.positions, Position, "positions")
    _validate_items(state.orders, Order, "orders")
    _validate_items(state.fills, Fill, "fills")
    _validate_unique_ids(state.positions, "position_id", "positions")
    _validate_unique_ids(state.orders, "order_id", "orders")
    _validate_unique_ids(state.fills, "fill_id", "fills")
    open_symbols = tuple(position.symbol for position in state.positions if position.status == "OPEN")
    if len(open_symbols) != len(set(open_symbols)):
        raise ValueError("OPEN positions must have unique symbols")


def _validate_run_result(result: RunResult) -> RunResult:
    if not isinstance(result, RunResult):
        raise ValueError("execution result must be a RunResult")
    _validate_cash(result.cash, "execution result cash")
    _validate_items(result.positions, Position, "execution result positions")
    _validate_items(result.orders, Order, "execution result orders")
    _validate_items(result.fills, Fill, "execution result fills")
    _validate_unique_ids(result.positions, "position_id", "execution result positions")
    _validate_unique_ids(result.orders, "order_id", "execution result orders")
    _validate_unique_ids(result.fills, "fill_id", "execution result fills")
    return result


def _validate_execution_ledger(result: RunResult) -> None:
    """Require a one-to-one, internally consistent new order/fill ledger."""
    fills_by_order: dict[str, list[Fill]] = {}
    for fill in result.fills:
        fills_by_order.setdefault(fill.order_id, []).append(fill)

    for order in result.orders:
        if order.status not in {"FILLED", "CANCELED_UNFILLED"}:
            raise ValueError("execution result orders must be FILLED or CANCELED_UNFILLED")
        if order.side not in {Side.BUY, Side.SELL}:
            raise ValueError("execution result order side must be BUY or SELL")
        if not isinstance(order.position_id, str) or not order.position_id:
            raise ValueError("execution result order position_id must be a nonempty string")
        if not isinstance(order.asset_type, str) or not order.asset_type:
            raise ValueError("execution result order asset_type must be a nonempty string")
        _validate_order_dates(order)
        if isinstance(order.requested_quantity, bool) or not isinstance(order.requested_quantity, int) or order.requested_quantity < 1:
            raise ValueError("execution result order requested_quantity must be positive")
        linked_fills = fills_by_order.pop(order.order_id, [])
        if order.status == "FILLED":
            if order.filled_quantity != order.requested_quantity or order.unfilled_quantity != 0:
                raise ValueError("FILLED order quantities must be complete")
            if len(linked_fills) != 1:
                raise ValueError("each FILLED order must have exactly one fill")
            _validate_fill_matches_order(linked_fills[0], order)
        else:
            if order.filled_quantity != 0 or order.unfilled_quantity != order.requested_quantity:
                raise ValueError("CANCELED_UNFILLED order quantities must be unfilled")
            if linked_fills:
                raise ValueError("CANCELED_UNFILLED order must not have a fill")

    if fills_by_order:
        raise ValueError("each fill must link to exactly one FILLED order")


def _validate_entry_ledger(result: RunResult) -> None:
    if any(order.side is not Side.BUY for order in result.orders):
        raise ValueError("entry result orders must be BUY orders")


def _validate_fill_matches_order(fill: Fill, order: Order) -> None:
    if (
        fill.order_id != order.order_id
        or fill.position_id != order.position_id
        or fill.symbol != order.symbol
        or fill.asset_type != order.asset_type
        or fill.side is not order.side
        or fill.quantity != order.filled_quantity
    ):
        raise ValueError("fill must match its FILLED order identity, side, and quantity")
    if isinstance(fill.quantity, bool) or not isinstance(fill.quantity, int) or fill.quantity < 1:
        raise ValueError("fill quantity must be positive")
    _validate_fill_financials(fill)
    if type(fill.trade_date) is not date:
        raise ValueError("fill trade_date must be a plain date")
    if fill.trade_date != order.scheduled_trade_date:
        raise ValueError("fill trade_date must equal its order scheduled_trade_date")


def _validate_order_dates(order: Order) -> None:
    if type(order.signal_date) is not date:
        raise ValueError("execution result order signal_date must be a plain date")
    if order.scheduled_trade_date is not None and type(order.scheduled_trade_date) is not date:
        raise ValueError("execution result order scheduled_trade_date must be a plain date or None")
    if order.scheduled_trade_date is not None and order.scheduled_trade_date <= order.signal_date:
        raise ValueError("execution result order scheduled_trade_date must be strictly after signal_date")


def _validate_fill_financials(fill: Fill) -> None:
    values = (
        ("reference_open", fill.reference_open),
        ("raw_slippage_price", fill.raw_slippage_price),
        ("fill_price", fill.fill_price),
        ("notional", fill.notional),
        ("commission", fill.commission),
        ("sell_tax", fill.sell_tax),
        ("fixed_fee", fill.fixed_fee),
        ("total_cost", fill.total_cost),
        ("cash_delta", fill.cash_delta),
    )
    for name, value in values:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"fill {name} must be a finite Decimal")
    if any(value <= 0 for _, value in values[:4]):
        raise ValueError("fill prices and notional must be positive")
    if any(value < 0 for _, value in values[4:7]):
        raise ValueError("fill commission, sell_tax, and fixed_fee must be non-negative")
    if fill.notional != fill.fill_price * fill.quantity:
        raise ValueError("fill notional must equal fill_price multiplied by quantity")
    if fill.total_cost != fill.commission + fill.sell_tax + fill.fixed_fee:
        raise ValueError("fill total_cost must equal commission, sell_tax, and fixed_fee")
    if fill.side is Side.BUY:
        if fill.sell_tax != 0:
            raise ValueError("BUY fill sell_tax must be zero")
        if fill.cash_delta != -fill.notional - fill.total_cost or fill.cash_delta >= 0:
            raise ValueError("BUY fill cash_delta must be negative notional plus costs")
    elif fill.side is Side.SELL:
        if fill.total_cost >= fill.notional:
            raise ValueError("SELL fill total_cost must be less than notional")
        if fill.cash_delta != fill.notional - fill.total_cost or fill.cash_delta <= 0:
            raise ValueError("SELL fill cash_delta must be positive notional minus costs")
    else:
        raise ValueError("fill side must be BUY or SELL")


def _validate_cash(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a non-negative finite Decimal")


def _validate_items(values: object, item_type: type[object], name: str) -> None:
    if not isinstance(values, tuple) or not all(isinstance(value, item_type) for value in values):
        raise ValueError(f"{name} must be a tuple of {item_type.__name__} values")


def _validate_unique_ids(values: Iterable[object], field: str, name: str) -> None:
    identifiers = tuple(getattr(value, field) for value in values)
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
        raise ValueError(f"{name} {field} values must be nonempty strings")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{name} must have unique {field} values")


def _validate_cash_continuity(previous_cash: Decimal, result: RunResult) -> None:
    expected_cash = previous_cash + sum((fill.cash_delta for fill in result.fills), Decimal("0"))
    if result.cash != expected_cash:
        raise ValueError("execution result cash does not continue from state cash and fills")


def _validate_new_ledger_ids(state: PortfolioState, result: RunResult) -> None:
    _reject_id_collisions(state.orders, result.orders, "order_id")
    _reject_id_collisions(state.fills, result.fills, "fill_id")


def _reject_id_collisions(existing: Iterable[object], new: Iterable[object], field: str) -> None:
    existing_ids = {getattr(value, field) for value in existing}
    colliding_ids = existing_ids.intersection(getattr(value, field) for value in new)
    if colliding_ids:
        raise ValueError(f"execution result {field} collides with existing ledger")


def _validate_entry_positions(state: PortfolioState, result: RunResult) -> tuple[Position, ...]:
    filled_orders = {order.order_id: order for order in result.orders if order.status == "FILLED"}
    filled_fills = {fill.fill_id: fill for fill in result.fills}
    if any(order.side is not Side.BUY for order in result.orders):
        raise ValueError("entry result orders must be BUY orders")
    new_positions = result.positions
    existing_ids = {position.position_id for position in state.positions}
    existing_open_symbols = {position.symbol for position in state.positions if position.status == "OPEN"}

    for position in new_positions:
        if position.status != "OPEN":
            raise ValueError("entry result positions must be OPEN filled positions")
        order = filled_orders.get(position.entry_order_id)
        fill = filled_fills.get(position.entry_fill_id)
        if order is None or fill is None:
            raise ValueError("entry result position must correspond to a FILLED BUY order and fill")
        if (
            fill.position_id != position.position_id
            or fill.order_id != order.order_id
            or order.position_id != position.position_id
            or order.symbol != position.symbol
            or fill.symbol != position.symbol
            or order.asset_type != position.asset_type
            or fill.quantity != position.quantity
            or fill.fill_price != position.entry_price
            or position.exit_order_id is not None
            or position.exit_fill_id is not None
            or position.exit_price is not None
            or position.exit_reason is not None
        ):
            raise ValueError("entry result position identity must match its order and fill")
        if position.position_id in existing_ids:
            raise ValueError("entry result position_id collides with existing position")
        if position.symbol in existing_open_symbols:
            raise ValueError("entry result OPEN symbol collides with existing OPEN position")

    filled_position_ids = {fill.position_id for fill in filled_fills.values()}
    returned_position_ids = {position.position_id for position in new_positions}
    if filled_position_ids != returned_position_ids:
        raise ValueError("entry result must return exactly its FILLED BUY positions")
    return new_positions


def _validate_exit_ledger(result: RunResult) -> None:
    if any(order.side is not Side.SELL for order in result.orders):
        raise ValueError("exit result orders must be SELL orders")


def _validate_exit_snapshot(existing: tuple[Position, ...], result: RunResult) -> None:
    snapshot = result.positions
    existing_by_id = {position.position_id: position for position in existing}
    snapshot_by_id = {position.position_id: position for position in snapshot}
    if set(snapshot_by_id) != set(existing_by_id) or len(snapshot) != len(existing):
        raise ValueError("exit result must return exactly one snapshot for every existing position")

    open_position_ids = {position.position_id for position in existing if position.status == "OPEN"}
    if any(order.position_id not in open_position_ids for order in result.orders):
        raise ValueError("exit result orders may only target existing OPEN positions")
    closed_transition_ids = {
        position_id for position_id, original in existing_by_id.items()
        if original.status == "OPEN" and snapshot_by_id[position_id].status == "CLOSED"
    }
    filled_order_position_ids = {
        order.position_id for order in result.orders if order.status == "FILLED"
    }
    if filled_order_position_ids != closed_transition_ids:
        raise ValueError("FILLED orders must exactly match CLOSED transitions")

    for position_id, original in existing_by_id.items():
        updated = snapshot_by_id[position_id]
        if original.status == "CLOSED":
            if updated != original:
                raise ValueError("CLOSED positions must be completely unchanged")
            continue
        if _open_position_identity(updated) != _open_position_identity(original):
            raise ValueError("OPEN position entry identity, quantity, and age must be preserved")
        if updated.status == "OPEN":
            if _exit_metadata(updated) != (None, None, None, None):
                raise ValueError("OPEN positions must not contain exit metadata")
            continue
        if updated.status != "CLOSED":
            raise ValueError("OPEN positions may only remain OPEN or transition to CLOSED")
        _validate_closed_position_exit(updated, result)


def _open_position_identity(position: Position) -> tuple[object, ...]:
    return (
        position.position_id,
        position.symbol,
        position.asset_type,
        position.entry_order_id,
        position.entry_fill_id,
        position.entry_price,
        position.initial_stop_price,
        position.quantity,
        position.age_sessions,
    )


def _exit_metadata(position: Position) -> tuple[object, ...]:
    return (position.exit_order_id, position.exit_fill_id, position.exit_price, position.exit_reason)


def _validate_closed_position_exit(position: Position, result: RunResult) -> None:
    matching_orders = tuple(
        order for order in result.orders
        if order.status == "FILLED" and order.side is Side.SELL and order.position_id == position.position_id
    )
    if len(matching_orders) != 1:
        raise ValueError("OPEN to CLOSED position requires exactly one SELL FILLED order")
    order = matching_orders[0]
    matching_fills = tuple(fill for fill in result.fills if fill.order_id == order.order_id)
    if len(matching_fills) != 1:
        raise ValueError("OPEN to CLOSED position requires exactly one SELL fill")
    fill = matching_fills[0]
    if (
        position.exit_order_id != order.order_id
        or position.exit_fill_id != fill.fill_id
        or position.exit_price != fill.fill_price
        or position.exit_reason != order.intent_reason
    ):
        raise ValueError("CLOSED position exit metadata must match its SELL order and fill")
