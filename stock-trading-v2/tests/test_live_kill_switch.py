"""Tests for the live-trading kill switch (fail-closed pre-submit halt)."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from swing_v2.live.kill_switch import (
    LiveTradingHalted,
    engage_kill_switch,
    is_kill_switch_engaged,
    release_kill_switch,
    require_not_halted,
)


class LiveKillSwitchTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(tempfile.mkdtemp()) / "live-kill-switch.json")

    def test_require_not_halted_passes_when_absent(self):
        require_not_halted(self.path)  # does not raise

    def test_require_not_halted_raises_when_engaged(self):
        engage_kill_switch(self.path, reason="market anomaly", engaged_at=datetime.now(timezone.utc))
        self.assertTrue(is_kill_switch_engaged(self.path))
        with self.assertRaises(LiveTradingHalted) as ctx:
            require_not_halted(self.path)
        self.assertIn("market anomaly", str(ctx.exception))

    def test_release_clears_the_halt(self):
        engage_kill_switch(self.path, reason="x", engaged_at=datetime.now(timezone.utc))
        release_kill_switch(self.path)
        require_not_halted(self.path)  # no longer raises

    def test_corrupt_marker_fails_closed(self):
        Path(self.path).write_text("not json", encoding="utf-8")
        with self.assertRaises(LiveTradingHalted):
            require_not_halted(self.path)


if __name__ == "__main__":
    unittest.main()
