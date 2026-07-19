"""Pure close-time evaluation of exit signals for a portfolio of positions."""

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from swing_v2.contracts import DailyBar

from .engine import Position, _is_valid_evaluation_bar


@dataclass(frozen=True)
class ExitIntent:
    """A SELL instruction generated at a valid close for next-open execution."""

    symbol: str
    quantity: int
    reason: str
    signal_date: date


@dataclass(frozen=True)
class ExitEvaluationResult:
    """Positions after this close evaluation and newly generated exit intents."""

    positions: tuple[Position, ...]
    exit_intents: tuple[ExitIntent, ...]


def evaluate_exit_signals(
    *,
    positions: Sequence[Position],
    bars_by_symbol: Mapping[str, DailyBar | None],
    historical_closes_by_symbol: Mapping[str, Sequence[Decimal]],
    pending_exit_symbols: Set[str],
) -> ExitEvaluationResult:
    """Evaluate every open position at today's close without placing an order.

    ``historical_closes_by_symbol`` contains prior valid closes only; the current
    valid close is appended before calculating the 20-session SMA.  Position age
    lifecycle: 진입 체결 후 종가 평가 전 0, 유효 종가 평가 후 1.  Invalid bars
    leave a position untouched and cannot create an intent.
    """
    position_values = tuple(positions)
    _validate_inputs(
        position_values, bars_by_symbol, historical_closes_by_symbol, pending_exit_symbols
    )

    updated: list[Position] = []
    intents: list[ExitIntent] = []
    for position in position_values:
        if position.status != "OPEN":
            updated.append(position)
            continue

        bar = bars_by_symbol.get(position.symbol)
        if not _is_valid_evaluation_bar(bar):
            updated.append(position)
            continue

        assert bar is not None
        evaluated = replace(position, age_sessions=position.age_sessions + 1)
        updated.append(evaluated)
        if position.symbol in pending_exit_symbols:
            continue

        reason = _exit_reason(
            position=evaluated,
            current_close=bar.close,
            historical_closes=historical_closes_by_symbol[position.symbol],
        )
        if reason is not None:
            intents.append(
                ExitIntent(
                    symbol=position.symbol,
                    quantity=position.quantity,
                    reason=reason,
                    signal_date=bar.trade_date,
                )
            )

    return ExitEvaluationResult(positions=tuple(updated), exit_intents=tuple(intents))


def _exit_reason(
    *, position: Position, current_close: Decimal, historical_closes: Sequence[Decimal]
) -> str | None:
    if current_close <= position.initial_stop_price:
        return "STOP_CLOSE"
    if position.age_sessions >= 20:
        return "MAX_HOLD"
    closes = (*historical_closes, current_close)
    if (
        position.age_sessions >= 10
        and len(closes) >= 20
        and current_close < sum(closes[-20:]) / Decimal("20")
    ):
        return "TREND_BREAK"
    return None


def _validate_inputs(
    positions: tuple[Position, ...],
    bars_by_symbol: Mapping[str, DailyBar | None],
    historical_closes_by_symbol: Mapping[str, Sequence[Decimal]],
    pending_exit_symbols: Set[str],
) -> None:
    if not isinstance(bars_by_symbol, Mapping):
        raise ValueError("bars_by_symbol must be a mapping")
    if not isinstance(historical_closes_by_symbol, Mapping):
        raise ValueError("historical_closes_by_symbol must be a mapping")
    if not isinstance(pending_exit_symbols, set) or not all(
        isinstance(symbol, str) and symbol for symbol in pending_exit_symbols
    ):
        raise ValueError("pending_exit_symbols must be a set of nonempty symbols")
    if not all(isinstance(position, Position) for position in positions):
        raise ValueError("positions must contain only Position values")

    open_positions = tuple(position for position in positions if position.status == "OPEN")
    symbols = tuple(position.symbol for position in open_positions)
    if len(symbols) != len(set(symbols)):
        raise ValueError("open positions must not contain duplicate symbols")

    for position in open_positions:
        _validate_open_position(position)
        bar = bars_by_symbol.get(position.symbol)
        if bar is not None and not isinstance(bar, DailyBar):
            raise ValueError("bars_by_symbol values must be DailyBar or None")
        if bar is not None and (bar.symbol, bar.asset_type) != (position.symbol, position.asset_type):
            raise ValueError("daily bar identity must match its position")
        if position.symbol not in historical_closes_by_symbol:
            raise ValueError("historical closes are required for every open position")
        history = historical_closes_by_symbol[position.symbol]
        if isinstance(history, (str, bytes)) or not isinstance(history, Sequence):
            raise ValueError("historical closes must be a sequence")
        if not all(
            isinstance(close, Decimal) and close.is_finite() and close > Decimal("0")
            for close in history
        ):
            raise ValueError("historical closes must contain positive finite Decimal values")


def _validate_open_position(position: Position) -> None:
    if not isinstance(position.symbol, str) or not position.symbol:
        raise ValueError("Position symbol must be a nonempty str")
    if not isinstance(position.asset_type, str) or not position.asset_type:
        raise ValueError("Position asset_type must be a nonempty str")
    if isinstance(position.age_sessions, bool) or not isinstance(position.age_sessions, int) or position.age_sessions < 0:
        raise ValueError("open Position age_sessions must be an integer greater than or equal to zero")
    if isinstance(position.quantity, bool) or not isinstance(position.quantity, int) or position.quantity < 1:
        raise ValueError("open Position quantity must be an integer greater than or equal to one")
    for name, value in (
        ("entry_price", position.entry_price),
        ("initial_stop_price", position.initial_stop_price),
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= Decimal("0"):
            raise ValueError(f"open Position {name} must be a positive finite Decimal")
