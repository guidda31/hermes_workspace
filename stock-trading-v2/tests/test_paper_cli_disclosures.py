"""Tests that the paper CLI threads DART disclosures into the brief (price+disclosures).

Mirrors the forward CLI: `render_paper_prompt` takes an optional `disclosure_provider`
threaded into `build_brief`, so point-in-time disclosures appear in the agent prompt.
With no provider the brief stays price-only. No network, no real money/order.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest_data import DailyBarSnapshot, SnapshotMetadata, save_snapshot
from swing_v2.contracts import DailyBar
from swing_v2.llm.brief import EvidenceItem
from swing_v2.paper.cli import render_paper_prompt

D = Decimal
KST = timezone(timedelta(hours=9))
PILOT = ("005930", "000660")


def _bar(day, symbol, close):
    c = D(close)
    return DailyBar(day, symbol, "STOCK", c, c + D("1"), c - D("1"), c, 1_000_000, c * 1_000_000, True)


def _write_snapshot(tmp):
    days = []
    cursor = date(2026, 7, 20)
    while len(days) < 6:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()
    market = [_bar(d, "KS11", 2500 + i) for i, d in enumerate(days)]
    histories = {
        "005930": [_bar(d, "005930", 70000 + i * 100) for i, d in enumerate(days)],
        "000660": [_bar(d, "000660", 180000 + i * 100) for i, d in enumerate(days)],
    }
    metadata = SnapshotMetadata("TEST", "2026-07-21T00:00:00+09:00", days[-1].isoformat(), True)
    snapshot = DailyBarSnapshot(metadata, "KS11", {"005930": "STOCK", "000660": "STOCK"}, days, histories, market)
    path = Path(tmp) / "snap.json"
    save_snapshot(snapshot, path)
    return path, days[-2], days[-1]  # snapshot_path, signal_date, execution_date


class PaperCliDisclosureTests(unittest.TestCase):
    def _paths(self, tmp):
        return Path(tmp) / "sessions", Path(tmp) / "kill.json"

    def test_render_includes_injected_disclosure_evidence(self):
        with TemporaryDirectory() as tmp:
            snap, signal_date, _ = _write_snapshot(tmp)
            sessions, kill = self._paths(tmp)
            evidence_id = f"dart:005930:{signal_date.isoformat()}"

            def fake_provider(symbol, sig_date):
                if symbol != "005930":
                    return ()
                return (
                    EvidenceItem(
                        evidence_id=evidence_id,
                        kind="disclosure",
                        symbol="005930",
                        published_at=datetime(sig_date.year, sig_date.month, sig_date.day, 9, tzinfo=KST),
                        summary="single treasury share disposal",
                    ),
                )

            prompt = render_paper_prompt(
                snapshot_path=snap, signal_date=signal_date, symbols=PILOT,
                session_dir=sessions, kill_switch_path=kill,
                disclosure_provider=fake_provider,
            )
            self.assertIn(evidence_id, prompt)
            self.assertIn("single treasury share disposal", prompt)

    def test_render_without_provider_stays_price_only(self):
        with TemporaryDirectory() as tmp:
            snap, signal_date, _ = _write_snapshot(tmp)
            sessions, kill = self._paths(tmp)
            prompt = render_paper_prompt(
                snapshot_path=snap, signal_date=signal_date, symbols=PILOT,
                session_dir=sessions, kill_switch_path=kill,
            )
            self.assertNotIn("dart:", prompt)


if __name__ == "__main__":
    unittest.main()
