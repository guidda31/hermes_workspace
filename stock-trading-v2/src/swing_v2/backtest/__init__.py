"""Pure, in-memory backtest v0 vertical slice."""

from .engine import (
    ExecutionCostConfig,
    Fill,
    Order,
    Position,
    RunResult,
    Side,
    run_single_position_backtest,
    run_two_day_backtest,
)

from .candidates import Candidate, CandidateSelection, select_entry_candidates
from .close_time_candidates import (
    NEUTRAL_SCORE,
    CandidateAssessment,
    assess_close_time_candidates,
)
from .universe_assessment import UniverseCandidateAssessment, assess_eligible_close_time_candidates
from .position_sizing import (
    calculate_entry_quantity,
    calculate_entry_quantity_for_fill_price,
    validate_entry_sizing_inputs,
)
from .entry_planning import EntryCandidate, EntryPlan, create_entry_plans
from .entry_execution import execute_entry_plans_ioc
from .exit_evaluation import ExitEvaluationResult, ExitIntent, evaluate_exit_signals
from .exit_execution import execute_exit_intents_ioc
from .portfolio_valuation import PortfolioValuation, mark_to_market
from .portfolio_day import PortfolioDayResult, run_portfolio_day
from .portfolio_state import (
    PortfolioState,
    apply_entry_execution,
    apply_exit_execution,
    create_portfolio_state,
)
from .daily_loss_guard import (
    DailyLossGuardConfig,
    DailyLossGuardInput,
    DailyLossGuardResult,
    evaluate_daily_loss_guard,
)
from .backtest_engine import (
    BacktestConfig,
    BacktestData,
    BacktestResult,
    BacktestRiskConfig,
    BacktestRunner,
    EquityCurvePoint,
    SignalRecord,
    UniverseExclusionRecord,
)

__all__ = [
    "Candidate",
    "CandidateAssessment",
    "UniverseCandidateAssessment",
    "CandidateSelection",
    "EntryCandidate",
    "EntryPlan",
    "ExitEvaluationResult",
    "ExitIntent",
    "DailyLossGuardConfig",
    "DailyLossGuardInput",
    "DailyLossGuardResult",
    "BacktestConfig",
    "BacktestData",
    "BacktestResult",
    "BacktestRiskConfig",
    "BacktestRunner",
    "EquityCurvePoint",
    "SignalRecord",
    "UniverseExclusionRecord",
    "NEUTRAL_SCORE",
    "ExecutionCostConfig",
    "Fill",
    "Order",
    "Position",
    "PortfolioState",
    "PortfolioValuation",
    "PortfolioDayResult",
    "RunResult",
    "Side",
    "calculate_entry_quantity",
    "calculate_entry_quantity_for_fill_price",
    "assess_close_time_candidates",
    "assess_eligible_close_time_candidates",
    "validate_entry_sizing_inputs",
    "create_entry_plans",
    "create_portfolio_state",
    "execute_entry_plans_ioc",
    "execute_exit_intents_ioc",
    "apply_entry_execution",
    "apply_exit_execution",
    "evaluate_daily_loss_guard",
    "evaluate_exit_signals",
    "mark_to_market",
    "run_portfolio_day",
    "run_single_position_backtest",
    "run_two_day_backtest",
    "select_entry_candidates",
]
