"""Tests for the point-in-time (PIT) brief the strategy hands to Hermes.

The brief is the ONLY thing the agent gets to see for a signal date. Its defining
safety property: nothing published or observed after the signal date may appear in
it. Price bars are bounded by the existing no-lookahead snapshot query; disclosures
and news arrive through injected providers and are filtered by publication time, and
any evidence item without a timezone-aware publication time is rejected fail-closed.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from swing_v2.backtest_data import (
    DailyBarSnapshot,
    SnapshotBacktestData,
    SnapshotMetadata,
)
from swing_v2.contracts import DailyBar
from swing_v2.llm.brief import EvidenceItem, build_brief
from swing_v2.llm.decision import parse_symbol_decision


KST = timezone(timedelta(hours=9))
PILOT = ("005930", "000660")


def _bar(day, symbol, asset_type, close, *, tradable=True, volume=1_000_000):
    close = Decimal(close)
    return DailyBar(
        trade_date=day,
        symbol=symbol,
        asset_type=asset_type,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        trading_value=close * volume,
        is_tradable=tradable,
    )


def _make_data(last_day=date(2026, 7, 16), n=6):
    """Build a small in-memory snapshot of n ascending weekday sessions."""
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
    metadata = SnapshotMetadata(
        source="TEST",
        retrieved_at="2026-07-17T00:00:00+09:00",
        data_as_of=days[-1].isoformat(),
        trading_value_is_close_times_volume_proxy=True,
    )
    snapshot = DailyBarSnapshot(
        metadata=metadata,
        market_symbol="KS11",
        asset_types={"005930": "STOCK", "000660": "STOCK"},
        trade_calendar=days,
        histories=histories,
        market_history=market,
    )
    return SnapshotBacktestData(snapshot), days


class BuildBriefBasicsTests(unittest.TestCase):
    def test_brief_reports_signal_date_and_pilot_symbols(self):
        data, days = _make_data()
        brief = build_brief(data, signal_date=days[-1], symbols=PILOT)
        self.assertEqual(brief.signal_date, days[-1])
        self.assertEqual(brief.known_symbols, frozenset(PILOT))

    def test_symbol_summary_uses_close_at_signal_date_not_later(self):
        data, days = _make_data(n=8)
        signal_date = days[3]  # deliberately before the last available session
        brief = build_brief(data, signal_date=signal_date, symbols=PILOT)
        summary = {s.symbol: s for s in brief.symbols}
        # close at days[3] for 005930 is 70000 + 3*100
        self.assertEqual(summary["005930"].latest_close, Decimal("70300"))
        # no bar from a later day leaks in
        for s in brief.symbols:
            self.assertLessEqual(s.latest_trade_date, signal_date)

    def test_signal_date_absent_from_calendar_is_rejected(self):
        data, days = _make_data()
        with self.assertRaises(ValueError):
            build_brief(data, signal_date=date(2026, 7, 18), symbols=PILOT)  # a non-session day

    def test_price_evidence_ids_are_present(self):
        data, days = _make_data()
        brief = build_brief(data, signal_date=days[-1], symbols=PILOT)
        for symbol in PILOT:
            self.assertIn(f"px:{symbol}:{days[-1].isoformat()}", brief.known_evidence_ids)


class BriefDisclosureNewsPitTests(unittest.TestCase):
    def _provider(self, items_by_symbol):
        def fetch(symbol, signal_date):
            return items_by_symbol.get(symbol, ())
        return fetch

    def test_disclosure_published_on_or_before_signal_date_is_included(self):
        data, days = _make_data()
        signal_date = days[-1]
        item = EvidenceItem(
            evidence_id="dart:005930:0001",
            kind="disclosure",
            symbol="005930",
            published_at=datetime(2026, 7, 16, 9, 0, tzinfo=KST),
            summary="quarterly earnings up",
        )
        brief = build_brief(
            data,
            signal_date=signal_date,
            symbols=PILOT,
            disclosure_provider=self._provider({"005930": [item]}),
        )
        self.assertIn("dart:005930:0001", brief.known_evidence_ids)

    def test_future_published_item_is_excluded(self):
        data, days = _make_data()
        signal_date = days[-1]
        future = EvidenceItem(
            evidence_id="news:005930:future",
            kind="news",
            symbol="005930",
            published_at=datetime(2026, 7, 17, 9, 0, tzinfo=KST),  # after signal_date
            summary="leaked next-day move",
        )
        brief = build_brief(
            data,
            signal_date=signal_date,
            symbols=PILOT,
            news_provider=self._provider({"005930": [future]}),
        )
        self.assertNotIn("news:005930:future", brief.known_evidence_ids)

    def test_item_without_timezone_is_rejected_fail_closed(self):
        data, days = _make_data()
        with self.assertRaises(ValueError):
            EvidenceItem(
                evidence_id="dart:005930:naive",
                kind="disclosure",
                symbol="005930",
                published_at=datetime(2026, 7, 16, 9, 0),  # naive, no tzinfo
                summary="missing tz",
            )

    def test_decision_citing_excluded_future_evidence_is_rejected(self):
        data, days = _make_data()
        signal_date = days[-1]
        future = EvidenceItem(
            evidence_id="news:005930:future",
            kind="news",
            symbol="005930",
            published_at=datetime(2026, 7, 17, 9, 0, tzinfo=KST),
            summary="leaked",
        )
        brief = build_brief(
            data,
            signal_date=signal_date,
            symbols=PILOT,
            news_provider=self._provider({"005930": [future]}),
        )
        raw = {
            "symbol": "005930",
            "action": "BUY",
            "conviction": "0.7",
            "target_weight": "0.1",
            "rationale": "acting on leaked future news",
            "cited_evidence": ["news:005930:future"],
        }
        with self.assertRaises(ValueError):
            parse_symbol_decision(
                raw,
                known_symbols=brief.known_symbols,
                known_evidence_ids=brief.known_evidence_ids,
            )


if __name__ == "__main__":
    unittest.main()
