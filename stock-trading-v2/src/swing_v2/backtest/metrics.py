"""Pure performance-metrics report over a completed ``BacktestResult``.

This is the results layer of the backtest (doc-04 §7.1): given an already-run,
immutable ``BacktestResult`` it derives the cost-inclusive summary a reader needs
to judge the strategy -- returns, drawdown, volatility/Sharpe, CAGR, trade and
win/loss statistics, exposure, and total costs. Every metric is a pure function of
the in-memory result: nothing is simulated, mutated, or fetched. Decimal-only.

Empty ``equity_curve`` is rejected: a report needs at least one session
(fail-closed ``ValueError``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .backtest_engine import BacktestResult, EquityCurvePoint
from .engine import Fill, Position


@dataclass(frozen=True)
class BacktestMetrics:
    starting_nav: Decimal
    ending_nav: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    max_drawdown_peak_date: date
    max_drawdown_trough_date: date
    daily_volatility: Decimal
    annualized_sharpe: Decimal | None
    cagr: Decimal | None
    total_fills: int
    filled_orders: int
    canceled_orders: int
    closed_round_trips: int
    win_rate: Decimal
    average_win: Decimal
    average_loss: Decimal
    profit_factor: Decimal | None
    average_holding_sessions: Decimal
    max_concurrent_positions: int
    average_position_count: Decimal
    total_costs: Decimal
    stale_mark_days: int


def build_backtest_metrics(
    result: BacktestResult, *, initial_cash: Decimal, annualization_days: int = 252,
) -> BacktestMetrics:
    """Summarize a completed backtest into a cost-inclusive metrics report.

    Raises ``ValueError`` on a non-result, non-positive ``initial_cash``,
    non-positive ``annualization_days``, or an empty ``equity_curve``.
    """
    if type(result) is not BacktestResult:
        raise ValueError("result must be a BacktestResult")
    if type(initial_cash) is not Decimal or not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("initial_cash must be a positive finite Decimal")
    if type(annualization_days) is not int or annualization_days < 1:
        raise ValueError("annualization_days must be a positive int")

    curve = result.equity_curve
    if len(curve) == 0:
        raise ValueError("a backtest metrics report needs at least one session")
    for point in curve:
        if type(point) is not EquityCurvePoint:
            raise ValueError("equity_curve must contain EquityCurvePoint values")
        if not point.nav_close.is_finite() or not point.daily_return.is_finite() or not point.drawdown.is_finite():
            raise ValueError("equity_curve values must be finite Decimals")

    starting_nav = initial_cash
    ending_nav = curve[-1].nav_close
    total_return = ending_nav / starting_nav - Decimal("1")

    max_drawdown, peak_date, trough_date = _drawdown_range(curve)
    daily_volatility, annualized_sharpe = _volatility_sharpe(curve, annualization_days)
    cagr = _cagr(starting_nav, ending_nav, len(curve), annualization_days)

    filled_orders = sum(1 for order in result.orders if order.status == "FILLED")
    canceled_orders = sum(1 for order in result.orders if order.status.startswith("CANCELED"))

    (win_rate, average_win, average_loss, profit_factor,
     closed_round_trips, average_holding_sessions) = _closed_position_stats(
        result.positions, result.fills,
    )

    position_counts = [point.position_count for point in curve]
    average_position_count = sum(
        (Decimal(count) for count in position_counts), Decimal("0"),
    ) / Decimal(len(position_counts))
    total_costs = sum((fill.total_cost for fill in result.fills), Decimal("0"))

    return BacktestMetrics(
        starting_nav=starting_nav, ending_nav=ending_nav, total_return=total_return,
        max_drawdown=max_drawdown, max_drawdown_peak_date=peak_date,
        max_drawdown_trough_date=trough_date, daily_volatility=daily_volatility,
        annualized_sharpe=annualized_sharpe, cagr=cagr,
        total_fills=len(result.fills), filled_orders=filled_orders,
        canceled_orders=canceled_orders, closed_round_trips=closed_round_trips,
        win_rate=win_rate, average_win=average_win, average_loss=average_loss,
        profit_factor=profit_factor, average_holding_sessions=average_holding_sessions,
        max_concurrent_positions=max(position_counts),
        average_position_count=average_position_count, total_costs=total_costs,
        stale_mark_days=sum(1 for point in curve if point.stale_mark_count > 0),
    )


def _drawdown_range(curve: tuple[EquityCurvePoint, ...]) -> tuple[Decimal, date, date]:
    """Return (max_drawdown, peak_date, trough_date) from the recorded drawdowns."""
    trough_index = min(range(len(curve)), key=lambda i: curve[i].drawdown)
    trough = curve[trough_index]
    peak_date = trough.trade_date
    for i in range(trough_index, -1, -1):
        if curve[i].nav_close == trough.peak_nav:
            peak_date = curve[i].trade_date
            break
    return trough.drawdown, peak_date, trough.trade_date


def _volatility_sharpe(
    curve: tuple[EquityCurvePoint, ...], annualization_days: int,
) -> tuple[Decimal, Decimal | None]:
    """Sample-stdev daily volatility and annualized Sharpe (risk-free 0)."""
    returns = [point.daily_return for point in curve]
    n = len(returns)
    if n < 2:
        return Decimal("0"), None
    mean = sum(returns, Decimal("0")) / Decimal(n)
    variance = sum(((r - mean) ** 2 for r in returns), Decimal("0")) / Decimal(n - 1)
    stdev = variance.sqrt()
    if stdev == 0:
        return stdev, None
    sharpe = (mean / stdev) * Decimal(annualization_days).sqrt()
    return stdev, sharpe


def _cagr(
    starting_nav: Decimal, ending_nav: Decimal, sessions: int, annualization_days: int,
) -> Decimal | None:
    """CAGR from NAV growth; None if the curve spans fewer than annualization_days.

    The fractional power ``ratio ** (annualization_days / sessions)`` has no exact
    Decimal form, so this makes the report's single, deliberate float conversion:
    the ratio and exponent go through float only for the power, and the result is
    brought straight back to Decimal.
    """
    if sessions < annualization_days:
        return None
    ratio = ending_nav / starting_nav
    powered = Decimal(str(float(ratio) ** (annualization_days / sessions)))
    return powered - Decimal("1")


def _closed_position_stats(
    positions: tuple[Position, ...], fills: tuple[Fill, ...],
) -> tuple[Decimal, Decimal, Decimal, Decimal | None, int, Decimal]:
    """Win rate, avg win/loss, profit factor, count, and avg holding over CLOSED positions.

    Per-position net P&L is the sum of the entry fill ``cash_delta`` (negative) and
    the exit fill ``cash_delta`` (positive), looked up by fill id -- already net of
    every cost. profit_factor is None when there are no losers (never infinity).
    """
    fills_by_id = {fill.fill_id: fill for fill in fills}
    closed = [p for p in positions if p.status == "CLOSED"]
    wins: list[Decimal] = []
    losses: list[Decimal] = []
    for position in closed:
        entry = fills_by_id.get(position.entry_fill_id)
        exit_fill = fills_by_id.get(position.exit_fill_id) if position.exit_fill_id is not None else None
        if entry is None or exit_fill is None:
            raise ValueError("closed position must reference existing entry and exit fills")
        net = entry.cash_delta + exit_fill.cash_delta
        if net > 0:
            wins.append(net)
        elif net < 0:
            losses.append(net)

    closed_count = len(closed)
    win_rate = Decimal(len(wins)) / Decimal(closed_count) if closed_count else Decimal("0")
    average_win = sum(wins, Decimal("0")) / Decimal(len(wins)) if wins else Decimal("0")
    average_loss = sum(losses, Decimal("0")) / Decimal(len(losses)) if losses else Decimal("0")
    profit_factor = (
        sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0"))) if losses else None
    )
    average_holding_sessions = (
        sum((Decimal(p.age_sessions) for p in closed), Decimal("0")) / Decimal(closed_count)
        if closed_count else Decimal("0")
    )
    return win_rate, average_win, average_loss, profit_factor, closed_count, average_holding_sessions
