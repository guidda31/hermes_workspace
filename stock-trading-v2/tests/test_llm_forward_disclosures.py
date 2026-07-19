"""Tests that DART disclosures, when a provider is supplied, reach the forward brief.

With a disclosure provider the rendered prompt and the recorded brief carry the PIT
disclosures (so the agent can cite them); without one the pipeline is unchanged.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest_data import DailyBarSnapshot, SnapshotMetadata, save_snapshot
from swing_v2.contracts import DailyBar
from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.forward_cli import record_from_snapshot, render_from_snapshot


D = Decimal
KST = timezone(timedelta(hours=9))
PILOT = ("005930", "000660")


def _bar(day, symbol, close):
    c = D(close)
    return DailyBar(day, symbol, "STOCK" if symbol != "KOSPI" else "INDEX",
                    c, c + D("1"), c - D("1"), c, 1_000_000, c * 1_000_000, True)


def _write_snapshot(tmp):
    days = []
    cursor = date(2026, 7, 24)
    while len(days) < 8:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()
    market = [_bar(d, "KOSPI", 2500 + i) for i, d in enumerate(days)]
    histories = {s: [_bar(d, s, 70000 + i) for i, d in enumerate(days)] for s in PILOT}
    meta = SnapshotMetadata("TEST", "2026-07-25T00:00:00+09:00", days[-1].isoformat(), False)
    snap = DailyBarSnapshot(meta, "KOSPI", {s: "STOCK" for s in PILOT}, days, histories, market)
    path = Path(tmp) / "snap.json"
    save_snapshot(snap, path)
    return path, days


def _disclosure_provider(signal_date):
    item = EvidenceItem(
        evidence_id="dart:005930:20260716000100", kind="disclosure", symbol="005930",
        published_at=datetime(signal_date.year, signal_date.month, signal_date.day, 9, tzinfo=KST),
        summary="주요사항보고서(유상증자결정)",
    )

    def provider(symbol, sd):
        return (item,) if symbol == "005930" else ()

    return provider


class ForwardDisclosureTests(unittest.TestCase):
    def test_render_includes_disclosure_when_provider_supplied(self):
        with TemporaryDirectory() as tmp:
            snap, days = _write_snapshot(tmp)
            sd = days[-1]
            prompt = render_from_snapshot(
                snap, sd, PILOT, window=6, disclosure_provider=_disclosure_provider(sd),
            )
            self.assertIn("dart:005930:20260716000100", prompt)
            self.assertIn("주요사항보고서", prompt)

    def test_render_price_only_without_provider(self):
        with TemporaryDirectory() as tmp:
            snap, days = _write_snapshot(tmp)
            prompt = render_from_snapshot(snap, days[-1], PILOT, window=6)
            self.assertNotIn("dart:", prompt)

    def test_recorded_brief_can_be_cited_with_disclosure_evidence(self):
        with TemporaryDirectory() as tmp:
            snap, days = _write_snapshot(tmp)
            sd = days[-1]
            reply = ('[{"symbol": "005930", "action": "BUY", "conviction": "0.8", '
                     '"target_weight": "0.1", "rationale": "disclosure-driven", '
                     f'"cited_evidence": ["dart:005930:20260716000100", "px:005930:{sd.isoformat()}"]}}]')
            out = Path(tmp) / "signal.json"
            record = record_from_snapshot(
                snap, sd, PILOT, reply, eligible=frozenset(PILOT), model_id="claude/forward",
                decided_at=datetime(2026, 7, 24, 16, tzinfo=KST), output_path=out,
                window=6, disclosure_provider=_disclosure_provider(sd),
            )
            # citing the disclosure evidence is accepted (it is in the brief)
            self.assertEqual(record["admitted_symbols"], ["005930"])


if __name__ == "__main__":
    unittest.main()
