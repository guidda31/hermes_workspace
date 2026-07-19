"""Tests for news_provider_or_none: graceful name-map + key resolution for news."""

import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.providers import news_provider_or_none


class NewsProviderOrNoneTests(unittest.TestCase):
    def test_none_without_name_map(self):
        self.assertIsNone(news_provider_or_none(symbols=("005930",), env={"NAVER_CLIENT_ID": "i", "NAVER_CLIENT_SECRET": "s"}))

    def test_none_without_naver_keys(self):
        self.assertIsNone(news_provider_or_none(
            symbols=("005930",), name_by_symbol={"005930": "삼성전자"}, env={},
        ))

    def test_builds_from_name_cache_and_keys(self):
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "names.json"
            cache.write_text(json.dumps({"005930": "삼성전자"}, ensure_ascii=False))

            def fake_http_get(url, headers):
                return {"items": [{
                    "title": "삼성전자 <b>실적</b> 발표", "link": "http://n/1",
                    "pubDate": "Wed, 15 Jul 2026 09:00:00 +0900",
                }]}

            provider = news_provider_or_none(
                symbols=("005930",), name_cache_path=cache,
                env={"NAVER_CLIENT_ID": "i", "NAVER_CLIENT_SECRET": "s"}, http_get=fake_http_get,
            )
            self.assertIsNotNone(provider)
            items = provider("005930", date(2026, 7, 17))
            self.assertTrue(items)
            self.assertIsInstance(items[0], EvidenceItem)
            self.assertEqual(items[0].kind, "news")
            self.assertNotIn("<b>", items[0].summary)  # tags stripped by the adapter


if __name__ == "__main__":
    unittest.main()
