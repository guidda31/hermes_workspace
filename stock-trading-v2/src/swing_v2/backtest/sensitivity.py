"""One-at-a-time parameter sensitivity sweeps for the backtest (doc-02 §6).

Holding a base risk config fixed, each sweep re-runs the backtest with exactly one
parameter overridden. If a headline result only survives at a single hand-picked
value, that fragility shows up here rather than hiding behind one lucky combination.
Only fields already exposed on ``BacktestRiskConfig`` are sweepable; liquidity and
participation thresholds live in the signal layer and would need the same
parametrization (as the regime window received) before they can be swept.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from decimal import Decimal

from .backtest_engine import BacktestRiskConfig
from .cli import default_risk, run_backtest_from_snapshot
from .metrics import BacktestMetrics

_RISK_FIELDS = frozenset(f.name for f in fields(BacktestRiskConfig))


@dataclass(frozen=True)
class SensitivityPoint:
    parameter: str
    value: str
    metrics: BacktestMetrics


def default_sensitivity_grid() -> dict[str, list]:
    """The doc-02 §6 sweep grid over config-exposed parameters."""
    return {
        "initial_stop_pct": [Decimal("0.06"), Decimal("0.08"), Decimal("0.10")],
        "max_positions": [3, 5, 8],
        "max_gap_up_pct": [Decimal("0.03"), Decimal("0.05"), Decimal("0.07")],
        "max_position_notional_pct": [Decimal("0.10"), Decimal("0.20"), Decimal("0.30")],
        "risk_per_position": [Decimal("0.005"), Decimal("0.01"), Decimal("0.02")],
    }


def run_parameter_sweep(
    *,
    snapshot_path,
    initial_cash: Decimal,
    grid: Mapping[str, Sequence],
    base_risk: BacktestRiskConfig | None = None,
) -> tuple[SensitivityPoint, ...]:
    """Return one SensitivityPoint per (parameter, value), overriding one at a time."""
    if not isinstance(grid, Mapping) or not grid:
        raise ValueError("grid must be a nonempty mapping of parameter -> values")
    unknown = set(grid) - _RISK_FIELDS
    if unknown:
        raise ValueError(f"grid has non-BacktestRiskConfig parameters: {sorted(unknown)}")
    base = base_risk if base_risk is not None else default_risk()
    if not isinstance(base, BacktestRiskConfig):
        raise ValueError("base_risk must be a BacktestRiskConfig")

    points: list[SensitivityPoint] = []
    for parameter, values in grid.items():
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
            raise ValueError(f"grid values for {parameter} must be a nonempty sequence")
        for value in values:
            risk = replace(base, **{parameter: value})
            _, metrics = run_backtest_from_snapshot(
                snapshot_path=snapshot_path, initial_cash=initial_cash, risk=risk,
            )
            points.append(SensitivityPoint(parameter=parameter, value=str(value), metrics=metrics))
    return tuple(points)
