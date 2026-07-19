"""TDD coverage for the durable manual paper-trading kill switch."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from swing_v2.paper.kill_switch import (
    engage_kill_switch,
    is_kill_switch_engaged,
    read_kill_switch,
    release_kill_switch,
)

KST = timezone(timedelta(hours=9))
ENGAGED_AT = datetime(2026, 7, 19, 9, 30, tzinfo=KST)
LATER = datetime(2026, 7, 19, 15, 0, tzinfo=KST)


class KillSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "HALT"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_engage_then_is_engaged_true(self) -> None:
        self.assertFalse(is_kill_switch_engaged(self.path))
        engage_kill_switch(self.path, reason="manual halt", engaged_at=ENGAGED_AT)
        self.assertTrue(is_kill_switch_engaged(self.path))

    def test_read_returns_reason_and_time(self) -> None:
        engage_kill_switch(self.path, reason="risk breach", engaged_at=ENGAGED_AT)
        marker = read_kill_switch(self.path)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["reason"], "risk breach")
        self.assertEqual(marker["engaged_at"], ENGAGED_AT)

    def test_not_engaged_returns_false_and_none(self) -> None:
        self.assertFalse(is_kill_switch_engaged(self.path))
        self.assertIsNone(read_kill_switch(self.path))

    def test_engage_twice_keeps_first_reason(self) -> None:
        engage_kill_switch(self.path, reason="first reason", engaged_at=ENGAGED_AT)
        engage_kill_switch(self.path, reason="second reason", engaged_at=LATER)
        marker = read_kill_switch(self.path)
        self.assertEqual(marker["reason"], "first reason")
        self.assertEqual(marker["engaged_at"], ENGAGED_AT)

    def test_release_removes_marker(self) -> None:
        engage_kill_switch(self.path, reason="manual halt", engaged_at=ENGAGED_AT)
        self.assertTrue(is_kill_switch_engaged(self.path))
        release_kill_switch(self.path)
        self.assertFalse(is_kill_switch_engaged(self.path))
        self.assertIsNone(read_kill_switch(self.path))

    def test_release_when_absent_is_noop(self) -> None:
        release_kill_switch(self.path)  # must not raise
        self.assertFalse(is_kill_switch_engaged(self.path))

    def test_blank_reason_raises(self) -> None:
        with self.assertRaises(ValueError):
            engage_kill_switch(self.path, reason="   ", engaged_at=ENGAGED_AT)
        self.assertFalse(is_kill_switch_engaged(self.path))

    def test_naive_engaged_at_raises(self) -> None:
        naive = datetime(2026, 7, 19, 9, 30)
        with self.assertRaises(ValueError):
            engage_kill_switch(self.path, reason="manual halt", engaged_at=naive)
        self.assertFalse(is_kill_switch_engaged(self.path))

    def test_corrupt_marker_fails_closed_as_engaged(self) -> None:
        self.path.write_text("{ this is not valid json", encoding="utf-8")
        self.assertTrue(is_kill_switch_engaged(self.path))


if __name__ == "__main__":
    unittest.main()
