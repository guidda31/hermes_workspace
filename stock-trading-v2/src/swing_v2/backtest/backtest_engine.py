"""Calendar-driven, no-lookahead portfolio backtest orchestration.

This module owns the multi-session runner.  ``engine.py`` intentionally remains
limited to shared order/fill mechanics used by the smaller v0 primitives.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from swing_v2.contracts import DailyBar
from swing_v2.universe_metadata import UniverseExclusion, UniverseMetadataSnapshot, select_eligible_universe

from .close_time_candidates import CandidateAssessment, assess_close_time_candidates
from .daily_loss_guard import DailyLossGuardConfig, DailyLossGuardInput, DailyLossGuardResult, evaluate_daily_loss_guard
from .engine import ExecutionCostConfig, Fill, Order, Position
from .entry_planning import EntryCandidate, EntryPlan, create_entry_plans
from .exit_evaluation import ExitIntent
from .portfolio_day import PortfolioDayResult, run_portfolio_day
from .portfolio_state import PortfolioState


@dataclass(frozen=True)
class BacktestRiskConfig:
    risk_per_position: Decimal
    max_positions: int
    max_position_notional_pct: Decimal
    initial_stop_pct: Decimal
    max_daily_loss_pct: Decimal


@dataclass(frozen=True)
class BacktestConfig:
    start_date: date
    end_date: date
    universe: tuple[str, ...]
    market_symbol: str
    initial_cash: Decimal
    costs: ExecutionCostConfig
    risk: BacktestRiskConfig
    universe_metadata: UniverseMetadataSnapshot


class BacktestData(Protocol):
    def get_bars(self, trade_date: date) -> Mapping[str, DailyBar | None]: ...
    def get_market_index_bar(self, trade_date: date) -> DailyBar | None: ...
    def get_historical_closes(self, symbol: str, end_date: date, window: int) -> Sequence[Decimal]: ...
    def get_historical_bars(self, symbol: str, end_date: date, window: int) -> Sequence[DailyBar]: ...
    def get_trade_calendar(self, start_date: date, end_date: date) -> Sequence[date]: ...
    def get_asset_type(self, symbol: str) -> str: ...


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    signal_date: date
    symbol: str
    eligible: bool
    rejection_reason: str | None
    risk_on: bool
    liquidity_pass: bool
    momentum_pass: bool
    candidate_rank: int | None
    breakout_strength: Decimal | None
    momentum_60: Decimal | None
    scheduled_trade_date: date | None


@dataclass(frozen=True)
class UniverseExclusionRecord:
    """A point-in-time metadata denial retained independently of signal computation."""

    signal_date: date
    symbol: str
    reason: str


@dataclass(frozen=True)
class EquityCurvePoint:
    trade_date: date
    cash: Decimal
    market_value: Decimal
    nav_close: Decimal
    daily_return: Decimal
    cumulative_return: Decimal
    peak_nav: Decimal
    drawdown: Decimal
    gross_exposure: Decimal
    position_count: int
    stale_mark_count: int
    new_entry_blocked: bool
    new_entry_block_reason: str | None


@dataclass(frozen=True)
class BacktestResult:
    all_day_results: tuple[PortfolioDayResult, ...]
    equity_curve: tuple[EquityCurvePoint, ...]
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    positions: tuple[Position, ...]
    signals: tuple[SignalRecord, ...]
    universe_exclusions: tuple[UniverseExclusionRecord, ...]


class BacktestRunner:
    """Run one immutable configuration over only its injected trading calendar."""

    def run(self, config: BacktestConfig, data: BacktestData) -> BacktestResult:
        _validate_config(config)
        calendar = _validated_calendar(data, config)
        if not calendar:
            return BacktestResult((), (), (), (), (), (), ())

        state = PortfolioState(cash=config.initial_cash, positions=(), orders=(), fills=())
        pending_entry_plans: tuple[EntryPlan, ...] = ()
        # This is the close-time cash budget saved with the next-session plans;
        # it is not silently recomputed from an execution-day gap or sell proceeds.
        planned_entry_cash_budget = config.initial_cash
        pending_exit_intents: tuple[ExitIntent, ...] = ()
        prior_nav = config.initial_cash
        days: list[PortfolioDayResult] = []
        guards: list[DailyLossGuardResult] = []
        signals: list[SignalRecord] = []
        universe_exclusions: list[UniverseExclusionRecord] = []

        for index, trade_date in enumerate(calendar):
            bars = _validated_bars(data.get_bars(trade_date), trade_date)
            open_or_pending_symbols = {
                position.symbol for position in state.positions if position.status == "OPEN"
            } | {plan.symbol for plan in pending_entry_plans}
            # Every close-time evaluation of an open position receives its history.
            historical_closes = {
                symbol: _validated_closes(data.get_historical_closes(symbol, trade_date, 200), symbol)
                for symbol in open_or_pending_symbols
            }
            day = run_portfolio_day(
                opening_state=state,
                trade_date=trade_date,
                pending_entry_plans=pending_entry_plans,
                pending_exit_intents=pending_exit_intents,
                entry_scheduled_trade_dates_by_symbol={plan.symbol: trade_date for plan in pending_entry_plans},
                exit_scheduled_trade_dates_by_symbol={intent.symbol: trade_date for intent in pending_exit_intents},
                entry_open_bars_by_symbol=bars,
                exit_open_bars_by_symbol=bars,
                entry_execution_id=f"entry-{trade_date.isoformat()}",
                exit_execution_id=f"exit-{trade_date.isoformat()}",
                planned_entry_available_cash=planned_entry_cash_budget,
                costs=config.costs,
                closing_bars_by_symbol=bars,
                historical_closes_by_symbol=historical_closes,
            )
            days.append(day)
            state = day.closing_state
            pending_exit_intents = day.next_pending_exit_intents

            # The daily P&L is deliberately calculated exactly once from NAV.
            daily_pnl = day.valuation.nav - prior_nav
            guard = evaluate_daily_loss_guard(
                DailyLossGuardInput(day_start_equity=prior_nav, realized_pnl=daily_pnl, unrealized_pnl=Decimal("0")),
                DailyLossGuardConfig(max_daily_loss_pct=config.risk.max_daily_loss_pct),
            )
            guards.append(guard)
            next_trade_date = calendar[index + 1] if index + 1 < len(calendar) else None
            pending_entry_plans = ()
            if next_trade_date is not None:
                assessments, exclusions = self._assess_close(config, data, trade_date, bars)
                universe_exclusions.extend(
                    UniverseExclusionRecord(trade_date, exclusion.symbol, exclusion.reason)
                    for exclusion in exclusions
                )
                pending_entry_plans = self._plan_next_entries(config, day, bars, assessments, guard)
                ranks = {plan.symbol: rank for rank, plan in enumerate(pending_entry_plans, start=1)}
                signals.extend(_signal_records(assessments, trade_date, next_trade_date, ranks))
                # A new batch is reserved against the just-closed real cash state.
                planned_entry_cash_budget = state.cash
            prior_nav = day.valuation.nav

        return _build_result(days, guards, signals, universe_exclusions, config.initial_cash)

    @staticmethod
    def _assess_close(
        config: BacktestConfig,
        data: BacktestData,
        trade_date: date,
        bars: Mapping[str, DailyBar | None],
    ) -> tuple[tuple[CandidateAssessment, ...], tuple[UniverseExclusion, ...]]:
        # All candidate inputs terminate at trade_date; the next session's bar is
        # neither read nor used to construct price estimates or ranks.
        selection = select_eligible_universe(
            config.universe_metadata, trade_date, requested_symbols=config.universe,
        )
        market_closes = _validated_closes(
            data.get_historical_closes(config.market_symbol, trade_date, 201), config.market_symbol
        )
        asset_types = {symbol: _validated_asset_type(data.get_asset_type(symbol)) for symbol in selection.symbols}
        histories = {
            symbol: _validated_history(data.get_historical_bars(symbol, trade_date, 201), symbol, asset_types[symbol], trade_date)
            for symbol in selection.symbols
        }
        return assess_close_time_candidates(
            signal_date=trade_date,
            market_closes=market_closes,
            asset_types=asset_types,
            asset_histories=histories,
            universe_symbols=set(selection.symbols),
        ), selection.exclusions

    @staticmethod
    def _plan_next_entries(
        config: BacktestConfig,
        day: PortfolioDayResult,
        closing_bars: Mapping[str, DailyBar | None],
        assessments: Sequence[CandidateAssessment],
        guard: DailyLossGuardResult,
    ) -> tuple[EntryPlan, ...]:
        if not guard.entries_allowed:
            return ()
        return _create_close_price_plans(config, day, closing_bars, assessments)


def _create_close_price_plans(
    config: BacktestConfig,
    day: PortfolioDayResult,
    closing_bars: Mapping[str, DailyBar | None],
    assessments: Sequence[CandidateAssessment],
) -> tuple[EntryPlan, ...]:
    candidates: list[EntryCandidate] = []
    for assessment in assessments:
        bar = closing_bars.get(assessment.symbol)
        if not assessment.candidate.eligible or bar is None:
            continue
        # This estimate is explicitly t's close and therefore cannot peek at t+1.
        candidates.append(EntryCandidate(assessment.candidate, bar.close, assessment.asset_type or "", day.trade_date))
    return create_entry_plans(
        candidates=candidates,
        active_symbols={position.symbol for position in day.closing_state.positions if position.status == "OPEN"},
        pending_entry_symbols=set(),
        max_positions=config.risk.max_positions,
        nav=day.valuation.nav,
        available_cash=day.closing_state.cash,
        costs=config.costs,
        risk_per_position=config.risk.risk_per_position,
        max_position_notional_pct=config.risk.max_position_notional_pct,
        initial_stop_pct=config.risk.initial_stop_pct,
        daily_loss_guard_input=None,
        daily_loss_guard_config=None,
    )


def _validate_config(config: object) -> None:
    if not isinstance(config, BacktestConfig):
        raise ValueError("config must be BacktestConfig")
    if type(config.start_date) is not date or type(config.end_date) is not date or config.start_date > config.end_date:
        raise ValueError("config dates must be plain ordered date values")
    if not config.universe or any(type(symbol) is not str or not symbol for symbol in config.universe):
        raise ValueError("config universe must contain nonempty plain strings")
    if len(config.universe) != len(set(config.universe)):
        raise ValueError("config universe must not contain duplicate symbols")
    if not isinstance(config.universe_metadata, UniverseMetadataSnapshot):
        raise ValueError("config universe_metadata must be a UniverseMetadataSnapshot")
    if type(config.market_symbol) is not str or not config.market_symbol:
        raise ValueError("config market_symbol must be a nonempty plain str")
    if not isinstance(config.initial_cash, Decimal) or not config.initial_cash.is_finite() or config.initial_cash <= 0:
        raise ValueError("config initial_cash must be a positive finite Decimal")
    if not isinstance(config.risk, BacktestRiskConfig):
        raise ValueError("config risk must be BacktestRiskConfig")
    risk = config.risk
    if isinstance(risk.max_positions, bool) or not isinstance(risk.max_positions, int) or risk.max_positions < 1:
        raise ValueError("risk max_positions must be a positive int")
    for name, value in (("risk_per_position", risk.risk_per_position), ("max_position_notional_pct", risk.max_position_notional_pct), ("initial_stop_pct", risk.initial_stop_pct), ("max_daily_loss_pct", risk.max_daily_loss_pct)):
        if not isinstance(value, Decimal) or not value.is_finite() or not Decimal("0") < value < Decimal("1"):
            raise ValueError(f"risk {name} must be a Decimal between zero and one")
    _validate_costs(config.costs)


def _validate_costs(costs: object) -> None:
    if not isinstance(costs, ExecutionCostConfig) or not callable(getattr(costs, "tick_rounder", None)):
        raise ValueError("config costs must be a valid ExecutionCostConfig")
    for value in (costs.buy_slippage_bps, costs.sell_slippage_bps, costs.buy_commission_bps, costs.sell_commission_bps, costs.fixed_fee_per_order):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("cost values must be nonnegative finite Decimal values")
    if not isinstance(costs.sell_tax_bps_by_asset_type, Mapping):
        raise ValueError("sell_tax_bps_by_asset_type must be a mapping")
    for asset_type, value in costs.sell_tax_bps_by_asset_type.items():
        if type(asset_type) is not str or not asset_type or not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("sell tax mapping must contain nonnegative Decimal values")


def _validated_calendar(data: BacktestData, config: BacktestConfig) -> tuple[date, ...]:
    calendar = tuple(data.get_trade_calendar(config.start_date, config.end_date))
    if any(type(item) is not date or item < config.start_date or item > config.end_date for item in calendar):
        raise ValueError("trade calendar must contain in-range plain dates")
    if tuple(sorted(calendar)) != calendar or len(set(calendar)) != len(calendar):
        raise ValueError("trade calendar must be strictly ascending and unique")
    return calendar


def _validated_bars(bars: object, trade_date: date) -> Mapping[str, DailyBar | None]:
    if not isinstance(bars, Mapping):
        raise ValueError("get_bars must return a mapping")
    for symbol, bar in bars.items():
        if type(symbol) is not str or not symbol or (bar is not None and (not isinstance(bar, DailyBar) or bar.trade_date != trade_date or bar.symbol != symbol)):
            raise ValueError("session bars must have matching symbols and trade_date")
    return bars


def _validated_closes(values: object, symbol: str) -> tuple[Decimal, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError(f"historical closes for {symbol} must be a sequence")
    closes = tuple(values)
    if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 for value in closes):
        raise ValueError(f"historical closes for {symbol} must be positive finite Decimals")
    return closes


def _validated_asset_type(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("data asset type must be a nonempty plain str")
    return value


def _validated_history(values: object, symbol: str, asset_type: str, signal_date: date) -> tuple[DailyBar, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError(f"historical bars for {symbol} must be a sequence")
    history = tuple(values)
    if not history or any(not isinstance(bar, DailyBar) for bar in history):
        raise ValueError(f"historical bars for {symbol} must be nonempty DailyBar values")
    if history[-1].trade_date != signal_date or any(bar.symbol != symbol or bar.asset_type != asset_type for bar in history):
        raise ValueError(f"historical bars for {symbol} must end at the signal date")
    if any(history[index].trade_date >= history[index + 1].trade_date for index in range(len(history) - 1)):
        raise ValueError(f"historical bars for {symbol} must be ascending")
    return history


def _signal_records(assessments: Sequence[CandidateAssessment], signal_date: date, scheduled: date, ranks: Mapping[str, int]) -> tuple[SignalRecord, ...]:
    return tuple(SignalRecord(
        signal_id=f"signal-{signal_date.isoformat()}-{assessment.symbol}", signal_date=signal_date,
        symbol=assessment.symbol, eligible=assessment.candidate.eligible,
        rejection_reason=",".join(assessment.rejection_reasons) or None, risk_on=assessment.risk_on,
        liquidity_pass=assessment.liquidity, momentum_pass=assessment.momentum,
        candidate_rank=ranks.get(assessment.symbol), breakout_strength=assessment.breakout_strength,
        momentum_60=assessment.momentum_60, scheduled_trade_date=scheduled,
    ) for assessment in assessments)


def _build_result(days: Sequence[PortfolioDayResult], guards: Sequence[DailyLossGuardResult], signals: Sequence[SignalRecord], universe_exclusions: Sequence[UniverseExclusionRecord], initial_cash: Decimal) -> BacktestResult:
    orders: list[Order] = []
    fills: list[Fill] = []
    final_positions: dict[str, Position] = {}
    points: list[EquityCurvePoint] = []
    prior_nav = initial_cash
    peak_nav = initial_cash
    for day, guard in zip(days, guards, strict=True):
        nav = day.valuation.nav
        peak_nav = max(peak_nav, nav)
        points.append(EquityCurvePoint(
            trade_date=day.trade_date, cash=day.valuation.cash, market_value=day.valuation.open_market_value,
            nav_close=nav, daily_return=nav / prior_nav - Decimal("1"),
            cumulative_return=nav / initial_cash - Decimal("1"), peak_nav=peak_nav,
            drawdown=nav / peak_nav - Decimal("1"), gross_exposure=day.valuation.open_market_value / nav if nav else Decimal("0"),
            position_count=sum(position.status == "OPEN" for position in day.closing_state.positions), stale_mark_count=0,
            new_entry_blocked=not guard.entries_allowed, new_entry_block_reason=guard.reason,
        ))
        orders.extend(day.exit_run_result.orders); orders.extend(day.entry_run_result.orders)
        fills.extend(day.exit_run_result.fills); fills.extend(day.entry_run_result.fills)
        final_positions.update({position.position_id: position for position in day.closing_state.positions})
        prior_nav = nav
    return BacktestResult(tuple(days), tuple(points), tuple(orders), tuple(fills), tuple(final_positions.values()), tuple(signals), tuple(universe_exclusions))
