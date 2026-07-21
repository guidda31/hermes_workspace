"""Tests for auto-recording realized loss from filled SELL orders."""

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from swing_v2.live.pilot_reconcile import (
    STATUS_FILLED,
    STATUS_OPEN,
    OrderReconciliation,
)
from swing_v2.live.realized_settlement import (
    PendingSell,
    load_pending_sells,
    record_pending_sell,
    rewrite_pending_sells,
    settle,
)

_WHEN = datetime(2026, 7, 22, 10, tzinfo=timezone.utc)


class PendingStoreTests(unittest.TestCase):
    def test_record_load_roundtrip(self):
        path = str(Path(tempfile.mkdtemp()) / "pending.jsonl")
        record_pending_sell(path, order_number="0000000123", symbol="005930",
                            quantity=10, avg_cost=Decimal("71000"), at=_WHEN)
        pending = load_pending_sells(path)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].order_number, "0000000123")
        self.assertEqual(pending[0].avg_cost, Decimal("71000"))

    def test_absent_is_empty(self):
        self.assertEqual(load_pending_sells(str(Path(tempfile.mkdtemp()) / "none.jsonl")), ())

    def test_corrupt_fails_closed(self):
        path = str(Path(tempfile.mkdtemp()) / "pending.jsonl")
        Path(path).write_text("{bad\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_pending_sells(path)

    def test_rewrite_keeps_only_remaining(self):
        path = str(Path(tempfile.mkdtemp()) / "pending.jsonl")
        keep = PendingSell("0000000999", "000660", 1, Decimal("175000"), _WHEN.isoformat())
        rewrite_pending_sells(path, [keep])
        self.assertEqual(load_pending_sells(path), (keep,))


class SettleTests(unittest.TestCase):
    def _pending(self):
        return [PendingSell("0000000123", "005930", 10, Decimal("71000"), _WHEN.isoformat())]

    def test_filled_sell_settles_with_realized_loss(self):
        recon = {"0000000123": OrderReconciliation("0000000123", "005930", 10, 0, STATUS_FILLED,
                                                    Decimal("69000"))}  # sold below cost
        settled, remaining = settle(self._pending(), recon)
        self.assertEqual(remaining, [])
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0].realized_pnl, Decimal("-20000"))  # (69000-71000)*10
        self.assertEqual(settled[0].sell_price, Decimal("69000"))

    def test_open_sell_stays_pending(self):
        recon = {"0000000123": OrderReconciliation("0000000123", "005930", 0, 10, STATUS_OPEN)}
        settled, remaining = settle(self._pending(), recon)
        self.assertEqual(settled, [])
        self.assertEqual(len(remaining), 1)

    def test_unreconciled_stays_pending(self):
        settled, remaining = settle(self._pending(), {})  # no reconciliation yet
        self.assertEqual(settled, [])
        self.assertEqual(len(remaining), 1)

    def test_profitable_sell_settles_with_positive_pnl(self):
        recon = {"0000000123": OrderReconciliation("0000000123", "005930", 10, 0, STATUS_FILLED,
                                                    Decimal("75000"))}
        settled, _ = settle(self._pending(), recon)
        self.assertEqual(settled[0].realized_pnl, Decimal("40000"))


if __name__ == "__main__":
    unittest.main()
