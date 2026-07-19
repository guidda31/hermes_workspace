"""Pure next-open IOC execution for ranked multi-symbol entry plans."""

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
    _validate_initial_stop_pct,
)
from .entry_planning import EntryPlan


_SCHEDULED_DATE_MISMATCH = "SCHEDULED_DATE_MISMATCH"


def execute_entry_plans_ioc(
    *,
    execution_id: str,
    plans: Sequence[EntryPlan],
    next_day_bars: Mapping[str, DailyBar | None],
    scheduled_trade_dates_by_symbol: Mapping[str, date],
    initial_cash: Decimal,
    available_cash: Decimal,
    costs: ExecutionCostConfig,
) -> RunResult:
    """Execute ranked entry plans exactly once at their respective next-day opens.

    ``execution_id`` is a caller-injected nonempty plain string namespace. Every
    generated order, fill (through its order), position, and signal identity embeds
    it, so a namespace may be applied to an append-only ``PortfolioState`` only once.
    Plans are processed in the supplied rank order. The caller injects each actual
    market-calendar scheduled session; it must be strictly after the plan's signal
    date, with no calendar arithmetic performed here. A bar on another date is a
    one-shot ``SCHEDULED_DATE_MISMATCH`` cancellation; missing or invalid bars
    retain their existing IOC cancellation reasons. ``available_cash`` is the executable cash budget (which can be less than
 ``initial_cash`` when cash is reserved elsewhere); ``cash`` in the returned
 result is total cash after executed fills. ``EntryPlan.expected_*`` values are
 close-time estimates used only for sizing and reservation. A valid t+1 open is
 always priced by the shared fill model; it fills when that *actual* debit fits
 the executable budget and otherwise becomes a one-shot ``CASH_UNAVAILABLE``
 IOC cancellation.
    """
    _validate_execution_id(execution_id)
    ordered_plans = tuple(plans)
    _validate_inputs(
        execution_id,
        ordered_plans,
        next_day_bars,
        scheduled_trade_dates_by_symbol,
        initial_cash,
        available_cash,
    )

    cash = initial_cash
    executable_cash = available_cash
    orders: list[Order] = []
    fills: list[Fill] = []
    positions: list[Position] = []

    for sequence, plan in enumerate(ordered_plans, start=1):
        bar = next_day_bars.get(plan.symbol)
        scheduled_trade_date = scheduled_trade_dates_by_symbol[plan.symbol]
        order = _entry_order_for_plan(plan, sequence, scheduled_trade_date, execution_id)
        unfilled_reason = _unfilled_reason(bar)
        if unfilled_reason is None and bar is not None and bar.trade_date != scheduled_trade_date:
            unfilled_reason = _SCHEDULED_DATE_MISMATCH
        if unfilled_reason is not None:
            orders.append(_cancel(order, unfilled_reason))
            continue

        assert bar is not None
        filled_order = replace(
            order,
            status="FILLED",
            filled_quantity=plan.quantity,
        )
        fill = _fill(filled_order, bar, costs)
        if fill.cash_delta < -executable_cash:
            orders.append(_cancel(order, "CASH_UNAVAILABLE"))
            continue

        orders.append(filled_order)
        fills.append(fill)
        cash += fill.cash_delta
        executable_cash += fill.cash_delta
        positions.append(
            Position(
                position_id=fill.position_id,
                symbol=plan.symbol,
                asset_type=plan.asset_type,
                entry_order_id=filled_order.order_id,
                entry_fill_id=fill.fill_id,
                entry_price=fill.fill_price,
                initial_stop_price=fill.fill_price * (Decimal("1") - plan.initial_stop_pct),
                quantity=fill.quantity,
                exit_order_id=None,
                exit_fill_id=None,
                exit_price=None,
                exit_reason=None,
                status="OPEN",
                age_sessions=0,
            )
        )

    return RunResult(
        cash=cash,
        orders=tuple(orders),
        fills=tuple(fills),
        positions=tuple(positions),
    )


def _validate_inputs(
    execution_id: str,
    plans: tuple[EntryPlan, ...],
    next_day_bars: Mapping[str, DailyBar | None],
    scheduled_trade_dates_by_symbol: Mapping[str, date],
    initial_cash: Decimal,
    available_cash: Decimal,
) -> None:
    _validate_execution_id(execution_id)
    for name, cash in (("initial_cash", initial_cash), ("available_cash", available_cash)):
        if not isinstance(cash, Decimal) or not cash.is_finite() or cash < 0:
            raise ValueError(f"{name} must be a non-negative finite Decimal")
    if available_cash > initial_cash:
        raise ValueError("available_cash must not exceed initial_cash")
    if not isinstance(next_day_bars, Mapping):
        raise ValueError("next_day_bars must be a mapping")
    if not isinstance(scheduled_trade_dates_by_symbol, Mapping):
        raise ValueError("scheduled_trade_dates_by_symbol must be a mapping")
    if not all(isinstance(plan, EntryPlan) for plan in plans):
        raise ValueError("plans must contain only EntryPlan values")
    for plan in plans:
        for name, value in (("symbol", plan.symbol), ("asset_type", plan.asset_type)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"EntryPlan {name} must be a nonempty str")
        if type(plan.signal_date) is not date:
            raise ValueError("EntryPlan signal_date must be a plain date")
    symbols = tuple(plan.symbol for plan in plans)
    if len(symbols) != len(set(symbols)):
        raise ValueError("plans must not contain duplicate symbols")

    for plan in plans:
        if plan.symbol not in scheduled_trade_dates_by_symbol:
            raise ValueError("each EntryPlan symbol must have a scheduled trade date")
        scheduled_trade_date = scheduled_trade_dates_by_symbol[plan.symbol]
        if type(scheduled_trade_date) is not date:
            raise ValueError("scheduled trade dates must be plain date values")
        if scheduled_trade_date <= plan.signal_date:
            raise ValueError("scheduled trade date must be strictly after signal_date")
        if isinstance(plan.quantity, bool) or not isinstance(plan.quantity, int) or plan.quantity < 1:
            raise ValueError("EntryPlan quantity must be an int greater than or equal to one")
        for name, value in (
            ("expected_fill_price", plan.expected_fill_price),
            ("expected_cash_cost", plan.expected_cash_cost),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"EntryPlan {name} must be a positive finite Decimal")
        if not isinstance(plan.initial_stop_pct, Decimal):
            raise ValueError("EntryPlan initial_stop_pct must be a Decimal")
        _validate_initial_stop_pct(plan.initial_stop_pct)
        bar = next_day_bars.get(plan.symbol)
        if bar is not None and not isinstance(bar, DailyBar):
            raise ValueError("next_day_bars values must be DailyBar or None")
        if bar is not None and (bar.symbol, bar.asset_type) != (plan.symbol, plan.asset_type):
            raise ValueError("next-day bar identity must match its entry plan")


def _validate_execution_id(execution_id: str) -> None:
    if type(execution_id) is not str or not execution_id:
        raise ValueError("execution_id must be a nonempty plain str")


def _entry_order_for_plan(
    plan: EntryPlan, sequence: int, scheduled_trade_date: date, execution_id: str
) -> Order:
    return Order(
        order_id=f"{execution_id}-entry-order-{sequence}",
        signal_id=f"{execution_id}-entry-plan-{sequence}",
        position_id=f"{execution_id}-position-{sequence}",
        symbol=plan.symbol,
        asset_type=plan.asset_type,
        side=Side.BUY,
        signal_date=plan.signal_date,
        scheduled_trade_date=scheduled_trade_date,
        status="PENDING",
        intent_reason="ENTRY_PLAN",
        requested_quantity=plan.quantity,
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



