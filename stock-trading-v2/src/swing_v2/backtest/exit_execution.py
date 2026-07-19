"""Pure one-shot next-open IOC execution for close-generated exit intents."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal

from swing_v2.contracts import DailyBar

from .engine import (
    ExecutionCostConfig,
    Fill,
    Order,
    Position,
    RunResult,
    Side,
    _fill,
    _unfilled_reason,
)
from .exit_evaluation import ExitIntent, _validate_open_position


_SCHEDULED_DATE_MISMATCH = "SCHEDULED_DATE_MISMATCH"


def execute_exit_intents_ioc(
    *,
    execution_id: str,
    positions: Sequence[Position],
    exit_intents: Sequence[ExitIntent],
    next_day_bars: Mapping[str, DailyBar | None],
    scheduled_trade_dates_by_symbol: Mapping[str, date],
    initial_cash: Decimal,
    costs: ExecutionCostConfig,
) -> RunResult:
    """Attempt each close-generated exit exactly once at its next supplied open.

    ``execution_id`` is a caller-injected nonempty plain string namespace. Every
    generated order, fill (through its order), and signal identity embeds it, so a
    namespace may be applied to an append-only ``PortfolioState`` only once.
    Intents are processed in their input order.  Each must identify exactly one
    currently OPEN position by symbol and full quantity. The caller must inject the
    actual market-calendar scheduled session for every intent; it must be after the
    signal date. Missing or invalid bars cancel under the existing IOC reasons. A
    supplied bar whose ``trade_date`` differs from the scheduled session cancels as
    ``SCHEDULED_DATE_MISMATCH`` (and is never carried to a later bar). Sell pricing,
    tax, commission, and cash release are delegated to the shared ``_fill`` cost
    logic. Unlike entry plans, an ``ExitIntent`` carries no expected price/cash
    fields, so there is deliberately no proceeds-versus-plan comparison contract.
    """
    _validate_execution_id(execution_id)
    position_values = tuple(positions)
    intent_values = tuple(exit_intents)
    _validate_inputs(
        execution_id,
        position_values,
        intent_values,
        next_day_bars,
        scheduled_trade_dates_by_symbol,
        initial_cash,
    )

    positions_by_symbol = {
        position.symbol: position for position in position_values if position.status == "OPEN"
    }
    updated_by_id = {position.position_id: position for position in position_values}
    cash = initial_cash
    orders: list[Order] = []
    fills: list[Fill] = []

    for sequence, intent in enumerate(intent_values, start=1):
        position = positions_by_symbol[intent.symbol]
        bar = next_day_bars.get(intent.symbol)
        scheduled_trade_date = scheduled_trade_dates_by_symbol[intent.symbol]
        order = _exit_order_for_intent(
            position, intent, sequence, scheduled_trade_date, execution_id
        )
        reason = _unfilled_reason(bar)
        if reason is None and bar is not None and bar.trade_date != scheduled_trade_date:
            reason = _SCHEDULED_DATE_MISMATCH
        if reason is not None:
            orders.append(_cancel(order, reason))
            continue

        assert bar is not None
        filled_order = replace(order, status="FILLED", filled_quantity=intent.quantity)
        fill = _fill(filled_order, bar, costs)
        orders.append(filled_order)
        fills.append(fill)
        cash += fill.cash_delta
        updated_by_id[position.position_id] = replace(
            position,
            exit_order_id=filled_order.order_id,
            exit_fill_id=fill.fill_id,
            exit_price=fill.fill_price,
            exit_reason=intent.reason,
            status="CLOSED",
        )

    return RunResult(
        cash=cash,
        orders=tuple(orders),
        fills=tuple(fills),
        positions=tuple(updated_by_id[position.position_id] for position in position_values),
    )


def _validate_inputs(
    execution_id: str,
    positions: tuple[Position, ...],
    intents: tuple[ExitIntent, ...],
    next_day_bars: Mapping[str, DailyBar | None],
    scheduled_trade_dates_by_symbol: Mapping[str, date],
    initial_cash: Decimal,
) -> None:
    _validate_execution_id(execution_id)
    if not isinstance(initial_cash, Decimal) or not initial_cash.is_finite() or initial_cash < 0:
        raise ValueError("initial_cash must be a non-negative finite Decimal")
    if not isinstance(next_day_bars, Mapping):
        raise ValueError("next_day_bars must be a mapping")
    if not isinstance(scheduled_trade_dates_by_symbol, Mapping):
        raise ValueError("scheduled_trade_dates_by_symbol must be a mapping")
    if not all(isinstance(position, Position) for position in positions):
        raise ValueError("positions must contain only Position values")
    if not all(isinstance(intent, ExitIntent) for intent in intents):
        raise ValueError("exit_intents must contain only ExitIntent values")

    open_positions = tuple(position for position in positions if position.status == "OPEN")
    open_symbols = tuple(position.symbol for position in open_positions)
    if len(open_symbols) != len(set(open_symbols)):
        raise ValueError("open positions must not contain duplicate symbols")
    position_ids = tuple(position.position_id for position in positions)
    if len(position_ids) != len(set(position_ids)):
        raise ValueError("positions must not contain duplicate position_ids")
    for position in open_positions:
        _validate_open_position(position)

    intent_symbols = tuple(intent.symbol for intent in intents)
    if len(intent_symbols) != len(set(intent_symbols)):
        raise ValueError("exit_intents must not contain duplicate symbols")
    for intent in intents:
        _validate_intent(intent)
        matches = tuple(position for position in open_positions if position.symbol == intent.symbol)
        if len(matches) != 1:
            raise ValueError("each ExitIntent symbol must match exactly one OPEN position")
        if intent.quantity != matches[0].quantity:
            raise ValueError("ExitIntent quantity must match its OPEN position quantity")
        if intent.symbol not in scheduled_trade_dates_by_symbol:
            raise ValueError("each ExitIntent symbol must have a scheduled trade date")
        scheduled_trade_date = scheduled_trade_dates_by_symbol[intent.symbol]
        if type(scheduled_trade_date) is not date:
            raise ValueError("scheduled trade dates must be plain date values")
        if scheduled_trade_date <= intent.signal_date:
            raise ValueError("scheduled trade date must be strictly after signal_date")
        bar = next_day_bars.get(intent.symbol)
        if bar is not None and not isinstance(bar, DailyBar):
            raise ValueError("next_day_bars values must be DailyBar or None")
        if bar is not None and (bar.symbol, bar.asset_type) != (matches[0].symbol, matches[0].asset_type):
            raise ValueError("next-day bar identity must match its OPEN position")


def _validate_execution_id(execution_id: str) -> None:
    if type(execution_id) is not str or not execution_id:
        raise ValueError("execution_id must be a nonempty plain str")


def _validate_intent(intent: ExitIntent) -> None:
    if not isinstance(intent.symbol, str) or not intent.symbol:
        raise ValueError("ExitIntent symbol must be a nonempty str")
    if isinstance(intent.quantity, bool) or not isinstance(intent.quantity, int) or intent.quantity < 1:
        raise ValueError("ExitIntent quantity must be an integer greater than or equal to one")
    if not isinstance(intent.reason, str) or not intent.reason:
        raise ValueError("ExitIntent reason must be a nonempty str")
    if type(intent.signal_date) is not date:
        raise ValueError("ExitIntent signal_date must be a plain date")


def _exit_order_for_intent(
    position: Position,
    intent: ExitIntent,
    sequence: int,
    scheduled_trade_date: date,
    execution_id: str,
) -> Order:
    return Order(
        order_id=f"{execution_id}-exit-order-{sequence}",
        signal_id=f"{execution_id}-exit-intent-{sequence}",
        position_id=position.position_id,
        symbol=position.symbol,
        asset_type=position.asset_type,
        side=Side.SELL,
        signal_date=intent.signal_date,
        scheduled_trade_date=scheduled_trade_date,
        status="PENDING",
        intent_reason=intent.reason,
        requested_quantity=intent.quantity,
        filled_quantity=0,
        unfilled_quantity=0,
        unfilled_reason=None,
    )


def _cancel(order: Order, reason: str) -> Order:
    return replace(
        order,
        status="CANCELED_UNFILLED",
        unfilled_quantity=order.requested_quantity,
        unfilled_reason=reason,
    )
