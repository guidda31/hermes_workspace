"""Durable per-day realized-loss ledger for the pretrade daily-loss circuit breaker.

The pretrade risk gate blocks new orders once today's realized loss exceeds a fraction
of equity — but only if it is actually told today's loss. This module is that source of
truth: an append-only per-day ledger of realized round-trip P&L, and a query that sums
today's entries into a NONNEGATIVE loss amount to feed ``AccountRiskSnapshot.daily_loss``.

Realized (not unrealized) by construction: an entry is recorded from a closing SELL, as
``(sell_price - avg_cost) * quantity`` (negative = a loss). The ledger is one JSON object
per line under ``<dir>/<YYYY-MM-DD>.jsonl``. Reads FAIL CLOSED: a present-but-corrupt
ledger raises rather than silently reporting zero loss, so the caller halts instead of
trading through an unknown state.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path

DEFAULT_DAILY_LOSS_LEDGER = "data/live-daily-loss"
DAILY_LOSS_SCHEMA_VERSION = 1


def _finite_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return value


def compute_realized_pnl(*, sell_price: Decimal, avg_cost: Decimal, quantity: int) -> Decimal:
    """Realized P&L of a closing sell: (sell_price - avg_cost) * quantity. Negative = loss."""
    _finite_decimal(sell_price, "sell_price")
    _finite_decimal(avg_cost, "avg_cost")
    if sell_price <= 0 or avg_cost < 0:
        raise ValueError("sell_price must be positive and avg_cost nonnegative")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive plain int")
    return (sell_price - avg_cost) * Decimal(quantity)


def _day_path(ledger_dir, day: str) -> Path:
    if type(day) is not str:
        raise ValueError("day must be a YYYY-MM-DD str")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("day must be a YYYY-MM-DD str") from exc
    return Path(ledger_dir) / f"{day}.jsonl"


def record_realized_trade(
    ledger_dir, *, day: str, symbol: str, quantity: int,
    sell_price: Decimal, avg_cost: Decimal, recorded_at: datetime,
) -> Decimal:
    """Append one realized-trade entry for ``day``; returns its realized P&L (neg = loss)."""
    if type(symbol) is not str or not symbol.strip():
        raise ValueError("symbol must be a nonempty plain str")
    if type(recorded_at) is not datetime or recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be a timezone-aware datetime")
    realized = compute_realized_pnl(sell_price=sell_price, avg_cost=avg_cost, quantity=quantity)

    path = _day_path(ledger_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": DAILY_LOSS_SCHEMA_VERSION,
        "day": day, "symbol": symbol, "quantity": quantity,
        "sell_price": format(sell_price.normalize(), "f"),
        "avg_cost": format(avg_cost.normalize(), "f"),
        "realized_pnl": format(realized.normalize(), "f"),
        "recorded_at": recorded_at.isoformat(),
    }
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return realized


def realized_loss_for(ledger_dir, day: str) -> Decimal:
    """Return today's realized LOSS as a nonnegative Decimal (0 if net flat/profit).

    Fail-closed: a present-but-corrupt ledger raises ``ValueError`` (the caller must halt).
    An absent ledger means no trades today -> ``Decimal("0")``.
    """
    path = _day_path(ledger_dir, day)
    if not path.exists():
        return Decimal("0")
    net = Decimal("0")
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            net += Decimal(str(json.loads(raw)["realized_pnl"]))
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError(f"corrupt daily-loss ledger for {day}; halt and inspect it") from exc
    return -net if net < 0 else Decimal("0")
