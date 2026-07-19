"""Tests for one-at-a-time parameter sensitivity sweeps (doc-02 §6).

Each point re-runs the backtest with exactly one risk parameter overridden from the
base config, so a fragile result (one that only works at one parameter value) becomes
visible instead of hidden behind a single hand-picked combination.
"""

import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest.metrics import BacktestMetrics
from swing_v2.backtest.sensitivity import (
    SensitivityPoint,
    default_sensitivity_grid,
    run_parameter_sweep,
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
    histories = {"005930": [_bar(d, "005930", 70000 + i) for i, d in enumerate(days)]}
    metadata = SnapshotMetadata("TEST", "2026-07-21T00:00:00+09:00", days[-1].isoformat(), True)
    snapshot = DailyBarSnapshot(metadata, "KS11", {"005930": "STOCK"}, days, histories, market)
    path = Path(tmp) / "snap.json"
    save_snapshot(snapshot, path)
    return path


class SensitivityTests(unittest.TestCase):
    def test_sweep_returns_a_point_per_grid_value(self):
        with TemporaryDirectory() as tmp:
            snap = _write_snapshot(tmp)
            grid = {"max_positions": [3, 5], "initial_stop_pct": [D("0.06"), D("0.10")]}
            points = run_parameter_sweep(snapshot_path=snap, initial_cash=D("10000000"), grid=grid)
            self.assertEqual(len(points), 4)
            self.assertTrue(all(isinstance(p, SensitivityPoint) for p in points))
            self.assertTrue(all(isinstance(p.metrics, BacktestMetrics) for p in points))
            self.assertEqual({p.parameter for p in points}, {"max_positions", "initial_stop_pct"})

    def test_default_grid_has_the_doc02_parameters(self):
        grid = default_sensitivity_grid()
        for key in ("initial_stop_pct", "max_positions", "max_gap_up_pct", "max_position_notional_pct"):
            self.assertIn(key, grid)

    def test_unknown_parameter_is_rejected(self):
        with TemporaryDirectory() as tmp:
            snap = _write_snapshot(tmp)
            with self.assertRaises(ValueError):
                run_parameter_sweep(
                    snapshot_path=snap, initial_cash=D("10000000"), grid={"not_a_field": [1]},
                )

    def test_empty_grid_is_rejected(self):
        with TemporaryDirectory() as tmp:
            snap = _write_snapshot(tmp)
            with self.assertRaises(ValueError):
                run_parameter_sweep(snapshot_path=snap, initial_cash=D("10000000"), grid={})


if __name__ == "__main__":
    unittest.main()
