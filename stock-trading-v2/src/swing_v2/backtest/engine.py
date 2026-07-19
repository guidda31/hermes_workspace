"""Minimal deterministic execution mechanics for the backtest v0 slice."""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Callable, Mapping, Sequence

from swing_v2.contracts import DailyBar


BPS_DENOMINATOR = Decimal("10000")
DEFAULT_INITIAL_STOP_PCT = Decimal("0.05")


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class ExecutionCostConfig:
    buy_slippage_bps: Decimal
    sell_slippage_bps: Decimal
    buy_commission_bps: Decimal
    sell_commission_bps: Decimal
    sell_tax_bps_by_asset_type: Mapping[str, Decimal]
    fixed_fee_per_order: Decimal
    tick_rounder: Callable[[Decimal, Side], Decimal]


@dataclass(frozen=True)
class Order:
    order_id: str
    signal_id: str
    position_id: str | None
    symbol: str
    asset_type: str
    side: Side
    signal_date: date
    scheduled_trade_date: date | None
    status: str
    intent_reason: str
    requested_quantity: int
    filled_quantity: int
    unfilled_quantity: int
    unfilled_reason: str | None


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    position_id: str
    trade_date: date
    symbol: str
    asset_type: str
    side: Side
    quantity: int
    reference_open: Decimal
    raw_slippage_price: Decimal
    fill_price: Decimal
    notional: Decimal
    commission: Decimal
    sell_tax: Decimal
    fixed_fee: Decimal
    total_cost: Decimal
    cash_delta: Decimal


@dataclass(frozen=True)
class Position:
    position_id: str
    symbol: str
    asset_type: str
    entry_order_id: str
    entry_fill_id: str
    entry_price: Decimal
    initial_stop_price: Decimal
    quantity: int
    exit_order_id: str | None
    exit_fill_id: str | None
    exit_price: Decimal | None
    exit_reason: str | None
    status: str
    age_sessions: int


@dataclass(frozen=True)
class RunResult:
    cash: Decimal
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    positions: tuple[Position, ...]


def _unfilled_reason(bar: DailyBar | None) -> str | None:
    if bar is None:
        return "MISSING_BAR"
    if not bar.is_tradable:
        return "NOT_TRADABLE"
    if bar.open <= Decimal("0") or bar.volume <= 0 or bar.trading_value <= Decimal("0"):
        return "INVALID_EXECUTION_BAR"
    return None


def _is_valid_evaluation_bar(bar: DailyBar | None) -> bool:
    """Return whether a bar may contribute a close to position evaluation.

    Valid closes are the same tradeable observations that can support an IOC
    execution: a present, tradable bar with positive open, volume, and trading
    value. Bars outside that set are processed for pending-order cancellation,
    but never for closes, age, SMA, or exit signals.
    """
    return (
        bar is not None
        and bar.is_tradable
        and bar.open > Decimal("0")
        and bar.volume > 0
        and bar.trading_value > Decimal("0")
    )


def _validate_initial_stop_pct(initial_stop_pct: Decimal) -> None:
    if not initial_stop_pct.is_finite() or not Decimal("0") < initial_stop_pct < Decimal("1"):
        raise ValueError("initial_stop_pct must be between zero and one, exclusive")


def _fill(order: Order, bar: DailyBar, costs: ExecutionCostConfig) -> Fill:
    is_buy = order.side is Side.BUY
    slippage_bps = costs.buy_slippage_bps if is_buy else costs.sell_slippage_bps
    commission_bps = costs.buy_commission_bps if is_buy else costs.sell_commission_bps
    price_factor = Decimal("1") if is_buy else Decimal("-1")
    raw_price = bar.open * (Decimal("1") + price_factor * slippage_bps / BPS_DENOMINATOR)
    fill_price = costs.tick_rounder(raw_price, order.side)
    notional = fill_price * Decimal(order.requested_quantity)
    commission = notional * commission_bps / BPS_DENOMINATOR
    sell_tax = (
        notional * costs.sell_tax_bps_by_asset_type[order.asset_type] / BPS_DENOMINATOR
        if order.side is Side.SELL
        else Decimal("0")
    )
    total_cost = commission + sell_tax + costs.fixed_fee_per_order
    cash_delta = -notional - total_cost if is_buy else notional - total_cost
    return Fill(
        fill_id=f"fill-{order.order_id}", order_id=order.order_id,
        position_id=order.position_id or "position-1", trade_date=bar.trade_date,
        symbol=bar.symbol, asset_type=order.asset_type, side=order.side, quantity=order.requested_quantity,
        reference_open=bar.open, raw_slippage_price=raw_price, fill_price=fill_price,
        notional=notional, commission=commission, sell_tax=sell_tax,
        fixed_fee=costs.fixed_fee_per_order, total_cost=total_cost, cash_delta=cash_delta,
    )


def _entry_order(signal_bar: DailyBar, quantity: int) -> Order:
    return Order(
        order_id="order-1", signal_id="signal-1", position_id=None,
        symbol=signal_bar.symbol, asset_type=signal_bar.asset_type, side=Side.BUY,
        signal_date=signal_bar.trade_date, scheduled_trade_date=None, status="PENDING",
        intent_reason="ENTRY_SIGNAL", requested_quantity=quantity, filled_quantity=0,
        unfilled_quantity=0, unfilled_reason=None,
    )


def _stop_order(position: Position, signal_date: date, sequence: int) -> Order:
    return Order(
        order_id=f"order-{sequence}", signal_id=f"signal-{sequence}",
        position_id=position.position_id, symbol=position.symbol, asset_type=position.asset_type,
        side=Side.SELL, signal_date=signal_date, scheduled_trade_date=None, status="PENDING",
        intent_reason="STOP_CLOSE", requested_quantity=position.quantity,
        filled_quantity=0, unfilled_quantity=0, unfilled_reason=None,
    )


def _exit_order(position: Position, signal_date: date, sequence: int, reason: str) -> Order:
    return replace(_stop_order(position, signal_date, sequence), intent_reason=reason)


def run_two_day_backtest(
    *, bars: Sequence[DailyBar | None], initial_cash: Decimal, quantity: int,
    signal_at_t_close: bool, costs: ExecutionCostConfig,
    initial_stop_pct: Decimal = DEFAULT_INITIAL_STOP_PCT,
) -> RunResult:
    """Run one close signal, its one-shot next-open entry, and a close stop exit.

    ``None`` explicitly models a missing next-day bar. A pending order is processed
    in exactly the immediately following sequence slot and is never carried later.
    """
    _validate_initial_stop_pct(initial_stop_pct)
    signal_bar = next((bar for bar in bars if bar is not None), None)
    if signal_bar is not None and any(
        bar is not None
        and (bar.symbol, bar.asset_type) != (signal_bar.symbol, signal_bar.asset_type)
        for bar in bars
    ):
        raise ValueError("all non-missing bars must have the same symbol and asset type")
    if not bars or bars[0] is None or not signal_at_t_close:
        return RunResult(cash=initial_cash, orders=(), fills=(), positions=())
    if quantity < 1:
        raise ValueError("quantity must be positive")

    cash = initial_cash
    orders: list[Order] = []
    fills: list[Fill] = []
    position: Position | None = None
    pending: Order | None = _entry_order(bars[0], quantity)

    for bar in bars[1:]:
        if pending is not None:
            attempted = replace(
                pending, scheduled_trade_date=None if bar is None else bar.trade_date
            )
            reason = _unfilled_reason(bar)
            pending = None  # IOC: even an unfilled order cannot reach a later bar.
            if reason is not None:
                orders.append(replace(
                    attempted, status="CANCELED_UNFILLED",
                    unfilled_quantity=attempted.requested_quantity, unfilled_reason=reason,
                ))
            else:
                assert bar is not None
                completed = replace(
                    attempted, status="FILLED", filled_quantity=attempted.requested_quantity
                )
                fill = _fill(completed, bar, costs)
                orders.append(completed)
                fills.append(fill)
                cash += fill.cash_delta
                if fill.side is Side.BUY:
                    position = Position(
                        position_id=fill.position_id, symbol=fill.symbol,
                        asset_type=completed.asset_type, entry_order_id=completed.order_id,
                        entry_fill_id=fill.fill_id, entry_price=fill.fill_price,
                        initial_stop_price=fill.fill_price * (Decimal("1") - initial_stop_pct),
                        quantity=fill.quantity, exit_order_id=None, exit_fill_id=None,
                        exit_price=None, exit_reason=None, status="OPEN",
                        age_sessions=0,
                    )
                elif position is not None:
                    position = replace(
                        position, exit_order_id=completed.order_id, exit_fill_id=fill.fill_id,
                        exit_price=fill.fill_price, exit_reason=completed.intent_reason,
                        status="CLOSED",
                    )

        if (
            position is not None
            and position.status == "OPEN"
            and _is_valid_evaluation_bar(bar)
        ):
            assert bar is not None
            if bar.close <= position.initial_stop_price:
                pending = _stop_order(position, bar.trade_date, len(orders) + 1)

    return RunResult(
        cash=cash, orders=tuple(orders), fills=tuple(fills),
        positions=() if position is None else (position,),
    )


def run_single_position_backtest(
    *, bars: Sequence[DailyBar | None], initial_cash: Decimal, quantity: int,
    costs: ExecutionCostConfig, initial_stop_pct: Decimal = DEFAULT_INITIAL_STOP_PCT,
) -> RunResult:
    """Run one close-signalled long position through a sequence of daily bars.

    The first bar is the entry signal day. Entry and exit intents are each IOC:
    they are attempted only at the immediately following bar's open.
    """
    _validate_initial_stop_pct(initial_stop_pct)
    if len(bars) < 2 or bars[0] is None:
        return RunResult(cash=initial_cash, orders=(), fills=(), positions=())
    if quantity < 1:
        raise ValueError("quantity must be positive")
    signal_bar = bars[0]
    if any(
        bar is not None and (bar.symbol, bar.asset_type) != (signal_bar.symbol, signal_bar.asset_type)
        for bar in bars
    ):
        raise ValueError("all non-missing bars must have the same symbol and asset type")

    cash = initial_cash
    orders: list[Order] = []
    fills: list[Fill] = []
    position: Position | None = None
    pending: Order | None = _entry_order(signal_bar, quantity)
    closes: list[Decimal] = [signal_bar.close] if _is_valid_evaluation_bar(signal_bar) else []

    for bar in bars[1:]:
        if pending is not None:
            attempted = replace(
                pending, scheduled_trade_date=None if bar is None else bar.trade_date
            )
            unfilled_reason = _unfilled_reason(bar)
            pending = None  # IOC orders never survive their scheduled open.
            if unfilled_reason is not None:
                orders.append(replace(
                    attempted, status="CANCELED_UNFILLED",
                    unfilled_quantity=attempted.requested_quantity,
                    unfilled_reason=unfilled_reason,
                ))
            else:
                assert bar is not None
                completed = replace(
                    attempted, status="FILLED", filled_quantity=attempted.requested_quantity,
                )
                fill = _fill(completed, bar, costs)
                orders.append(completed)
                fills.append(fill)
                cash += fill.cash_delta
                if fill.side is Side.BUY:
                    position = Position(
                        position_id=fill.position_id, symbol=fill.symbol,
                        asset_type=completed.asset_type, entry_order_id=completed.order_id,
                        entry_fill_id=fill.fill_id, entry_price=fill.fill_price,
                        initial_stop_price=fill.fill_price * (Decimal("1") - initial_stop_pct),
                        quantity=fill.quantity, exit_order_id=None, exit_fill_id=None,
                        exit_price=None, exit_reason=None, status="OPEN", age_sessions=0,
                    )
                elif position is not None:
                    position = replace(
                        position, exit_order_id=completed.order_id, exit_fill_id=fill.fill_id,
                        exit_price=fill.fill_price, exit_reason=completed.intent_reason,
                        status="CLOSED",
                    )

        # Process an IOC attempt before discarding an invalid close: the attempt
        # must be recorded as CANCELED_UNFILLED on this exact sequence slot.
        if not _is_valid_evaluation_bar(bar):
            continue
        assert bar is not None
        closes.append(bar.close)
        if position is None or position.status != "OPEN":
            continue

        position = replace(position, age_sessions=position.age_sessions + 1)
        if bar.close <= position.initial_stop_price:
            exit_reason = "STOP_CLOSE"
        elif position.age_sessions >= 20:
            exit_reason = "MAX_HOLD"
        elif position.age_sessions >= 10 and len(closes) >= 20 and bar.close < sum(closes[-20:]) / 20:
            exit_reason = "TREND_BREAK"
        else:
            continue
        pending = _exit_order(position, bar.trade_date, len(orders) + 1, exit_reason)

    return RunResult(
        cash=cash, orders=tuple(orders), fills=tuple(fills),
        positions=() if position is None else (position,),
    )
