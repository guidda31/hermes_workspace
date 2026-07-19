"""Pure P&L / performance report over a sequence of paper-trading sessions.

Given the ordered ``PaperSessionResult`` objects produced by repeated paper sessions,
this computes an equity curve, total return, max drawdown, realized-P&L and cost sums,
and win/loss session counts. Every metric is a pure function of the in-memory results:
no session is simulated, no account mutated, no network or broker touched. Decimal-only.

Empty input is rejected: a report needs at least one session (fail-closed ValueError).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..backtest.engine import Fill
from .session import PaperSessionResult


@dataclass(frozen=True)
class PaperReport:
    equity_curve: tuple[tuple[date, Decimal], ...]
    starting_nav: Decimal
    ending_nav: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    total_realized_pnl: Decimal
    total_costs: Decimal
    session_count: int
    fill_count: int
    winning_sessions: int
    losing_sessions: int


def build_paper_report(results: Sequence[PaperSessionResult]) -> PaperReport:
    """Summarize paper sessions into an equity/P&L report; raise on empty or unsorted input."""
    if not all(type(r) is PaperSessionResult for r in results):
        raise ValueError("results must be PaperSessionResult values")
    if len(results) == 0:
        raise ValueError("a paper report needs at least one session")

    previous: date | None = None
    for result in results:
        if previous is not None and result.trade_date <= previous:
            raise ValueError("results must be strictly ascending by trade_date")
        previous = result.trade_date
        if type(result.nav) is not Decimal or not result.nav.is_finite():
            raise ValueError("session nav must be a finite Decimal")
        if type(result.realized_pnl) is not Decimal or not result.realized_pnl.is_finite():
            raise ValueError("session realized_pnl must be a finite Decimal")

    equity_curve = tuple((r.trade_date, r.nav) for r in results)
    starting_nav = results[0].nav
    ending_nav = results[-1].nav
    if not starting_nav.is_finite() or starting_nav <= 0:
        raise ValueError("starting nav must be a positive finite Decimal")
    total_return = ending_nav / starting_nav - Decimal("1")

    max_drawdown = Decimal("0")
    running_peak = starting_nav
    for _, nav in equity_curve:
        if nav > running_peak:
            running_peak = nav
        drawdown = nav / running_peak - Decimal("1")
        if drawdown < max_drawdown:
            max_drawdown = drawdown

    fills: tuple[Fill, ...] = tuple(f for r in results for f in r.fills)
    total_realized_pnl = sum((r.realized_pnl for r in results), Decimal("0"))
    total_costs = sum((f.total_cost for f in fills), Decimal("0"))
    winning_sessions = sum(1 for r in results if r.realized_pnl > 0)
    losing_sessions = sum(1 for r in results if r.realized_pnl < 0)

    return PaperReport(
        equity_curve=equity_curve,
        starting_nav=starting_nav,
        ending_nav=ending_nav,
        total_return=total_return,
        max_drawdown=max_drawdown,
        total_realized_pnl=total_realized_pnl,
        total_costs=total_costs,
        session_count=len(results),
        fill_count=len(fills),
        winning_sessions=winning_sessions,
        losing_sessions=losing_sessions,
    )
