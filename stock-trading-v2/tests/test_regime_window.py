"""Tests for the configurable regime warm-up window (doc-04 §3.1 hypothesis)."""

import unittest
from decimal import Decimal as D

from swing_v2.signals import is_risk_on


def _rising(n):
    return [D(100) + D(i) for i in range(n)]


class RegimeWindowTests(unittest.TestCase):
    def test_default_needs_200_closes(self):
        self.assertFalse(is_risk_on(_rising(199)))
        self.assertTrue(is_risk_on(_rising(200)))

    def test_shorter_long_window_trades_earlier(self):
        closes = _rising(60)  # not enough for 200, enough for 60
        self.assertFalse(is_risk_on(closes))  # default 200 -> risk-off (warm-up)
        self.assertTrue(is_risk_on(closes, long_window=60, short_window=20))

    def test_downtrend_is_risk_off_regardless_of_window(self):
        closes = [D(300) - D(i) for i in range(120)]  # falling
        self.assertFalse(is_risk_on(closes, long_window=100, short_window=30))

    def test_invalid_windows_raise(self):
        for lw, sw in ((1, 1), (100, 0), (50, 60), (200, 201)):
            with self.assertRaises(ValueError):
                is_risk_on(_rising(210), long_window=lw, short_window=sw)


if __name__ == "__main__":
    unittest.main()
