"""Tests for the evidence-provider factory that wires real DART/news into the brief.

Keys and transports are injected; a thin ``*_from_env`` wrapper reads the key from the
environment (raising if absent) but never hits the network in tests. Assembling a
provider makes no request — only invoking it would, and tests inject fakes.
"""

import io
import unittest
import zipfile
from datetime import date
from decimal import Decimal

from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.providers import (
    dart_provider,
    dart_provider_from_env,
    fetch_corp_codes,
    news_provider,
)


_CORP_XML = (
    "<result>"
    "<list><corp_code>00126380</corp_code><corp_name>SS</corp_name>"
    "<stock_code>005930</stock_code><modify_date>20260101</modify_date></list>"
    "</result>"
).encode("utf-8")


def _zip_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", _CORP_XML)
    return buffer.getvalue()


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DartProviderTests(unittest.TestCase):
    def test_dart_provider_maps_a_disclosure_to_evidence(self):
        def fake_http_get(url, params):
            return {"status": "000", "list": [{
                "stock_code": "005930", "rcept_no": "20260115000123",
                "rcept_dt": "20260115", "report_nm": "earnings",
            }]}

        provider = dart_provider(
            api_key="secret", corp_code_by_symbol={"005930": "00126380"}, http_get=fake_http_get,
        )
        items = provider("005930", date(2026, 7, 16))
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], EvidenceItem)
        self.assertEqual(items[0].evidence_id, "dart:005930:20260115000123")

    def test_fetch_corp_codes_via_injected_opener(self):
        codes = fetch_corp_codes(api_key="secret", url_opener=lambda url, timeout=None: _FakeResp(_zip_bytes()))
        self.assertEqual(codes, {"005930": "00126380"})

    def test_from_env_without_key_raises(self):
        with self.assertRaises(ValueError):
            dart_provider_from_env(corp_code_by_symbol={"005930": "00126380"}, env={})

    def test_from_env_with_key_and_codes_builds_without_network(self):
        provider = dart_provider_from_env(
            corp_code_by_symbol={"005930": "00126380"}, env={"OPENDART_API_KEY": "secret"},
        )
        self.assertTrue(callable(provider))


class NewsProviderTests(unittest.TestCase):
    def test_news_provider_wraps_injected_fetch(self):
        def fake_fetch(symbol, begin, end):
            return [{"title": "up", "published_at": "2026-07-15T09:00:00+09:00", "url": "http://x/1"}]

        provider = news_provider(fetch=fake_fetch)
        items = provider("005930", date(2026, 7, 16))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "news")


if __name__ == "__main__":
    unittest.main()
