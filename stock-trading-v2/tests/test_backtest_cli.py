"""Tests for the backtest CLI: run the engine from a snapshot and emit results.

The CLI wires the snapshot + a clearly-labelled RESEARCH (non-PIT, non-survivorship)
universe metadata into the engine, then computes metrics and writes the doc-04 §7
ledgers + run summary. The research metadata is for baseline exploration only.
"""

import json
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest.backtest_engine import BacktestResult
from swing_v2.backtest.cli import build_research_metadata, run_backtest_from_snapshot
from swing_v2.backtest.metrics import BacktestMetrics
from swing_v2.backtest_data import DailyBarSnapshot, SnapshotMetadata, save_snapshot
from swing_v2.contracts import DailyBar
from swing_v2.universe_metadata import AssetType, UniverseMetadataSnapshot, select_eligible_universe


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


class ResearchMetadataTests(unittest.TestCase):
    def test_research_metadata_makes_symbols_eligible_from_as_of(self):
        as_of = date(2024, 1, 2)
        meta = build_research_metadata({"005930": "STOCK", "069500": "ETF"}, as_of=as_of)
        self.assertIsInstance(meta, UniverseMetadataSnapshot)
        selection = select_eligible_universe(meta, date(2025, 1, 2), requested_symbols=("005930", "069500"))
        self.assertEqual(set(selection.symbols), {"005930", "069500"})

    def test_research_metadata_denies_before_as_of(self):
        meta = build_research_metadata({"005930": "STOCK"}, as_of=date(2024, 1, 2))
        selection = select_eligible_universe(meta, date(2023, 1, 1), requested_symbols=("005930",))
        self.assertEqual(selection.symbols, ())

    def test_research_metadata_source_is_labelled_non_pit(self):
        meta = build_research_metadata({"005930": "STOCK"}, as_of=date(2024, 1, 2))
        self.assertIn("RESEARCH", meta.records[0].provenance.source.upper())


class RunBacktestTests(unittest.TestCase):
    def test_run_produces_result_and_metrics_and_writes_files(self):
        with TemporaryDirectory() as tmp:
            snap, days = _write_snapshot(tmp)
            out = Path(tmp) / "out"
            result, metrics = run_backtest_from_snapshot(
                snapshot_path=snap, initial_cash=D("10000000"), output_dir=out,
            )
            self.assertIsInstance(result, BacktestResult)
            self.assertIsInstance(metrics, BacktestMetrics)
            self.assertEqual(metrics.starting_nav, D("10000000"))
            self.assertEqual(len(result.equity_curve), len(days))
            for name in ("equity_curve.csv", "orders.csv", "fills.csv", "positions.csv", "signals.csv", "run_summary.json"):
                self.assertTrue((out / name).exists(), name)
            summary = json.loads((out / "run_summary.json").read_text())
            self.assertIn("counts", summary)

    def test_run_without_output_dir_still_returns_metrics(self):
        with TemporaryDirectory() as tmp:
            snap, _ = _write_snapshot(tmp)
            result, metrics = run_backtest_from_snapshot(snapshot_path=snap, initial_cash=D("10000000"))
            self.assertIsInstance(metrics, BacktestMetrics)


if __name__ == "__main__":
    unittest.main()
