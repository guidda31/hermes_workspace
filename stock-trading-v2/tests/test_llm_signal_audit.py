"""Tests for the immutable signal-only audit record.

Because Hermes' judgment cannot be pinned/replayed like a temperature-0 API call,
auditability is the substitute: every trading day we durably record exactly what the
agent saw (the brief), what it decided, which decisions were admitted/rejected, the
model id, and the KST timestamp. The record must be tamper-evident, write-once, and
must contain NO order / fill / position / quantity / cash field — this is not trading.
"""

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest_data import DailyBarSnapshot, SnapshotBacktestData, SnapshotMetadata
from swing_v2.contracts import DailyBar
from swing_v2.llm.brief import build_brief
from swing_v2.llm.decision import DecisionAction, SymbolDecision
from swing_v2.llm.guardrail import GuardrailConfig, PortfolioContext, apply_guardrails
from swing_v2.llm.signal_audit import (
    build_signal_audit,
    load_signal_audit,
    write_signal_audit,
)


KST = timezone(timedelta(hours=9))
PILOT = ("005930", "000660")
_FORBIDDEN_KEYS = {
    "order_id", "fill_id", "position_id", "quantity", "filled_quantity",
    "price", "fill_price", "cash", "cash_delta", "notional", "shares",
}


def _bar(day, symbol, asset_type, close):
    close = Decimal(close)
    return DailyBar(day, symbol, asset_type, close, close, close, close, 1_000_000, close * 1_000_000, True)


def _make_data(last_day=date(2026, 7, 16), n=6):
    days = []
    cursor = last_day
    while len(days) < n:
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
    return SnapshotBacktestData(snapshot), days


def _sample_record():
    data, days = _make_data()
    brief = build_brief(data, signal_date=days[-1], symbols=PILOT)
    decisions = (
        SymbolDecision("005930", DecisionAction.BUY, Decimal("0.8"), Decimal("0.1"), "momentum", ()),
    )
    plan = apply_guardrails(
        decisions,
        portfolio=PortfolioContext(frozenset(), False),
        config=GuardrailConfig(eligible_symbols=frozenset(PILOT)),
    )
    record = build_signal_audit(
        brief=brief,
        decisions=decisions,
        plan=plan,
        model_id="openai/gpt-5.5",
        decided_at=datetime(2026, 7, 16, 16, 0, tzinfo=KST),
    )
    return record


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


class SignalAuditTests(unittest.TestCase):
    def test_record_captures_brief_decisions_model_and_time(self):
        record = _sample_record()
        self.assertEqual(record["signal_date"], "2026-07-16")
        self.assertEqual(record["model_id"], "openai/gpt-5.5")
        self.assertEqual(record["decided_at"], "2026-07-16T16:00:00+09:00")
        self.assertEqual(len(record["decisions"]), 1)
        self.assertEqual(record["admitted_symbols"], ["005930"])
        self.assertIn("brief_digest", record)
        self.assertIn("integrity", record)

    def test_record_contains_no_order_or_position_fields(self):
        record = _sample_record()
        self.assertEqual(_FORBIDDEN_KEYS & set(_walk_keys(record)), set())

    def test_write_is_immutable_and_reloads_with_verified_digest(self):
        record = _sample_record()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal-2026-07-16.json"
            write_signal_audit(record, path)
            reloaded = load_signal_audit(path)
            self.assertEqual(reloaded["signal_date"], "2026-07-16")

    def test_write_refuses_to_overwrite_existing_file(self):
        record = _sample_record()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal-2026-07-16.json"
            write_signal_audit(record, path)
            with self.assertRaises(ValueError):
                write_signal_audit(record, path)

    def test_tampered_file_fails_integrity_check(self):
        record = _sample_record()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal-2026-07-16.json"
            write_signal_audit(record, path)
            payload = json.loads(path.read_text())
            payload["admitted_symbols"] = ["999999"]  # tamper, digest not updated
            path.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                load_signal_audit(path)

    def test_brief_digest_is_deterministic(self):
        self.assertEqual(_sample_record()["brief_digest"], _sample_record()["brief_digest"])

    def test_rejected_decisions_are_recorded_with_reasons(self):
        data, days = _make_data()
        brief = build_brief(data, signal_date=days[-1], symbols=PILOT)
        decisions = (
            SymbolDecision("999999", DecisionAction.BUY, Decimal("0.8"), Decimal("0.1"), "x", ()),
        )
        # 999999 not eligible -> rejected. (not in brief universe, but guardrail sees the raw decision)
        plan = apply_guardrails(
            decisions,
            portfolio=PortfolioContext(frozenset(), False),
            config=GuardrailConfig(eligible_symbols=frozenset(PILOT)),
        )
        record = build_signal_audit(
            brief=brief, decisions=decisions, plan=plan,
            model_id="openai/gpt-5.5", decided_at=datetime(2026, 7, 16, 16, 0, tzinfo=KST),
        )
        self.assertEqual(record["admitted_symbols"], [])
        self.assertEqual(len(record["rejected"]), 1)
        self.assertEqual(record["rejected"][0]["symbol"], "999999")
        self.assertIn("reason", record["rejected"][0])


if __name__ == "__main__":
    unittest.main()
