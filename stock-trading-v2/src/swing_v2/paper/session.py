"""Simulate one paper-trading session from guardrail-admitted decisions.

Given the current paper account, the admitted BUY/SELL/HOLD decisions, and the next
session's execution bars, this produces simulated fills (next-open, reusing the
backtest cost model), the updated account, realized P&L, and a self-reconciled cash
balance. SELLs are processed before BUYs, so exit proceeds are only sizing budget for
the *next* session — not this one — mirroring the backtest.

No real money, order, broker, or network is involved: fills are simulated and the
account lives only in memory / whatever the caller durably stores. This module does
not import the live submitter or gate and opens no connection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_FLOOR

from ..backtest.engine import ExecutionCostConfig, Fill, Order, Side, _fill, _unfilled_reason
from ..contracts import DailyBar
from ..llm.decision import DecisionAction, SymbolDecision

_DEFAULT_INITIAL_STOP_PCT = Decimal("0.05")


@dataclass(frozen=True)
class PaperPosition:
    symbol: str
    asset_type: str
    entry_price: Decimal
    quantity: int
    entry_date: date

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("PaperPosition symbol must be a nonempty plain str")
        if type(self.asset_type) is not str or not self.asset_type:
            raise ValueError("PaperPosition asset_type must be a nonempty plain str")
        if type(self.entry_price) is not Decimal or not self.entry_price.is_finite() or self.entry_price <= 0:
            raise ValueError("PaperPosition entry_price must be a positive finite Decimal")
        if isinstance(self.quantity, bool) or type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("PaperPosition quantity must be a positive int")
        if type(self.entry_date) is not date:
            raise ValueError("PaperPosition entry_date must be a plain date")


@dataclass(frozen=True)
class PaperAccount:
    cash: Decimal
    positions: tuple[PaperPosition, ...] = ()

    def __post_init__(self) -> None:
        if type(self.cash) is not Decimal or not self.cash.is_finite() or self.cash < 0:
            raise ValueError("PaperAccount cash must be a nonnegative finite Decimal")
        if not isinstance(self.positions, tuple) or not all(isinstance(p, PaperPosition) for p in self.positions):
            raise ValueError("PaperAccount positions must be a tuple of PaperPosition")
        symbols = [p.symbol for p in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("PaperAccount positions must have unique symbols")
        object.__setattr__(self, "positions", tuple(sorted(self.positions, key=lambda p: p.symbol)))


@dataclass(frozen=True)
class UnfilledDecision:
    symbol: str
    side: str
    reason: str


@dataclass(frozen=True)
class PaperSessionResult:
    trade_date: date
    account: PaperAccount
    fills: tuple[Fill, ...]
    unfilled: tuple[UnfilledDecision, ...]
    realized_pnl: Decimal
    nav: Decimal


def simulate_paper_session(
    account: PaperAccount,
    *,
    decisions: Sequence[SymbolDecision],
    session_bars: Mapping[str, DailyBar | None],
    reference_close_by_symbol: Mapping[str, Decimal],
    costs: ExecutionCostConfig,
    trade_date: date,
    max_gap_up_pct: Decimal | None = None,
    initial_stop_pct: Decimal = _DEFAULT_INITIAL_STOP_PCT,
) -> PaperSessionResult:
    """Apply admitted decisions to the paper account with simulated next-open fills."""
    _validate_inputs(account, decisions, session_bars, reference_close_by_symbol, costs, trade_date, max_gap_up_pct)

    held: dict[str, PaperPosition] = {p.symbol: p for p in account.positions}
    equity = account.cash + sum(
        (Decimal(p.quantity) * _mark(p, reference_close_by_symbol) for p in account.positions), Decimal("0")
    )

    cash = account.cash
    fills: list[Fill] = []
    unfilled: list[UnfilledDecision] = []
    realized_pnl = Decimal("0")

    for decision in decisions:
        if decision.action is DecisionAction.SELL:
            cash, realized = _apply_sell(decision, held, session_bars, costs, trade_date, fills, unfilled, cash)
            realized_pnl += realized

    for decision in decisions:
        if decision.action is DecisionAction.BUY:
            cash = _apply_buy(
                decision, held, session_bars, reference_close_by_symbol, costs, trade_date,
                max_gap_up_pct, equity, fills, unfilled, cash,
            )
        # HOLD is a deliberate no-op: it neither fills nor changes the account.

    new_account = PaperAccount(cash=cash, positions=tuple(held.values()))
    nav = cash + sum(
        (Decimal(p.quantity) * _session_mark(p, session_bars, reference_close_by_symbol) for p in new_account.positions),
        Decimal("0"),
    )
    # Self-reconciliation: cash must be exactly the starting cash plus every fill delta.
    if new_account.cash != account.cash + sum((f.cash_delta for f in fills), Decimal("0")):
        raise ValueError("paper session cash failed reconciliation")
    return PaperSessionResult(
        trade_date=trade_date, account=new_account, fills=tuple(fills),
        unfilled=tuple(unfilled), realized_pnl=realized_pnl, nav=nav,
    )


def _apply_sell(decision, held, session_bars, costs, trade_date, fills, unfilled, cash):
    position = held.get(decision.symbol)
    if position is None:
        unfilled.append(UnfilledDecision(decision.symbol, "SELL", "NOT_HELD"))
        return cash, Decimal("0")
    bar = session_bars.get(decision.symbol)
    reason = _unfilled_reason(bar)
    if reason is not None:
        unfilled.append(UnfilledDecision(decision.symbol, "SELL", reason))
        return cash, Decimal("0")
    order = _paper_order(Side.SELL, position.symbol, position.asset_type, position.quantity, trade_date)
    fill = _fill(order, bar, costs)
    fills.append(fill)
    del held[decision.symbol]
    realized = fill.cash_delta - Decimal(position.quantity) * position.entry_price
    return cash + fill.cash_delta, realized


def _apply_buy(decision, held, session_bars, reference_close_by_symbol, costs, trade_date, max_gap_up_pct, equity, fills, unfilled, cash):
    symbol = decision.symbol
    if symbol in held:
        unfilled.append(UnfilledDecision(symbol, "BUY", "ALREADY_HELD"))
        return cash
    bar = session_bars.get(symbol)
    reason = _unfilled_reason(bar)
    if reason is not None:
        unfilled.append(UnfilledDecision(symbol, "BUY", reason))
        return cash
    reference_close = reference_close_by_symbol.get(symbol)
    if reference_close is None:
        unfilled.append(UnfilledDecision(symbol, "BUY", "NO_REFERENCE_CLOSE"))
        return cash
    if max_gap_up_pct is not None and bar.open > reference_close * (Decimal("1") + max_gap_up_pct):
        unfilled.append(UnfilledDecision(symbol, "BUY", "GAP_UP_BLOCKED"))
        return cash
    quantity = int((decision.target_weight * equity / bar.open).to_integral_value(rounding=ROUND_FLOOR))
    if quantity < 1:
        unfilled.append(UnfilledDecision(symbol, "BUY", "SUB_ONE_SHARE"))
        return cash
    order = _paper_order(Side.BUY, symbol, bar.asset_type, quantity, trade_date)
    fill = _fill(order, bar, costs)
    if fill.cash_delta < -cash:
        unfilled.append(UnfilledDecision(symbol, "BUY", "CASH_UNAVAILABLE"))
        return cash
    fills.append(fill)
    held[symbol] = PaperPosition(symbol, bar.asset_type, fill.fill_price, quantity, trade_date)
    return cash + fill.cash_delta


def _paper_order(side: Side, symbol: str, asset_type: str, quantity: int, trade_date: date) -> Order:
    return Order(
        order_id=f"paper-{trade_date.isoformat()}-{side.value}-{symbol}",
        signal_id=f"paper-{symbol}", position_id=f"paper-{symbol}",
        symbol=symbol, asset_type=asset_type, side=side, signal_date=trade_date,
        scheduled_trade_date=trade_date, status="PENDING", intent_reason="PAPER",
        requested_quantity=quantity, filled_quantity=0, unfilled_quantity=0, unfilled_reason=None,
    )


def _mark(position: PaperPosition, reference_close_by_symbol: Mapping[str, Decimal]) -> Decimal:
    price = reference_close_by_symbol.get(position.symbol)
    return price if isinstance(price, Decimal) and price.is_finite() and price > 0 else position.entry_price


def _session_mark(position: PaperPosition, session_bars: Mapping[str, DailyBar | None], reference_close_by_symbol: Mapping[str, Decimal]) -> Decimal:
    bar = session_bars.get(position.symbol)
    if bar is not None and bar.is_tradable and bar.close.is_finite() and bar.close > 0:
        return bar.close
    return _mark(position, reference_close_by_symbol)


def _validate_inputs(account, decisions, session_bars, reference_close_by_symbol, costs, trade_date, max_gap_up_pct) -> None:
    if not isinstance(account, PaperAccount):
        raise ValueError("account must be a PaperAccount")
    if type(trade_date) is not date:
        raise ValueError("trade_date must be a plain date")
    if not isinstance(session_bars, Mapping):
        raise ValueError("session_bars must be a mapping")
    if not isinstance(reference_close_by_symbol, Mapping):
        raise ValueError("reference_close_by_symbol must be a mapping")
    if not isinstance(costs, ExecutionCostConfig) or not callable(getattr(costs, "tick_rounder", None)):
        raise ValueError("costs must be an ExecutionCostConfig")
    if not all(type(d) is SymbolDecision for d in decisions):
        raise ValueError("decisions must be SymbolDecision values")
    if max_gap_up_pct is not None and (
        type(max_gap_up_pct) is not Decimal or not max_gap_up_pct.is_finite() or max_gap_up_pct <= 0
    ):
        raise ValueError("max_gap_up_pct must be a positive finite Decimal or None")
