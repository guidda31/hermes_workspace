"""Tests for the env-driven DART disclosure provider used by the forward pipeline.

Graceful by design: no OPENDART key -> None (the brief stays price-only, nothing
breaks). With a key it builds a real provider over an injected transport; corp codes
are cached locally so they are not re-fetched every run.
"""

import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.providers import dart_disclosure_provider_or_none


class DartWiringTests(unittest.TestCase):
    def test_no_key_returns_none(self):
        self.assertIsNone(dart_disclosure_provider_or_none(symbols=("005930",), env={}))

    def test_with_key_and_cached_corp_codes_builds_provider(self):
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "corp_codes.json"
            cache.write_text(json.dumps({"005930": "00126380", "000660": "00164779"}))

            def fake_http_get(url, params):
                return {"status": "000", "list": [{
                    "stock_code": "005930", "rcept_no": "20260716000100",
                    "rcept_dt": "20260716", "report_nm": "주요사항보고서",
                }]}

            provider = dart_disclosure_provider_or_none(
                symbols=("005930", "000660"), env={"OPENDART_API_KEY": "k"},
                cache_path=cache, http_get=fake_http_get,
            )
            self.assertIsNotNone(provider)
            items = provider("005930", date(2026, 7, 17))
            self.assertEqual(len(items), 1)
            self.assertIsInstance(items[0], EvidenceItem)
            self.assertEqual(items[0].kind, "disclosure")

    def test_fetches_and_caches_corp_codes_when_cache_absent(self):
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "corp_codes.json"
            calls = {"n": 0}

            def fake_url_opener(url, timeout=None):
                calls["n"] += 1
                import io, zipfile
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    z.writestr("CORPCODE.xml",
                               b"<result><list><corp_code>00126380</corp_code><corp_name>SS</corp_name>"
                               b"<stock_code>005930</stock_code><modify_date>20260101</modify_date></list></result>")

                class R:
                    def read(self_):
                        return buf.getvalue()
                    def __enter__(self_):
                        return self_
                    def __exit__(self_, *a):
                        return False
                return R()

            provider = dart_disclosure_provider_or_none(
                symbols=("005930",), env={"OPENDART_API_KEY": "k"},
                cache_path=cache, url_opener=fake_url_opener, http_get=lambda u, p: {"status": "013"},
            )
            self.assertIsNotNone(provider)
            self.assertTrue(cache.exists())  # corp codes were cached
            self.assertEqual(json.loads(cache.read_text()), {"005930": "00126380"})


if __name__ == "__main__":
    unittest.main()
