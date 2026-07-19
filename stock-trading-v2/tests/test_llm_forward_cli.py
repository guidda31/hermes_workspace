"""Tests for the autonomous two-step forward-observation CLI glue.

A Hermes routine runs `render_from_snapshot` to get the prompt, reasons, then runs
`record_from_snapshot` with its JSON reply to guardrail and durably record the signal.
Deterministic: the same snapshot+date rebuilds the identical brief, so the record step
needs no persisted brief. No orders, no network.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest_data import DailyBarSnapshot, SnapshotMetadata, save_snapshot
from swing_v2.contracts import DailyBar
from swing_v2.llm.forward_cli import record_from_snapshot, render_from_snapshot


KST = timezone(timedelta(hours=9))
PILOT = ("005930", "000660")


def _bar(day, symbol, asset_type, close):
    close = Decimal(close)
    return DailyBar(day, symbol, asset_type, close, close, close, close, 1_000_000, close * 1_000_000, True)


def _write_snapshot(tmp):
    days = []
    cursor = date(2026, 7, 16)
    while len(days) < 6:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()
    market = [_bar(d, "KS11", "INDEX", 2500 + i) for i, d in enumerate(days)]
    histories = {
        "005930": [_bar(d, "005930", "STOCK", 70000 + i * 100) for i, d in enumerate(days)],
        "000660": [_bar(d, "000660", "STOCK", 180000 + i * 100) for i, d in enumerate(days)],
    }
    metadata = SnapshotMetadata("TEST", "2026-07-17T00:00:00+09:00", days[-1].isoformat(), True)
    snapshot = DailyBarSnapshot(metadata, "KS11", {"005930": "STOCK", "000660": "STOCK"}, days, histories, market)
    path = Path(tmp) / "snap.json"
    save_snapshot(snapshot, path)
    return path, days[-1]


class ForwardCliTests(unittest.TestCase):
    def test_render_produces_prompt_with_symbols(self):
        with TemporaryDirectory() as tmp:
            path, signal_date = _write_snapshot(tmp)
            prompt = render_from_snapshot(path, signal_date, PILOT, window=6)
            self.assertIn("005930", prompt)
            self.assertIn(signal_date.isoformat(), prompt)

    def test_record_guardrails_and_writes_audit(self):
        with TemporaryDirectory() as tmp:
            path, signal_date = _write_snapshot(tmp)
            reply = ('[{"symbol": "005930", "action": "BUY", "conviction": "0.7", '
                     '"target_weight": "0.1", "rationale": "momentum", '
                     f'"cited_evidence": ["px:005930:{signal_date.isoformat()}"]}}]')
            out = Path(tmp) / "signal.json"
            record = record_from_snapshot(
                path, signal_date, PILOT, reply,
                eligible=frozenset(PILOT),
                model_id="hermes/openai-oauth",
                decided_at=datetime(2026, 7, 16, 16, 0, tzinfo=KST),
                output_path=out,
                window=6,
            )
            self.assertEqual(record["admitted_symbols"], ["005930"])
            self.assertTrue(out.exists())

    def test_record_rejects_ineligible_symbol(self):
        with TemporaryDirectory() as tmp:
            path, signal_date = _write_snapshot(tmp)
            reply = ('[{"symbol": "005930", "action": "BUY", "conviction": "0.7", '
                     '"target_weight": "0.1", "rationale": "x", '
                     f'"cited_evidence": ["px:005930:{signal_date.isoformat()}"]}}]')
            record = record_from_snapshot(
                path, signal_date, PILOT, reply,
                eligible=frozenset({"000660"}),  # 005930 NOT eligible
                model_id="hermes/openai-oauth",
                decided_at=datetime(2026, 7, 16, 16, 0, tzinfo=KST),
                window=6,
            )
            self.assertEqual(record["admitted_symbols"], [])
            self.assertEqual(record["rejected"][0]["symbol"], "005930")

    def test_record_rejects_hallucinated_evidence(self):
        with TemporaryDirectory() as tmp:
            path, signal_date = _write_snapshot(tmp)
            reply = ('[{"symbol": "005930", "action": "BUY", "conviction": "0.7", '
                     '"target_weight": "0.1", "rationale": "x", '
                     '"cited_evidence": ["news:made-up-1"]}]')
            with self.assertRaises(ValueError):
                record_from_snapshot(
                    path, signal_date, PILOT, reply,
                    eligible=frozenset(PILOT),
                    model_id="hermes/openai-oauth",
                    decided_at=datetime(2026, 7, 16, 16, 0, tzinfo=KST),
                    window=6,
                )


if __name__ == "__main__":
    unittest.main()
