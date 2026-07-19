"""doc-04 §9 robustness scenarios over the baseline backtest.

Two orthogonal stress checks, both thin wrappers over ``run_backtest_from_snapshot``:

- ``run_cost_scenarios`` reruns the *same* full backtest under several named cost
  configurations (default: base vs. stress) to show whether the strategy survives
  higher slippage/commission -- not just the friction-light base case.
- ``run_walk_forward`` splits the snapshot's full trade calendar into contiguous,
  roughly-equal windows by index and runs each in isolation, to show the result
  holds across sub-periods rather than one lucky window.

RESEARCH only (non-PIT, non-survivorship): inherits every caveat of the CLI it wraps.
Pure orchestration -- no network, no mutation, fail-closed ``ValueError``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..backtest_data import load_snapshot
from .cli import default_costs, run_backtest_from_snapshot, stress_costs
from .engine import ExecutionCostConfig
from .metrics import BacktestMetrics


def run_cost_scenarios(
    *,
    snapshot_path,
    initial_cash: Decimal,
    scenarios: Mapping[str, ExecutionCostConfig] | None = None,
) -> dict[str, BacktestMetrics]:
    """Run the full backtest once per named cost scenario; return {name: metrics}.

    ``scenarios`` defaults to {"base": default_costs(), "stress": stress_costs()}.
    Every value must be an ``ExecutionCostConfig``. Raises ``ValueError`` on a
    non-positive-Decimal ``initial_cash`` or an empty/ill-typed scenarios mapping.
    """
    if type(initial_cash) is not Decimal or not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("initial_cash must be a positive finite Decimal")
    if scenarios is None:
        scenarios = {"base": default_costs(), "stress": stress_costs()}
    if not isinstance(scenarios, Mapping) or not scenarios:
        raise ValueError("scenarios must be a nonempty mapping")
    result: dict[str, BacktestMetrics] = {}
    for name, costs in scenarios.items():
        if type(name) is not str or not name:
            raise ValueError("scenario name must be a nonempty plain str")
        if type(costs) is not ExecutionCostConfig:
            raise ValueError("scenario value must be an ExecutionCostConfig")
        _, metrics = run_backtest_from_snapshot(
            snapshot_path=snapshot_path, initial_cash=initial_cash, costs=costs,
        )
        result[name] = metrics
    return result


@dataclass(frozen=True)
class WalkForwardWindow:
    """One contiguous sub-period of the walk-forward split and its metrics."""

    index: int
    start_date: date
    end_date: date
    metrics: BacktestMetrics


def run_walk_forward(
    *,
    snapshot_path,
    initial_cash: Decimal,
    num_windows: int,
    costs: ExecutionCostConfig | None = None,
) -> tuple[WalkForwardWindow, ...]:
    """Split the full trade calendar into ``num_windows`` contiguous windows and run each.

    Sessions are partitioned by index into ``num_windows`` roughly-equal, disjoint,
    contiguous blocks; each block's [start, end] dates bound one backtest via
    ``run_backtest_from_snapshot(start_date=, end_date=, costs=)``. Each window still
    sees the full prior history for its indicators (correct, no lookahead) -- only its
    own dates are eligible to generate trades.

    Raises ``ValueError`` if ``num_windows`` is not a positive int, exceeds the number
    of sessions, or ``initial_cash`` is not a positive finite Decimal.
    """
    if type(initial_cash) is not Decimal or not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("initial_cash must be a positive finite Decimal")
    if type(num_windows) is not int or num_windows < 1:
        raise ValueError("num_windows must be a positive int")
    if costs is not None and type(costs) is not ExecutionCostConfig:
        raise ValueError("costs must be an ExecutionCostConfig or None")

    calendar = tuple(load_snapshot(snapshot_path).trade_calendar)
    sessions = len(calendar)
    if num_windows > sessions:
        raise ValueError("num_windows cannot exceed the number of sessions")

    windows: list[WalkForwardWindow] = []
    for index in range(num_windows):
        lo = index * sessions // num_windows
        hi = (index + 1) * sessions // num_windows
        block = calendar[lo:hi]
        if not block:
            raise ValueError("walk-forward produced an empty window")
        start_date, end_date = block[0], block[-1]
        _, metrics = run_backtest_from_snapshot(
            snapshot_path=snapshot_path, initial_cash=initial_cash,
            start_date=start_date, end_date=end_date, costs=costs,
        )
        windows.append(WalkForwardWindow(index=index, start_date=start_date, end_date=end_date, metrics=metrics))
    return tuple(windows)
