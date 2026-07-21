"""Auto-record realized loss from filled SELL orders.

Realized P&L needs the pre-sale cost basis (the position's average purchase price, which
vanishes from the balance once the position closes) AND the actual fill price (which lags
the order). So we CAPTURE the cost basis when a SELL is placed (``record_pending_sell``)
and SETTLE it once the fill is visible (``settle``): realized = (fill_price - avg_cost) *
filled_qty, then appended to the daily-loss ledger by the caller. Only fully-FILLED sells
settle; anything still open stays pending. Reads fail closed on a corrupt file.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .daily_loss import compute_realized_pnl
from .pilot_reconcile import STATUS_FILLED, OrderReconciliation

DEFAULT_PENDING_SELLS = "data/live-pending-sells.jsonl"


@dataclass(frozen=True)
class PendingSell:
    order_number: str
    symbol: str
    quantity: int
    avg_cost: Decimal
    submitted_at: str


@dataclass(frozen=True)
class SettledSell:
    symbol: str
    quantity: int
    sell_price: Decimal
    avg_cost: Decimal
    realized_pnl: Decimal


def record_pending_sell(
    pending_path, *, order_number: str, symbol: str, quantity: int, avg_cost: Decimal, at: datetime,
) -> None:
    """Append a placed SELL's cost basis so its realized P&L can be settled after the fill."""
    if type(order_number) is not str or not order_number.strip():
        raise ValueError("order_number must be a nonempty plain str")
    if type(symbol) is not str or not symbol.strip():
        raise ValueError("symbol must be a nonempty plain str")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive plain int")
    if type(avg_cost) is not Decimal or not avg_cost.is_finite() or avg_cost < 0:
        raise ValueError("avg_cost must be a nonnegative finite Decimal")
    if type(at) is not datetime or at.tzinfo is None:
        raise ValueError("at must be a timezone-aware datetime")
    path = Path(pending_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"order_number": order_number, "symbol": symbol, "quantity": quantity,
                       "avg_cost": format(avg_cost.normalize(), "f"), "submitted_at": at.isoformat()},
                      ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def load_pending_sells(pending_path) -> tuple[PendingSell, ...]:
    """Load outstanding pending sells. Absent file -> (); corrupt -> raise (fail-closed)."""
    path = Path(pending_path)
    if not path.exists():
        return ()
    out: list[PendingSell] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            obj = json.loads(raw)
            out.append(PendingSell(
                order_number=str(obj["order_number"]), symbol=str(obj["symbol"]),
                quantity=int(obj["quantity"]), avg_cost=Decimal(str(obj["avg_cost"])),
                submitted_at=str(obj["submitted_at"]),
            ))
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("pending-sells file is corrupt; halt and inspect it") from exc
    return tuple(out)


def rewrite_pending_sells(pending_path, pending: Sequence[PendingSell]) -> None:
    """Overwrite the pending-sells file with the still-outstanding entries (settled removed)."""
    path = Path(pending_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"order_number": p.order_number, "symbol": p.symbol, "quantity": p.quantity,
                    "avg_cost": format(p.avg_cost.normalize(), "f"), "submitted_at": p.submitted_at},
                   ensure_ascii=False, sort_keys=True)
        for p in pending
    ]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def settle(
    pending: Sequence[PendingSell], reconciliations: Mapping[str, OrderReconciliation],
) -> tuple[list[SettledSell], list[PendingSell]]:
    """Split pending sells into (settled, still-pending) using per-order reconciliations.

    A pending sell settles only when its order is fully FILLED with a known fill price;
    realized P&L = (fill_price - avg_cost) * filled_quantity. Anything else stays pending.
    """
    settled: list[SettledSell] = []
    remaining: list[PendingSell] = []
    for entry in pending:
        recon = reconciliations.get(entry.order_number)
        if recon is not None and recon.status == STATUS_FILLED and recon.average_fill_price is not None:
            realized = compute_realized_pnl(
                sell_price=recon.average_fill_price, avg_cost=entry.avg_cost, quantity=recon.filled_quantity)
            settled.append(SettledSell(entry.symbol, recon.filled_quantity,
                                       recon.average_fill_price, entry.avg_cost, realized))
        else:
            remaining.append(entry)
    return settled, remaining
