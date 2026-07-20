"""Read-only reconciliation of a placed pilot order against broker state.

Given a single order identity (order number plus symbol), this compares the
broker's still-resting open orders against its recorded fills and reports
whether the order filled, partially filled, is still resting, or is not visible
anywhere.  It never places, amends, or cancels an order: it only reads the
typed models produced by ``production_reconciliation`` and compares them.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .production_reconciliation import OpenOrder, OrderFill

STATUS_FILLED = "FILLED"
STATUS_PARTIAL = "PARTIAL"
STATUS_OPEN = "OPEN"
STATUS_NOT_FOUND = "NOT_FOUND"

_STATUSES = frozenset({STATUS_FILLED, STATUS_PARTIAL, STATUS_OPEN, STATUS_NOT_FOUND})


@dataclass(frozen=True)
class OrderReconciliation:
    order_number: str
    symbol: str
    filled_quantity: int
    open_quantity: int
    status: str

    def __post_init__(self) -> None:
        if type(self.order_number) is not str or not self.order_number:
            raise ValueError("order_number must be a nonempty str")
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("symbol must be a nonempty str")
        if type(self.filled_quantity) is not int or self.filled_quantity < 0:
            raise ValueError("filled_quantity must be a nonnegative int")
        if type(self.open_quantity) is not int or self.open_quantity < 0:
            raise ValueError("open_quantity must be a nonnegative int")
        if self.status not in _STATUSES:
            raise ValueError("status must be a known reconciliation status")


def _plain_identifier(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{name} must be a nonempty plain str")
    return value


def _typed_sequence(value: object, expected: type, name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    items = tuple(value)
    if not all(type(item) is expected for item in items):
        raise ValueError(f"{name} must contain only {expected.__name__} values")
    return items


def reconcile_order(
    *,
    order_number: str,
    symbol: str,
    open_orders: Sequence[OpenOrder],
    fills: Sequence[OrderFill],
) -> OrderReconciliation:
    """Pure comparison of one order identity against broker reads (no network).

    ``filled_quantity`` sums the recorded fill quantities for the matching
    order; ``open_quantity`` sums the still-unfilled quantity resting on the
    matching open orders (order quantity minus already-filled quantity).
    """
    number = _plain_identifier(order_number, "order_number")
    ticker = _plain_identifier(symbol, "symbol")
    orders = _typed_sequence(open_orders, OpenOrder, "open_orders")
    recorded = _typed_sequence(fills, OrderFill, "fills")

    matched = False
    filled_quantity = 0
    for entry in recorded:
        if entry.order_number == number and entry.symbol == ticker:
            matched = True
            filled_quantity += entry.filled_quantity

    open_quantity = 0
    for entry in orders:
        if entry.order_number == number and entry.symbol == ticker:
            matched = True
            open_quantity += entry.quantity - entry.filled_quantity

    if filled_quantity > 0 and open_quantity == 0:
        status = STATUS_FILLED
    elif filled_quantity > 0 and open_quantity > 0:
        status = STATUS_PARTIAL
    elif filled_quantity == 0 and open_quantity > 0:
        status = STATUS_OPEN
    elif matched:
        # Matched somewhere but nothing filled and nothing resting: no evidence
        # of a live or completed order, so fail closed to NOT_FOUND.
        status = STATUS_NOT_FOUND
    else:
        status = STATUS_NOT_FOUND

    return OrderReconciliation(number, ticker, filled_quantity, open_quantity, status)


def reconcile_via_client(
    recon_client: object,
    *,
    order_number: str,
    symbol: str,
    start: str,
    end: str,
) -> OrderReconciliation:
    """Read open orders and daily fills through ``recon_client`` then reconcile.

    ``recon_client`` must expose the read-only reader methods of
    ``KisProductionReconciliationClient``: ``read_open_orders()`` and
    ``read_daily_order_fills(start, end)``.  No order is ever mutated.
    """
    open_orders = recon_client.read_open_orders()
    fills = recon_client.read_daily_order_fills(start, end)
    return reconcile_order(
        order_number=order_number,
        symbol=symbol,
        open_orders=open_orders,
        fills=fills,
    )
