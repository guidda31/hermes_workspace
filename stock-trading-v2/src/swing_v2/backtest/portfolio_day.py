"""Pure, deterministic one-session portfolio lifecycle orchestration."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from swing_v2.contracts import DailyBar

from .engine import ExecutionCostConfig, RunResult
from .entry_execution import execute_entry_plans_ioc
from .entry_planning import EntryPlan
from .exit_evaluation import ExitIntent, evaluate_exit_signals
from .exit_execution import execute_exit_intents_ioc
from .portfolio_state import PortfolioState, apply_entry_execution, apply_exit_execution
from .portfolio_valuation import PortfolioValuation, mark_to_market


@dataclass(frozen=True)
class PortfolioDayResult:
    """The immutable opening-to-close result for exactly one trade session."""

    trade_date: date
    opening_state: PortfolioState
    closing_state: PortfolioState
    exit_run_result: RunResult
    entry_run_result: RunResult
    next_pending_exit_intents: tuple[ExitIntent, ...]
    valuation: PortfolioValuation


def run_portfolio_day(
    *,
    opening_state: PortfolioState,
    trade_date: date,
    pending_entry_plans: Sequence[EntryPlan],
    pending_exit_intents: Sequence[ExitIntent],
    entry_scheduled_trade_dates_by_symbol: Mapping[str, date],
    exit_scheduled_trade_dates_by_symbol: Mapping[str, date],
    entry_open_bars_by_symbol: Mapping[str, DailyBar | None],
    exit_open_bars_by_symbol: Mapping[str, DailyBar | None],
    entry_execution_id: str,
    exit_execution_id: str,
    planned_entry_available_cash: Decimal,
    costs: ExecutionCostConfig,
    closing_bars_by_symbol: Mapping[str, DailyBar | None],
    historical_closes_by_symbol: Mapping[str, Sequence[Decimal]],
    max_gap_up_pct: Decimal | None = None,
) -> PortfolioDayResult:
    """Process pending IOC orders, close-time exits, and NAV for ``trade_date``.

    Pending SELL intents execute first and are applied before BUY plans.  An entry
    plan is a prior-close commitment: ``planned_entry_available_cash`` is its
    already-planned cash budget, measured against *opening* cash, and therefore is
    never resized from this session's SELL proceeds.  The entry executor receives
    the cash after SELLs as its total balance, but this unchanged planned budget as
    ``available_cash``.  This makes same-day exit proceeds unavailable to a plan
    while still retaining correct total cash accounting.

    Every supplied scheduled-date value must equal this day's trade date and every
    pending plan/intent must have been generated strictly before it.  IOC
    cancellations are applied to the ledger and are not carried; a later close can
    independently generate a new exit intent.  Exit evaluation's aged positions
    replace the post-execution snapshot before valuation, preserving age updates.
    """
    entry_plans = tuple(pending_entry_plans)
    exit_intents = tuple(pending_exit_intents)
    _validate_day_inputs(
        opening_state=opening_state,
        trade_date=trade_date,
        entry_plans=entry_plans,
        exit_intents=exit_intents,
        entry_scheduled_dates=entry_scheduled_trade_dates_by_symbol,
        exit_scheduled_dates=exit_scheduled_trade_dates_by_symbol,
        entry_execution_id=entry_execution_id,
        exit_execution_id=exit_execution_id,
        planned_entry_available_cash=planned_entry_available_cash,
        closing_bars=closing_bars_by_symbol,
    )

    # These calls deliberately retain canceled IOC orders in the append-only ledger.
    exit_run = execute_exit_intents_ioc(
        execution_id=exit_execution_id,
        positions=opening_state.positions,
        exit_intents=exit_intents,
        next_day_bars=exit_open_bars_by_symbol,
        scheduled_trade_dates_by_symbol=exit_scheduled_trade_dates_by_symbol,
        initial_cash=opening_state.cash,
        costs=costs,
    )
    after_exits = apply_exit_execution(opening_state, exit_run)

    entry_run = execute_entry_plans_ioc(
        execution_id=entry_execution_id,
        plans=entry_plans,
        next_day_bars=entry_open_bars_by_symbol,
        scheduled_trade_dates_by_symbol=entry_scheduled_trade_dates_by_symbol,
        initial_cash=after_exits.cash,
        available_cash=planned_entry_available_cash,
        costs=costs,
        max_gap_up_pct=max_gap_up_pct,
    )
    after_entries = apply_entry_execution(after_exits, entry_run)

    evaluation = evaluate_exit_signals(
        positions=after_entries.positions,
        bars_by_symbol=closing_bars_by_symbol,
        historical_closes_by_symbol=historical_closes_by_symbol,
        pending_exit_symbols=set(),
    )
    closing_state = PortfolioState(
        cash=after_entries.cash,
        positions=evaluation.positions,
        orders=after_entries.orders,
        fills=after_entries.fills,
    )
    # The last close in each open/pending symbol's history (ending at trade_date) is
    # the last valid mark, used to stale-mark a position whose session bar is absent
    # or untradable rather than crashing the run.
    fallback_close_by_symbol = {
        symbol: tuple(closes)[-1]
        for symbol, closes in historical_closes_by_symbol.items()
        if tuple(closes)
    }
    valuation = mark_to_market(
        state=closing_state,
        bars_by_symbol=closing_bars_by_symbol,
        valuation_date=trade_date,
        fallback_close_by_symbol=fallback_close_by_symbol,
    )
    return PortfolioDayResult(
        trade_date=trade_date,
        opening_state=opening_state,
        closing_state=closing_state,
        exit_run_result=exit_run,
        entry_run_result=entry_run,
        next_pending_exit_intents=evaluation.exit_intents,
        valuation=valuation,
    )


def _validate_day_inputs(
    *,
    opening_state: object,
    trade_date: object,
    entry_plans: tuple[EntryPlan, ...],
    exit_intents: tuple[ExitIntent, ...],
    entry_scheduled_dates: object,
    exit_scheduled_dates: object,
    entry_execution_id: object,
    exit_execution_id: object,
    planned_entry_available_cash: object,
    closing_bars: object,
) -> None:
    if type(trade_date) is not date:
        raise ValueError("trade_date must be a plain date")
    if not isinstance(opening_state, PortfolioState):
        raise ValueError("opening_state must be a PortfolioState")
    # Reconstruct to validate tampered state before any executor is invoked.
    PortfolioState(
        cash=opening_state.cash,
        positions=opening_state.positions,
        orders=opening_state.orders,
        fills=opening_state.fills,
    )
    _validate_execution_id(entry_execution_id, "entry_execution_id")
    _validate_execution_id(exit_execution_id, "exit_execution_id")
    if entry_execution_id == exit_execution_id:
        raise ValueError("entry_execution_id and exit_execution_id must differ")
    if (
        not isinstance(planned_entry_available_cash, Decimal)
        or not planned_entry_available_cash.is_finite()
        or planned_entry_available_cash < 0
        or planned_entry_available_cash > opening_state.cash
    ):
        raise ValueError("planned_entry_available_cash must be a finite Decimal within opening cash")
    _validate_scheduled_dates(entry_scheduled_dates, trade_date, "entry")
    _validate_scheduled_dates(exit_scheduled_dates, trade_date, "exit")
    if not all(isinstance(plan, EntryPlan) for plan in entry_plans):
        raise ValueError("pending_entry_plans must contain only EntryPlan values")
    if not all(isinstance(intent, ExitIntent) for intent in exit_intents):
        raise ValueError("pending_exit_intents must contain only ExitIntent values")
    if any(type(plan.signal_date) is not date or plan.signal_date >= trade_date for plan in entry_plans):
        raise ValueError("EntryPlan signal_date must be strictly before trade_date")
    if any(type(intent.signal_date) is not date or intent.signal_date >= trade_date for intent in exit_intents):
        raise ValueError("ExitIntent signal_date must be strictly before trade_date")
    _validate_closing_bars(closing_bars, trade_date)


def _validate_execution_id(value: object, name: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a nonempty plain str")


def _validate_scheduled_dates(values: object, trade_date: date, side: str) -> None:
    if not isinstance(values, Mapping):
        raise ValueError(f"{side}_scheduled_trade_dates_by_symbol must be a mapping")
    for symbol, scheduled_date in values.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("scheduled trade date symbols must be nonempty strings")
        if type(scheduled_date) is not date or scheduled_date != trade_date:
            raise ValueError("all scheduled trade dates must equal trade_date")


def _validate_closing_bars(values: object, trade_date: date) -> None:
    if not isinstance(values, Mapping):
        raise ValueError("closing_bars_by_symbol must be a mapping")
    for symbol, bar in values.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("closing_bars_by_symbol keys must be nonempty strings")
        if bar is not None and not isinstance(bar, DailyBar):
            raise ValueError("closing_bars_by_symbol values must be DailyBar or None")
        if bar is not None and bar.trade_date != trade_date:
            raise ValueError("closing bar trade_date must equal trade_date")
