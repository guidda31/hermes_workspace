"""Tests for deriving the signal-implied portfolio from forward records."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from swing_v2.llm.positions import held_symbols_from_records


def _write(directory, signal_date, decisions, admitted):
    record = {"signal_date": signal_date, "admitted_symbols": admitted, "decisions": decisions}
    (Path(directory) / f"{signal_date}.json").write_text(json.dumps(record), encoding="utf-8")


class HeldSymbolsTests(unittest.TestCase):
    def test_net_buys_minus_sells_in_date_order(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "2026-07-20",
                   [{"symbol": "105560", "action": "BUY"}, {"symbol": "086790", "action": "BUY"}],
                   ["105560", "086790"])
            _write(d, "2026-07-21",
                   [{"symbol": "086790", "action": "SELL"}, {"symbol": "005930", "action": "BUY"}],
                   ["086790", "005930"])
            self.assertEqual(set(held_symbols_from_records(d)), {"105560", "005930"})

    def test_ignores_non_admitted_decisions(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "2026-07-20",
                   [{"symbol": "105560", "action": "BUY"}, {"symbol": "999999", "action": "BUY"}],
                   ["105560"])  # 999999 rejected -> not held
            self.assertEqual(held_symbols_from_records(d), ("105560",))

    def test_as_of_excludes_future_records(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "2026-07-20", [{"symbol": "105560", "action": "BUY"}], ["105560"])
            _write(d, "2026-07-22", [{"symbol": "005930", "action": "BUY"}], ["005930"])
            self.assertEqual(held_symbols_from_records(d, as_of=date(2026, 7, 21)), ("105560",))

    def test_missing_dir_is_empty(self):
        self.assertEqual(held_symbols_from_records("/nonexistent/path/xyz"), ())


if __name__ == "__main__":
    unittest.main()
