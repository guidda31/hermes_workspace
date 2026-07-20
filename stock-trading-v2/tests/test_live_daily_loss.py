"""Tests for the per-day realized-loss ledger feeding the daily-loss circuit breaker."""

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from swing_v2.live.daily_loss import (
    compute_realized_pnl,
    realized_loss_for,
    record_realized_trade,
)

_WHEN = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)


class ComputeTests(unittest.TestCase):
    def test_loss_is_negative(self):
        self.assertEqual(compute_realized_pnl(sell_price=Decimal("90"), avg_cost=Decimal("100"), quantity=3),
                         Decimal("-30"))

    def test_gain_is_positive(self):
        self.assertEqual(compute_realized_pnl(sell_price=Decimal("110"), avg_cost=Decimal("100"), quantity=2),
                         Decimal("20"))

    def test_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            compute_realized_pnl(sell_price=Decimal("0"), avg_cost=Decimal("100"), quantity=1)
        with self.assertRaises(ValueError):
            compute_realized_pnl(sell_price=Decimal("100"), avg_cost=Decimal("100"), quantity=0)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_absent_ledger_is_zero_loss(self):
        self.assertEqual(realized_loss_for(self.dir, "2026-07-21"), Decimal("0"))

    def test_records_and_sums_losses_as_nonnegative(self):
        record_realized_trade(self.dir, day="2026-07-21", symbol="005930", quantity=10,
                              sell_price=Decimal("69000"), avg_cost=Decimal("71000"), recorded_at=_WHEN)  # -20,000
        record_realized_trade(self.dir, day="2026-07-21", symbol="000660", quantity=1,
                              sell_price=Decimal("170000"), avg_cost=Decimal("175000"), recorded_at=_WHEN)  # -5,000
        self.assertEqual(realized_loss_for(self.dir, "2026-07-21"), Decimal("25000"))

    def test_net_profit_reports_zero_loss(self):
        record_realized_trade(self.dir, day="2026-07-21", symbol="005930", quantity=10,
                              sell_price=Decimal("75000"), avg_cost=Decimal("71000"), recorded_at=_WHEN)  # +40,000
        record_realized_trade(self.dir, day="2026-07-21", symbol="000660", quantity=1,
                              sell_price=Decimal("170000"), avg_cost=Decimal("175000"), recorded_at=_WHEN)  # -5,000
        self.assertEqual(realized_loss_for(self.dir, "2026-07-21"), Decimal("0"))  # net +35,000

    def test_days_are_isolated(self):
        record_realized_trade(self.dir, day="2026-07-20", symbol="005930", quantity=10,
                              sell_price=Decimal("60000"), avg_cost=Decimal("71000"), recorded_at=_WHEN)
        self.assertEqual(realized_loss_for(self.dir, "2026-07-21"), Decimal("0"))

    def test_corrupt_ledger_fails_closed(self):
        (Path(self.dir) / "2026-07-21.jsonl").write_text("{not json}\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            realized_loss_for(self.dir, "2026-07-21")

    def test_rejects_malformed_day(self):
        with self.assertRaises(ValueError):
            realized_loss_for(self.dir, "07/21/2026")


if __name__ == "__main__":
    unittest.main()
