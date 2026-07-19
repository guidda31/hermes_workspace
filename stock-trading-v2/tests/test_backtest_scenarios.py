"""Tests for doc-04 §9 robustness scenarios: cost-stress and walk-forward windows.

These run the baseline backtest under harsher costs and across contiguous sub-periods
to check the strategy is not a single-window fluke. The tiny on-disk fixture (~8
weekday sessions, 1 symbol) usually yields zero trades because the regime filter needs
~200 closes; that is expected, so assertions target structure and dates, not fills.
"""

import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest.cli import default_costs, stress_costs
from swing_v2.backtest.metrics import BacktestMetrics
from swing_v2.backtest.scenarios import (
    WalkForwardWindow,
    run_cost_scenarios,
    run_walk_forward,
)
from swing_v2.backtest_data import DailyBarSnapshot, SnapshotMetadata, save_snapshot
from swing_v2.contracts import DailyBar


D = Decimal


def _bar(day, symbol, close):
    c = D(close)
    return DailyBar(day, symbol, "STOCK", c, c + D("1"), c - D("1"), c, 1_000_000, c * 1_000_000, True)


def _write_snapshot(tmp, n=8):
    days = []
    cursor = date(2026, 7, 20)
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()
    market = [_bar(d, "KS11", 2500 + i) for i, d in enumerate(days)]
    histories = {"005930": [_bar(d, "005930", 70000 + i * 10) for i, d in enumerate(days)]}
    metadata = SnapshotMetadata("TEST", "2026-07-21T00:00:00+09:00", days[-1].isoformat(), True)
    snapshot = DailyBarSnapshot(metadata, "KS11", {"005930": "STOCK"}, days, histories, market)
    path = Path(tmp) / "snap.json"
    save_snapshot(snapshot, path)
    return path, days


class CostScenarioTests(unittest.TestCase):
    def test_default_scenarios_return_base_and_stress_metrics(self):
        with TemporaryDirectory() as tmp:
            snap, _ = _write_snapshot(tmp)
            out = run_cost_scenarios(snapshot_path=snap, initial_cash=D("10000000"))
            self.assertEqual(set(out), {"base", "stress"})
            for metrics in out.values():
                self.assertIsInstance(metrics, BacktestMetrics)
            # Stress cannot beat base once there are trades; with no trades they tie.
            self.assertLessEqual(out["stress"].total_return, out["base"].total_return)

    def test_custom_scenarios_are_honored(self):
        with TemporaryDirectory() as tmp:
            snap, _ = _write_snapshot(tmp)
            out = run_cost_scenarios(
                snapshot_path=snap,
                initial_cash=D("10000000"),
                scenarios={"only": default_costs()},
            )
            self.assertEqual(set(out), {"only"})
            self.assertIsInstance(out["only"], BacktestMetrics)

    def test_empty_scenarios_rejected(self):
        with TemporaryDirectory() as tmp:
            snap, _ = _write_snapshot(tmp)
            with self.assertRaises(ValueError):
                run_cost_scenarios(snapshot_path=snap, initial_cash=D("10000000"), scenarios={})

    def test_non_decimal_cash_rejected(self):
        with TemporaryDirectory() as tmp:
            snap, _ = _write_snapshot(tmp)
            with self.assertRaises(ValueError):
                run_cost_scenarios(snapshot_path=snap, initial_cash=10000000)


class WalkForwardTests(unittest.TestCase):
    def test_two_windows_cover_disjoint_contiguous_ranges_in_order(self):
        with TemporaryDirectory() as tmp:
            snap, days = _write_snapshot(tmp)
            windows = run_walk_forward(
                snapshot_path=snap, initial_cash=D("10000000"), num_windows=2,
            )
            self.assertEqual(len(windows), 2)
            for i, window in enumerate(windows):
                self.assertIsInstance(window, WalkForwardWindow)
                self.assertEqual(window.index, i)
                self.assertIsInstance(window.metrics, BacktestMetrics)
                self.assertLessEqual(window.start_date, window.end_date)
            # Disjoint and contiguous: second starts strictly after the first ends.
            self.assertLess(windows[0].end_date, windows[1].start_date)
            # Together they span the whole calendar.
            self.assertEqual(windows[0].start_date, days[0])
            self.assertEqual(windows[1].end_date, days[-1])

    def test_windows_with_stress_costs_run(self):
        with TemporaryDirectory() as tmp:
            snap, _ = _write_snapshot(tmp)
            windows = run_walk_forward(
                snapshot_path=snap, initial_cash=D("10000000"), num_windows=3,
                costs=stress_costs(),
            )
            self.assertEqual(len(windows), 3)
            self.assertEqual([w.index for w in windows], [0, 1, 2])

    def test_too_many_windows_rejected(self):
        with TemporaryDirectory() as tmp:
            snap, days = _write_snapshot(tmp)
            with self.assertRaises(ValueError):
                run_walk_forward(
                    snapshot_path=snap, initial_cash=D("10000000"), num_windows=len(days) + 1,
                )

    def test_zero_windows_rejected(self):
        with TemporaryDirectory() as tmp:
            snap, _ = _write_snapshot(tmp)
            with self.assertRaises(ValueError):
                run_walk_forward(snapshot_path=snap, initial_cash=D("10000000"), num_windows=0)


if __name__ == "__main__":
    unittest.main()
