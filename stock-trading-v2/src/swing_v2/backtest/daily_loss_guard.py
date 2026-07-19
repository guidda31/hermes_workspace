"""Pure daily-loss guard for new-entry decisions."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class DailyLossGuardInput:
    """The current day's P&L components measured from opening equity."""

    day_start_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal

    def __post_init__(self) -> None:
        _validate_positive_finite_decimal(self.day_start_equity, "day_start_equity")
        _validate_finite_decimal(self.realized_pnl, "realized_pnl")
        _validate_finite_decimal(self.unrealized_pnl, "unrealized_pnl")


@dataclass(frozen=True)
class DailyLossGuardConfig:
    """Configurable maximum loss fraction at which new entries stop."""

    max_daily_loss_pct: Decimal

    def __post_init__(self) -> None:
        _validate_finite_decimal(self.max_daily_loss_pct, "max_daily_loss_pct")
        if not 0 < self.max_daily_loss_pct < 1:
            raise ValueError("max_daily_loss_pct must be greater than 0 and less than 1")


@dataclass(frozen=True)
class DailyLossGuardResult:
    """Computed daily P&L and whether initiating new positions is permitted."""

    entries_allowed: bool
    reason: str | None
    daily_pnl: Decimal
    daily_return: Decimal


def evaluate_daily_loss_guard(
    inputs: DailyLossGuardInput, config: DailyLossGuardConfig
) -> DailyLossGuardResult:
    """Block new entries when the daily return reaches the configured loss limit."""
    if not isinstance(inputs, DailyLossGuardInput):
        raise ValueError("inputs must be a DailyLossGuardInput")
    if not isinstance(config, DailyLossGuardConfig):
        raise ValueError("config must be a DailyLossGuardConfig")

    daily_pnl = inputs.realized_pnl + inputs.unrealized_pnl
    daily_return = daily_pnl / inputs.day_start_equity
    if daily_return <= -config.max_daily_loss_pct:
        return DailyLossGuardResult(
            entries_allowed=False,
            reason="daily loss limit reached",
            daily_pnl=daily_pnl,
            daily_return=daily_return,
        )
    return DailyLossGuardResult(
        entries_allowed=True,
        reason=None,
        daily_pnl=daily_pnl,
        daily_return=daily_return,
    )


def _validate_finite_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


def _validate_positive_finite_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")
