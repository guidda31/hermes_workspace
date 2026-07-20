"""Tests for the daily risk-watch CLI (defensive disclosure/news monitor)."""

import unittest
from datetime import date, datetime, timedelta, timezone

from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.risk_cli import format_digest, watch_risks
from swing_v2.llm.risk_screen import Severity

KST = timezone(timedelta(hours=9))


def _item(eid, summary, symbol, kind="disclosure"):
    return EvidenceItem(eid, kind, symbol, datetime(2026, 7, 20, 9, tzinfo=KST), summary)


def _provider(items_by_symbol):
    def provider(symbol, as_of):
        return tuple(items_by_symbol.get(symbol, ()))
    return provider


class WatchRisksTests(unittest.TestCase):
    def test_flags_across_symbols_sorted_high_first(self):
        disc = _provider({
            "005380": [_item("d1", "생산중단", "005380")],
            "000660": [_item("d2", "주요사항보고서(유상증자결정)", "000660")],
            "035420": [_item("d3", "기업설명회(IR)개최", "035420")],  # not a risk
        })
        flags = watch_risks(symbols=("005380", "000660", "035420"),
                            disclosure_provider=disc, news_provider=None, as_of=date(2026, 7, 20))
        self.assertEqual([f.symbol for f in flags], ["005380", "000660"])  # HIGH before MEDIUM
        self.assertIs(flags[0].severity, Severity.HIGH)

    def test_screens_news_items_too(self):
        news = _provider({"005380": [_item("n1", "현대차 일부 라인 생산중단 검토", "005380", kind="news")]})
        flags = watch_risks(symbols=("005380",), disclosure_provider=None,
                            news_provider=news, as_of=date(2026, 7, 20))
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].category, "PRODUCTION_HALT")

    def test_no_providers_returns_empty(self):
        flags = watch_risks(symbols=("005380",), disclosure_provider=None,
                            news_provider=None, as_of=date(2026, 7, 20))
        self.assertEqual(flags, [])

    def test_digest_lists_high_and_medium_sections(self):
        disc = _provider({
            "005380": [_item("d1", "생산중단", "005380")],
            "000660": [_item("d2", "주요사항보고서(유상증자결정)", "000660")],
        })
        flags = watch_risks(symbols=("005380", "000660"), disclosure_provider=disc,
                            news_provider=None, as_of=date(2026, 7, 20))
        digest = format_digest(flags, names={"005380": "현대차", "000660": "SK하이닉스"}, as_of=date(2026, 7, 20))
        self.assertIn("HIGH", digest)
        self.assertIn("현대차", digest)
        self.assertIn("PRODUCTION_HALT", digest)
        self.assertIn("2026-07-20", digest)

    def test_digest_when_no_flags_says_clear(self):
        digest = format_digest([], names={}, as_of=date(2026, 7, 20))
        self.assertRegex(digest, r"no material|없음|clear|None")


if __name__ == "__main__":
    unittest.main()
