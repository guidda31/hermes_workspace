"""Tests for the unattended-trading authorization, budgets, and market-hours gate."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from swing_v2.live.autonomous import (
    AUTONOMOUS_CONFIRMATION,
    AutonomousBlocked,
    day_order_usage,
    is_krx_regular_session,
    load_authorization,
    record_order,
    require_autonomous_authorized,
    write_authorization,
)
from swing_v2.live.gate import LIVE_OPERATOR_CONFIRMATION

_KST = timezone(timedelta(hours=9))


def _auth_file(**over):
    path = str(Path(tempfile.mkdtemp()) / "auth.json")
    kw = dict(operator_confirmation=AUTONOMOUS_CONFIRMATION,
              expires_at=datetime(2027, 1, 1, tzinfo=_KST),
              max_orders_per_day=2, max_notional_per_day=Decimal("300000"))
    kw.update(over)
    write_authorization(path, **kw)
    return path


class AuthorizationTests(unittest.TestCase):
    def test_write_requires_exact_phrase(self):
        with self.assertRaises(ValueError):
            _auth_file(operator_confirmation="wrong")

    def test_roundtrip(self):
        auth = load_authorization(_auth_file())
        self.assertTrue(auth.enabled)
        self.assertEqual(auth.max_orders_per_day, 2)

    def test_absent_is_none(self):
        self.assertIsNone(load_authorization(str(Path(tempfile.mkdtemp()) / "nope.json")))

    def test_corrupt_fails_closed(self):
        p = str(Path(tempfile.mkdtemp()) / "auth.json")
        Path(p).write_text("{bad", encoding="utf-8")
        with self.assertRaises(AutonomousBlocked):
            load_authorization(p)


class RequireAuthorizedTests(unittest.TestCase):
    _NOW = datetime(2026, 7, 22, 10, 0, tzinfo=_KST)

    def test_returns_gate_phrase_when_within_budget(self):
        phrase = require_autonomous_authorized(_auth_file(), now=self._NOW, orders_today=0,
                                               notional_today=Decimal("0"), next_notional=Decimal("100000"))
        self.assertEqual(phrase, LIVE_OPERATOR_CONFIRMATION)

    def test_no_file_blocks(self):
        with self.assertRaises(AutonomousBlocked):
            require_autonomous_authorized(str(Path(tempfile.mkdtemp()) / "x.json"), now=self._NOW,
                                          orders_today=0, notional_today=Decimal("0"), next_notional=Decimal("1"))

    def test_expired_blocks(self):
        path = _auth_file(expires_at=datetime(2026, 7, 1, tzinfo=_KST))
        with self.assertRaises(AutonomousBlocked):
            require_autonomous_authorized(path, now=self._NOW, orders_today=0,
                                          notional_today=Decimal("0"), next_notional=Decimal("1"))

    def test_order_count_budget_blocks(self):
        with self.assertRaises(AutonomousBlocked):
            require_autonomous_authorized(_auth_file(max_orders_per_day=2), now=self._NOW, orders_today=2,
                                          notional_today=Decimal("0"), next_notional=Decimal("1"))

    def test_notional_budget_blocks(self):
        with self.assertRaises(AutonomousBlocked):
            require_autonomous_authorized(_auth_file(max_notional_per_day=Decimal("300000")), now=self._NOW,
                                          orders_today=0, notional_today=Decimal("250000"),
                                          next_notional=Decimal("100000"))  # 350k > 300k


class MarketHoursTests(unittest.TestCase):
    def test_open_during_session(self):
        self.assertTrue(is_krx_regular_session(datetime(2026, 7, 22, 10, 0, tzinfo=_KST)))  # Wed

    def test_closed_after_hours(self):
        self.assertFalse(is_krx_regular_session(datetime(2026, 7, 22, 22, 0, tzinfo=_KST)))

    def test_closed_on_weekend(self):
        self.assertFalse(is_krx_regular_session(datetime(2026, 7, 25, 10, 0, tzinfo=_KST)))  # Sat


class BudgetLedgerTests(unittest.TestCase):
    def test_records_and_totals(self):
        d = tempfile.mkdtemp()
        record_order(d, day="2026-07-22", symbol="086790", notional=Decimal("133400"), at=datetime.now(_KST))
        record_order(d, day="2026-07-22", symbol="105560", notional=Decimal("100000"), at=datetime.now(_KST))
        count, total = day_order_usage(d, "2026-07-22")
        self.assertEqual(count, 2)
        self.assertEqual(total, Decimal("233400"))

    def test_absent_day_is_zero(self):
        self.assertEqual(day_order_usage(tempfile.mkdtemp(), "2026-07-22"), (0, Decimal("0")))

    def test_corrupt_ledger_fails_closed(self):
        d = tempfile.mkdtemp()
        (Path(d) / "2026-07-22.jsonl").write_text("{bad\n", encoding="utf-8")
        with self.assertRaises(AutonomousBlocked):
            day_order_usage(d, "2026-07-22")


if __name__ == "__main__":
    unittest.main()
