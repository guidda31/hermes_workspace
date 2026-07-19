"""Durable, restart-safe multi-session paper runner.

Composes the paper leaf modules into a daily loop a Hermes routine can drive:
recover the latest account from the ledger, honor the manual kill switch (block new
BUYs, still allow exits), simulate the session, and persist it write-once so a date
can never be double-applied. Also rebuilds persisted sessions for cross-restart
reporting. No real money, order, or network — everything runs through the paper
simulator and local files.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..backtest.engine import ExecutionCostConfig, Fill, Side
from ..contracts import DailyBar
from ..llm.decision import DecisionAction, SymbolDecision
from .kill_switch import is_kill_switch_engaged
from .ledger import list_session_records, load_latest_account, save_paper_session
from .report import PaperReport, build_paper_report
from .session import (
    PaperAccount,
    PaperPosition,
    PaperSessionResult,
    UnfilledDecision,
    simulate_paper_session,
)

_KILL_SWITCH_REASON = "KILL_SWITCH_ENGAGED"


def run_paper_session(
    *,
    session_dir: str | Path,
    kill_switch_path: str | Path,
    initial_cash: Decimal,
    decisions: Sequence[SymbolDecision],
    session_bars: Mapping[str, DailyBar | None],
    reference_close_by_symbol: Mapping[str, Decimal],
    costs: ExecutionCostConfig,
    trade_date: date,
    max_gap_up_pct: Decimal | None = None,
) -> PaperSessionResult:
    """Run and durably persist one paper session, recovering prior state.

    The account is loaded from the ledger (restart recovery); ``initial_cash`` seeds
    only the very first session. When the kill switch is engaged, BUY decisions are
    dropped and recorded as ``KILL_SWITCH_ENGAGED`` unfilled; SELL/HOLD still run.
    Persistence is write-once, so re-running an already-recorded date raises.
    """
    if type(initial_cash) is not Decimal or not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("initial_cash must be a positive finite Decimal")
    if not all(type(d) is SymbolDecision for d in decisions):
        raise ValueError("decisions must be SymbolDecision values")

    account = load_latest_account(session_dir)
    if account is None:
        account = PaperAccount(cash=initial_cash)

    blocked = is_kill_switch_engaged(kill_switch_path)
    if blocked:
        effective = tuple(d for d in decisions if d.action is not DecisionAction.BUY)
        dropped = tuple(d for d in decisions if d.action is DecisionAction.BUY)
    else:
        effective, dropped = tuple(decisions), ()

    result = simulate_paper_session(
        account,
        decisions=effective,
        session_bars=session_bars,
        reference_close_by_symbol=reference_close_by_symbol,
        costs=costs,
        trade_date=trade_date,
        max_gap_up_pct=max_gap_up_pct,
    )
    if dropped:
        result = replace(
            result,
            unfilled=result.unfilled + tuple(
                UnfilledDecision(d.symbol, "BUY", _KILL_SWITCH_REASON) for d in dropped
            ),
        )

    save_paper_session(session_dir, result)
    return result


def load_session_results(session_dir: str | Path) -> tuple[PaperSessionResult, ...]:
    """Rebuild every persisted session as a PaperSessionResult, in date order."""
    return tuple(_result_from_record(record) for record in list_session_records(session_dir))


def paper_report(session_dir: str | Path) -> PaperReport:
    """Build the cumulative performance report from persisted sessions."""
    return build_paper_report(load_session_results(session_dir))


def _result_from_record(record: Mapping[str, object]) -> PaperSessionResult:
    trade_date = date.fromisoformat(str(record["trade_date"]))
    account_raw = record["account"]
    assert isinstance(account_raw, Mapping)
    positions = tuple(
        PaperPosition(
            symbol=str(p["symbol"]),
            asset_type=str(p["asset_type"]),
            entry_price=Decimal(str(p["entry_price"])),
            quantity=int(p["quantity"]),
            entry_date=date.fromisoformat(str(p["entry_date"])),
        )
        for p in account_raw["positions"]
    )
    account = PaperAccount(cash=Decimal(str(account_raw["cash"])), positions=positions)
    fills = tuple(_fill_from_record(f, trade_date) for f in record["fills"])
    unfilled = tuple(
        UnfilledDecision(str(u["symbol"]), str(u["side"]), str(u["reason"])) for u in record["unfilled"]
    )
    return PaperSessionResult(
        trade_date=trade_date,
        account=account,
        fills=fills,
        unfilled=unfilled,
        realized_pnl=Decimal(str(record["realized_pnl"])),
        nav=Decimal(str(record["nav"])),
    )


def _fill_from_record(raw: Mapping[str, object], trade_date: date) -> Fill:
    """Reconstruct a Fill from the ledger's stored subset.

    The ledger persists the economically meaningful fields; the few it omits are
    derived exactly (notional, fixed_fee) or set to a faithful stand-in (ids,
    raw_slippage_price) that the report never reads.
    """
    fill_id = str(raw["fill_id"])
    quantity = int(raw["quantity"])
    fill_price = Decimal(str(raw["fill_price"]))
    commission = Decimal(str(raw["commission"]))
    sell_tax = Decimal(str(raw["sell_tax"]))
    total_cost = Decimal(str(raw["total_cost"]))
    return Fill(
        fill_id=fill_id, order_id=fill_id, position_id=fill_id, trade_date=trade_date,
        symbol=str(raw["symbol"]), asset_type="", side=Side(str(raw["side"])),
        quantity=quantity, reference_open=Decimal(str(raw["reference_open"])),
        raw_slippage_price=fill_price, fill_price=fill_price, notional=fill_price * Decimal(quantity),
        commission=commission, sell_tax=sell_tax, fixed_fee=total_cost - commission - sell_tax,
        total_cost=total_cost, cash_delta=Decimal(str(raw["cash_delta"])),
    )
