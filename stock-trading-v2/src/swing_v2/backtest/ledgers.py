"""Serialize a completed ``BacktestResult`` to the doc-04 §7 output-contract ledgers.

Writes ``equity_curve.csv`` (§7.1) and the ``orders`` / ``fills`` / ``positions`` /
``signals`` ledgers (§7.2) as CSV, plus ``run_summary.json`` — together the auditable
record of one backtest run.

Encoding conventions: Decimals as canonical strings (never float); dates as ISO-8601;
the ``Side`` enum as its value; booleans as ``"true"`` / ``"false"``; ``None`` as an
empty field.

Omitted §7.2 columns. The serialized ``BacktestResult`` dataclasses do not carry these,
and this module never fabricates values, so each is omitted (not written) rather than
guessed:

- orders:    ``run_id``, ``submitted_at_phase``, ``candidate_rank``, ``risk_on``,
             ``breakout_strength``, ``momentum_60``, ``risk_budget``,
             ``execution_config_id``, ``data_snapshot_hash`` — run-level identifiers,
             execution-config context, or signal-side attributes that ``Order`` does not
             hold (they live on ``SignalRecord`` / the run config, not the order).
- fills:     ``run_id``, ``slippage_bps``, ``slippage_cost``, ``tick_rounding_rule`` —
             run-level id and cost-config artifacts absent from ``Fill``.
- positions: ``run_id``, ``entry_signal_date``, ``entry_trade_date``,
             ``exit_signal_date``, ``exit_trade_date``, ``gross_pnl``, ``total_costs``,
             ``net_realized_pnl``, ``net_return``, ``last_mark_date``,
             ``last_mark_price``, ``unrealized_pnl`` — run-level id plus derived P&L /
             mark / date-join fields not present on ``Position``.

``equity_curve`` (§7.1) and ``signals`` (§7.2) carry every spec column.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from .backtest_engine import BacktestResult, EquityCurvePoint, SignalRecord
from .engine import Fill, Order, Position


def _dec(value: Decimal) -> str:
    """Canonical Decimal-as-string; ``type``-strict, never float."""
    if type(value) is not Decimal:
        raise ValueError("expected a Decimal value")
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _opt_dec(value: Decimal | None) -> str:
    return "" if value is None else _dec(value)


def _bool(value: bool) -> str:
    if type(value) is not bool:
        raise ValueError("expected a bool value")
    return "true" if value else "false"


def _iso(value: date) -> str:
    if type(value) is not date:
        raise ValueError("expected a date value")
    return value.isoformat()


def _opt_iso(value: date | None) -> str:
    return "" if value is None else _iso(value)


def _opt_str(value: str | None) -> str:
    return "" if value is None else value


def _opt_int(value: int | None) -> str:
    return "" if value is None else str(value)


EQUITY_HEADER = [
    "trade_date", "cash", "market_value", "nav_close", "daily_return",
    "cumulative_return", "peak_nav", "drawdown", "gross_exposure",
    "position_count", "stale_mark_count", "new_entry_blocked",
    "new_entry_block_reason",
]

ORDERS_HEADER = [
    "order_id", "signal_id", "position_id", "symbol", "asset_type", "side",
    "signal_date", "scheduled_trade_date", "status", "intent_reason",
    "requested_quantity", "filled_quantity", "unfilled_quantity", "unfilled_reason",
]

FILLS_HEADER = [
    "fill_id", "order_id", "position_id", "trade_date", "symbol", "side", "quantity",
    "reference_open", "raw_slippage_price", "fill_price", "notional", "commission",
    "sell_tax", "fixed_fee", "total_cost", "cash_delta", "asset_type",
]

POSITIONS_HEADER = [
    "position_id", "symbol", "asset_type", "entry_order_id", "entry_fill_id",
    "entry_price", "initial_stop_price", "entry_quantity", "exit_order_id",
    "exit_fill_id", "exit_price", "exit_reason", "age_sessions", "status",
]

SIGNALS_HEADER = [
    "signal_id", "signal_date", "symbol", "eligible", "rejection_reason",
    "risk_on", "liquidity_pass", "momentum_pass", "candidate_rank",
    "breakout_strength", "momentum_60", "scheduled_trade_date",
]


def _equity_row(point: EquityCurvePoint) -> list[str]:
    return [
        _iso(point.trade_date), _dec(point.cash), _dec(point.market_value),
        _dec(point.nav_close), _dec(point.daily_return), _dec(point.cumulative_return),
        _dec(point.peak_nav), _dec(point.drawdown), _dec(point.gross_exposure),
        str(point.position_count), str(point.stale_mark_count),
        _bool(point.new_entry_blocked), _opt_str(point.new_entry_block_reason),
    ]


def _order_row(order: Order) -> list[str]:
    return [
        order.order_id, order.signal_id, _opt_str(order.position_id), order.symbol,
        order.asset_type, order.side.value, _iso(order.signal_date),
        _opt_iso(order.scheduled_trade_date), order.status, order.intent_reason,
        str(order.requested_quantity), str(order.filled_quantity),
        str(order.unfilled_quantity), _opt_str(order.unfilled_reason),
    ]


def _fill_row(fill: Fill) -> list[str]:
    return [
        fill.fill_id, fill.order_id, fill.position_id, _iso(fill.trade_date),
        fill.symbol, fill.side.value, str(fill.quantity), _dec(fill.reference_open),
        _dec(fill.raw_slippage_price), _dec(fill.fill_price), _dec(fill.notional),
        _dec(fill.commission), _dec(fill.sell_tax), _dec(fill.fixed_fee),
        _dec(fill.total_cost), _dec(fill.cash_delta), fill.asset_type,
    ]


def _position_row(position: Position) -> list[str]:
    return [
        position.position_id, position.symbol, position.asset_type,
        position.entry_order_id, position.entry_fill_id, _dec(position.entry_price),
        _dec(position.initial_stop_price), str(position.quantity),
        _opt_str(position.exit_order_id), _opt_str(position.exit_fill_id),
        _opt_dec(position.exit_price), _opt_str(position.exit_reason),
        str(position.age_sessions), position.status,
    ]


def _signal_row(signal: SignalRecord) -> list[str]:
    return [
        signal.signal_id, _iso(signal.signal_date), signal.symbol,
        _bool(signal.eligible), _opt_str(signal.rejection_reason),
        _bool(signal.risk_on), _bool(signal.liquidity_pass), _bool(signal.momentum_pass),
        _opt_int(signal.candidate_rank), _opt_dec(signal.breakout_strength),
        _opt_dec(signal.momentum_60), _opt_iso(signal.scheduled_trade_date),
    ]


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    """Write a header row plus one row per record; header-only when empty."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_backtest_ledgers(result: BacktestResult, output_dir: str | Path) -> dict[str, Path]:
    """Serialize ``result`` to the doc-04 §7.1/§7.2 CSV ledgers under ``output_dir``."""
    if type(result) is not BacktestResult:
        raise ValueError("result must be a BacktestResult")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    plan = (
        ("equity_curve", EQUITY_HEADER, [_equity_row(p) for p in result.equity_curve]),
        ("orders", ORDERS_HEADER, [_order_row(o) for o in result.orders]),
        ("fills", FILLS_HEADER, [_fill_row(f) for f in result.fills]),
        ("positions", POSITIONS_HEADER, [_position_row(p) for p in result.positions]),
        ("signals", SIGNALS_HEADER, [_signal_row(s) for s in result.signals]),
    )
    paths: dict[str, Path] = {}
    for name, header, rows in plan:
        path = directory / f"{name}.csv"
        _write_csv(path, header, rows)
        paths[name] = path
    return paths


def write_run_summary(
    result: BacktestResult, output_dir: str | Path, *, config_summary: dict
) -> Path:
    """Write ``run_summary.json`` with record counts and the injected ``config_summary``."""
    if type(result) is not BacktestResult:
        raise ValueError("result must be a BacktestResult")
    if type(config_summary) is not dict:
        raise ValueError("config_summary must be a dict")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    orders_by_status: dict[str, int] = {}
    for order in result.orders:
        orders_by_status[order.status] = orders_by_status.get(order.status, 0) + 1

    payload = {
        "counts": {
            "sessions": len(result.all_day_results),
            "orders": len(result.orders),
            "orders_by_status": orders_by_status,
            "fills": len(result.fills),
            "positions": len(result.positions),
            "signals": len(result.signals),
        },
        "config_summary": config_summary,
    }
    path = directory / "run_summary.json"
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path
