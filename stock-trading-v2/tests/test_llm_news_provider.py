"""Tests for the source-agnostic news evidence provider.

The provider turns generic news records (Mappings) into PIT ``EvidenceItem``
values for the brief layer. It never touches the network: the source query is an
injected ``fetch`` callable and every test feeds it canned dicts. The publication
time is the load-bearing invariant — a record whose publication timestamp is
missing, naive, or unparseable must be rejected fail-closed, never assumed.
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.news_provider import make_news_provider


KST = timezone(timedelta(hours=9))
UTC = timezone.utc
SIGNAL_DATE = date(2026, 7, 19)


def _iso_record(*, title="유상증자 결정 보도", published_at="2026-07-13T09:00:00+09:00",
                url="https://news.example.com/a1"):
    return {"title": title, "published_at": published_at, "url": url}


def _rfc_record(*, title="실적 발표 기사", pub_date="Mon, 13 Jul 2026 09:00:00 +0900",
                url="https://news.example.com/b2"):
    return {"title": title, "pubDate": pub_date, "url": url}


def _capturing_fetch(records):
    """Fake fetch returning canned dicts and recording its (symbol, begin, end)."""
    calls = []

    def fetch(symbol, begin_date, end_date):
        calls.append((symbol, begin_date, end_date))
        return records

    return fetch, calls


def _provider(records, *, window_days=14, kind="news"):
    fetch, calls = _capturing_fetch(records)
    provider = make_news_provider(fetch=fetch, window_days=window_days, kind=kind)
    return provider, calls


class NewsProviderTests(unittest.TestCase):
    def test_iso_tz_record_parses(self):
        provider, _ = _provider([_iso_record()])
        items = provider("005930", SIGNAL_DATE)
        self.assertIsInstance(items, tuple)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIsInstance(item, EvidenceItem)
        self.assertEqual(item.kind, "news")
        self.assertEqual(item.symbol, "005930")
        self.assertEqual(item.summary, "유상증자 결정 보도")
        self.assertEqual(item.published_at, datetime(2026, 7, 13, 9, 0, tzinfo=KST))
        self.assertIsNotNone(item.published_at.tzinfo)

    def test_iso_z_suffix_parses(self):
        provider, _ = _provider([_iso_record(published_at="2026-07-13T00:00:00Z")])
        item = provider("005930", SIGNAL_DATE)[0]
        self.assertEqual(item.published_at, datetime(2026, 7, 13, tzinfo=UTC))

    def test_rfc_1123_record_parses(self):
        provider, _ = _provider([_rfc_record()])
        item = provider("005930", SIGNAL_DATE)[0]
        self.assertEqual(item.published_at, datetime(2026, 7, 13, 9, 0, tzinfo=KST))
        self.assertEqual(item.published_at.utcoffset(), timedelta(hours=9))
        self.assertEqual(item.summary, "실적 발표 기사")

    def test_naive_iso_timestamp_raises(self):
        provider, _ = _provider([_iso_record(published_at="2026-07-13T09:00:00")])
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_missing_timestamp_raises(self):
        record = {"title": "제목만 있는 기사", "url": "https://news.example.com/x"}
        provider, _ = _provider([record])
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_blank_timestamp_raises(self):
        provider, _ = _provider([_iso_record(published_at="")])
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_unparseable_timestamp_raises(self):
        provider, _ = _provider([_iso_record(published_at="yesterday")])
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_missing_title_raises(self):
        provider, _ = _provider([{"published_at": "2026-07-13T09:00:00+09:00",
                                   "url": "https://news.example.com/x"}])
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_window_and_begin_date_passed_to_fetch(self):
        provider, calls = _provider([_iso_record()], window_days=14)
        provider("005930", SIGNAL_DATE)
        symbol, begin_date, end_date = calls[0]
        self.assertEqual(symbol, "005930")
        self.assertEqual(end_date, SIGNAL_DATE)
        self.assertEqual(begin_date, date(2026, 7, 5))

    def test_custom_window_changes_begin_date(self):
        provider, calls = _provider([_iso_record()], window_days=30)
        provider("005930", SIGNAL_DATE)
        _, begin_date, _ = calls[0]
        self.assertEqual(begin_date, date(2026, 6, 19))

    def test_symbol_on_item_is_requested_symbol(self):
        provider, _ = _provider([_iso_record()])
        item = provider("000660", SIGNAL_DATE)[0]
        self.assertEqual(item.symbol, "000660")

    def test_injected_id_used_for_evidence_id(self):
        record = _iso_record()
        record["id"] = "NAVER-42"
        provider, _ = _provider([record])
        item = provider("005930", SIGNAL_DATE)[0]
        self.assertEqual(item.evidence_id, "news:005930:NAVER-42")

    def test_derived_evidence_id_is_stable_for_same_input(self):
        provider_a, _ = _provider([_iso_record()])
        provider_b, _ = _provider([_iso_record()])
        id_a = provider_a("005930", SIGNAL_DATE)[0].evidence_id
        id_b = provider_b("005930", SIGNAL_DATE)[0].evidence_id
        self.assertEqual(id_a, id_b)
        self.assertTrue(id_a.startswith("news:005930:"))
        self.assertEqual(len(id_a.split(":")[-1]), 16)

    def test_derived_evidence_ids_are_unique_across_items(self):
        records = [
            _iso_record(url="https://news.example.com/one"),
            _iso_record(url="https://news.example.com/two"),
        ]
        provider, _ = _provider(records)
        items = provider("005930", SIGNAL_DATE)
        self.assertEqual(len({i.evidence_id for i in items}), 2)

    def test_kind_is_news(self):
        provider, _ = _provider([_iso_record()])
        self.assertEqual(provider("005930", SIGNAL_DATE)[0].kind, "news")

    def test_empty_fetch_returns_empty_tuple(self):
        provider, _ = _provider([])
        self.assertEqual(provider("005930", SIGNAL_DATE), ())

    def test_link_field_accepted_as_url(self):
        record = {"title": "링크 필드 기사", "published_at": "2026-07-13T09:00:00+09:00",
                  "link": "https://news.example.com/linked"}
        provider, _ = _provider([record])
        item = provider("005930", SIGNAL_DATE)[0]
        self.assertTrue(item.evidence_id.startswith("news:005930:"))

    def test_non_mapping_record_raises(self):
        provider, _ = _provider(["not a mapping"])
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_bad_symbol_type_raises(self):
        provider, _ = _provider([_iso_record()])
        with self.assertRaises(ValueError):
            provider(5930, SIGNAL_DATE)

    def test_bad_signal_date_type_raises(self):
        provider, _ = _provider([_iso_record()])
        with self.assertRaises(ValueError):
            provider("005930", "2026-07-19")

    def test_factory_rejects_non_callable_fetch(self):
        with self.assertRaises(ValueError):
            make_news_provider(fetch="not callable")

    def test_factory_rejects_nonpositive_window(self):
        with self.assertRaises(ValueError):
            make_news_provider(fetch=lambda s, b, e: [], window_days=0)

    def test_factory_rejects_bad_kind(self):
        with self.assertRaises(ValueError):
            make_news_provider(fetch=lambda s, b, e: [], kind="disclosure")


if __name__ == "__main__":
    unittest.main()
