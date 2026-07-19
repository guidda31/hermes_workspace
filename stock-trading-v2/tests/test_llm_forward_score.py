"""Tests for the forward_cli `score` step: score accumulated signal audits.

Completes the forward-observation loop — record signals over days, then score the
accumulated audits against the snapshot's realized outcomes.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest_data import DailyBarSnapshot, SnapshotMetadata, save_snapshot
from swing_v2.contracts import DailyBar
from swing_v2.llm.forward_cli import record_from_snapshot, score_accumulated_observations
from swing_v2.llm.forward_eval import ForwardObservationReport


D = Decimal
KST = timezone(timedelta(hours=9))


def _bar(day, symbol, asset_type, close):
    c = D(close)
    return DailyBar(day, symbol, asset_type, c, c + D("1"), c - D("1"), c, 1_000_000, c * 1_000_000, True)


def _write_snapshot(tmp):
    days = []
    cursor = date(2026, 7, 24)
    while len(days) < 10:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()
    market = [_bar(d, "KOSPI", "INDEX", 2500 + i) for i, d in enumerate(days)]
    histories = {
        "005930": [_bar(d, "005930", "STOCK", 70000 + i * 500) for i, d in enumerate(days)],   # rises
        "000660": [_bar(d, "000660", "STOCK", 180000 - i * 300) for i, d in enumerate(days)],   # falls
    }
    meta = SnapshotMetadata("TEST", "2026-07-25T00:00:00+09:00", days[-1].isoformat(), False)
    snap = DailyBarSnapshot(meta, "KOSPI", {"005930": "STOCK", "000660": "STOCK"}, days, histories, market)
    path = Path(tmp) / "snap.json"
    save_snapshot(snap, path)
    return path, days


def _reply(symbol, signal_date):
    return (f'[{{"symbol": "{symbol}", "action": "BUY", "conviction": "0.8", '
            f'"target_weight": "0.1", "rationale": "mom", '
            f'"cited_evidence": ["px:{symbol}:{signal_date.isoformat()}"]}}]')


class ForwardScoreTests(unittest.TestCase):
    def _record_days(self, snap, days, records_dir, symbol):
        for i in (0, 1):
            sd = days[i]
            record_from_snapshot(
                snap, sd, ("005930", "000660"), _reply(symbol, sd),
                eligible=frozenset({"005930", "000660"}), model_id="hermes/openai-oauth",
                decided_at=datetime(2026, 7, 25, 16, tzinfo=KST),
                output_path=records_dir / f"signal-{sd.isoformat()}.json", window=6,
            )

    def test_scores_accumulated_records(self):
        with TemporaryDirectory() as tmp:
            snap, days = _write_snapshot(tmp)
            rec = Path(tmp) / "records"
            rec.mkdir()
            self._record_days(snap, days, rec, "005930")  # picks the rising stock
            report = score_accumulated_observations(
                records_dir=rec, snapshot_path=snap, forward_sessions=2,
            )
            self.assertIsInstance(report, ForwardObservationReport)
            self.assertEqual(report.scored_count, 2)
            self.assertGreater(report.mean_pick_return, D("0"))  # 005930 rises
            self.assertTrue(all(o.symbol == "005930" for o in report.outcomes))

    def test_no_records_raises(self):
        with TemporaryDirectory() as tmp:
            snap, _ = _write_snapshot(tmp)
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaises(ValueError):
                score_accumulated_observations(records_dir=empty, snapshot_path=snap)


if __name__ == "__main__":
    unittest.main()
