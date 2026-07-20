"""TDD contract for read-only pilot-order reconciliation."""
from __future__ import annotations

from decimal import Decimal
import unittest

from swing_v2.live.intent import Side
from swing_v2.live.production_reconciliation import OpenOrder, OrderFill
from swing_v2.live.pilot_reconcile import (
    STATUS_FILLED,
    STATUS_NOT_FOUND,
    STATUS_OPEN,
    STATUS_PARTIAL,
    OrderReconciliation,
    reconcile_order,
    reconcile_via_client,
)

_ORGNO = "001"
_SYMBOL = "005930"
_NUMBER = "100"


def open_order(*, number: str = _NUMBER, symbol: str = _SYMBOL, quantity: int = 3, filled: int = 0) -> OpenOrder:
    return OpenOrder(_ORGNO, number, symbol, Side.BUY, quantity, Decimal("71000"), filled)


def fill(*, number: str = _NUMBER, symbol: str = _SYMBOL, quantity: int = 3, filled: int = 3) -> OrderFill:
    return OrderFill(_ORGNO, number, symbol, Side.BUY, quantity, filled, Decimal("71000"))


class FakeReconClient:
    def __init__(self, *, open_orders: tuple[OpenOrder, ...], fills: tuple[OrderFill, ...]) -> None:
        self._open_orders = open_orders
        self._fills = fills
        self.fill_range: tuple[str, str] | None = None

    def read_open_orders(self) -> tuple[OpenOrder, ...]:
        return self._open_orders

    def read_daily_order_fills(self, start_date: str, end_date: str) -> tuple[OrderFill, ...]:
        self.fill_range = (start_date, end_date)
        return self._fills


class ReconcileOrderTest(unittest.TestCase):
    def test_fully_filled_reports_filled(self) -> None:
        result = reconcile_order(
            order_number=_NUMBER, symbol=_SYMBOL,
            open_orders=(), fills=(fill(quantity=3, filled=3),),
        )
        self.assertEqual(result, OrderReconciliation(_NUMBER, _SYMBOL, 3, 0, STATUS_FILLED))

    def test_partial_fill_reports_partial(self) -> None:
        result = reconcile_order(
            order_number=_NUMBER, symbol=_SYMBOL,
            open_orders=(open_order(quantity=3, filled=1),),
            fills=(fill(quantity=3, filled=1),),
        )
        self.assertEqual(result, OrderReconciliation(_NUMBER, _SYMBOL, 1, 2, STATUS_PARTIAL))

    def test_resting_only_reports_open(self) -> None:
        result = reconcile_order(
            order_number=_NUMBER, symbol=_SYMBOL,
            open_orders=(open_order(quantity=3, filled=0),), fills=(),
        )
        self.assertEqual(result, OrderReconciliation(_NUMBER, _SYMBOL, 0, 3, STATUS_OPEN))

    def test_unknown_order_number_reports_not_found(self) -> None:
        result = reconcile_order(
            order_number="999", symbol=_SYMBOL,
            open_orders=(open_order(number="100"),),
            fills=(fill(number="100"),),
        )
        self.assertEqual(result, OrderReconciliation("999", _SYMBOL, 0, 0, STATUS_NOT_FOUND))

    def test_symbol_mismatch_is_not_matched(self) -> None:
        result = reconcile_order(
            order_number=_NUMBER, symbol="000660",
            open_orders=(open_order(symbol=_SYMBOL),),
            fills=(fill(symbol=_SYMBOL),),
        )
        self.assertEqual(result.status, STATUS_NOT_FOUND)

    def test_multiple_fills_are_summed(self) -> None:
        result = reconcile_order(
            order_number=_NUMBER, symbol=_SYMBOL,
            open_orders=(),
            fills=(fill(quantity=5, filled=2), fill(quantity=5, filled=3)),
        )
        self.assertEqual(result, OrderReconciliation(_NUMBER, _SYMBOL, 5, 0, STATUS_FILLED))

    def test_rejects_wrong_element_type(self) -> None:
        with self.assertRaises(ValueError):
            reconcile_order(order_number=_NUMBER, symbol=_SYMBOL, open_orders=(fill(),), fills=())

    def test_rejects_empty_order_number(self) -> None:
        with self.assertRaises(ValueError):
            reconcile_order(order_number="", symbol=_SYMBOL, open_orders=(), fills=())

    def test_rejects_non_sequence(self) -> None:
        with self.assertRaises(ValueError):
            reconcile_order(order_number=_NUMBER, symbol=_SYMBOL, open_orders=None, fills=())  # type: ignore[arg-type]


class ReconcileViaClientTest(unittest.TestCase):
    def test_partial_through_fake_client(self) -> None:
        client = FakeReconClient(
            open_orders=(open_order(quantity=4, filled=1),),
            fills=(fill(quantity=4, filled=1),),
        )
        result = reconcile_via_client(
            client, order_number=_NUMBER, symbol=_SYMBOL, start="20260701", end="20260719",
        )
        self.assertEqual(result, OrderReconciliation(_NUMBER, _SYMBOL, 1, 3, STATUS_PARTIAL))
        self.assertEqual(client.fill_range, ("20260701", "20260719"))

    def test_filled_through_fake_client(self) -> None:
        client = FakeReconClient(open_orders=(), fills=(fill(quantity=3, filled=3),))
        result = reconcile_via_client(
            client, order_number=_NUMBER, symbol=_SYMBOL, start="20260701", end="20260719",
        )
        self.assertEqual(result.status, STATUS_FILLED)


if __name__ == "__main__":
    unittest.main()
