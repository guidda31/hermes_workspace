"""Tests for the Naver News API adapter feeding the news EvidenceProvider.

No test touches the network. The concrete HTTP transport is injected as a fake
``http_get`` (returning a canned Naver-shaped dict) or, for the transport itself,
a fake ``url_opener``. The adapter must: look up the Korean company name for a
symbol, query Naver, strip ``<b>`` tags from titles, filter items to the
[begin, end] window by parsing ``pubDate``, and shape records for
``make_news_provider``. Client id/secret must never leak into any raised message.
"""

import unittest
import urllib.parse
from datetime import date

from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.naver_news import (
    NAVER_NEWS_URL,
    naver_http_get,
    naver_news_fetch,
    naver_news_provider_or_none,
)


CLIENT_ID = "test-client-id"
CLIENT_SECRET = "super-secret-value-never-log"
SIGNAL_DATE = date(2026, 7, 19)
NAME_BY_SYMBOL = {"005930": "삼성전자", "000660": "SK하이닉스"}


def _naver_item(*, title="<b>삼성전자</b> 실적 발표",
                pub_date="Mon, 13 Jul 2026 09:00:00 +0900",
                originallink="https://news.example.com/a1",
                description="본문 요약"):
    return {
        "title": title,
        "originallink": originallink,
        "link": "https://n.news.naver.com/a1",
        "pubDate": pub_date,
        "description": description,
    }


def _payload(*items):
    return {"lastBuildDate": "Mon, 13 Jul 2026 10:00:00 +0900",
            "total": len(items), "start": 1, "display": len(items),
            "items": list(items)}


class _FakeHttpGet:
    """Records (url, headers) and returns a canned Naver-shaped payload."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        return self._payload


class NaverNewsFetchTests(unittest.TestCase):
    def test_returns_records_shaped_for_make_news_provider(self):
        http_get = _FakeHttpGet(_payload(_naver_item()))
        fetch = naver_news_fetch(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                 name_by_symbol=NAME_BY_SYMBOL, http_get=http_get)
        records = fetch("005930", date(2026, 7, 5), SIGNAL_DATE)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(set(record), {"title", "pubDate", "link"})
        self.assertEqual(record["pubDate"], "Mon, 13 Jul 2026 09:00:00 +0900")

    def test_bold_tags_stripped_from_title(self):
        http_get = _FakeHttpGet(_payload(_naver_item(title="<b>삼성</b>전자 <b>급등</b>")))
        fetch = naver_news_fetch(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                 name_by_symbol=NAME_BY_SYMBOL, http_get=http_get)
        record = fetch("005930", date(2026, 7, 5), SIGNAL_DATE)[0]
        self.assertEqual(record["title"], "삼성전자 급등")
        self.assertNotIn("<b>", record["title"])

    def test_out_of_window_items_filtered(self):
        in_window = _naver_item(pub_date="Mon, 13 Jul 2026 09:00:00 +0900",
                                originallink="https://news.example.com/in")
        too_old = _naver_item(pub_date="Fri, 03 Jul 2026 09:00:00 +0900",
                              originallink="https://news.example.com/old")
        too_new = _naver_item(pub_date="Mon, 20 Jul 2026 09:00:00 +0900",
                              originallink="https://news.example.com/new")
        http_get = _FakeHttpGet(_payload(in_window, too_old, too_new))
        fetch = naver_news_fetch(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                 name_by_symbol=NAME_BY_SYMBOL, http_get=http_get)
        records = fetch("005930", date(2026, 7, 5), SIGNAL_DATE)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["link"], "https://news.example.com/in")

    def test_unknown_symbol_returns_empty(self):
        http_get = _FakeHttpGet(_payload(_naver_item()))
        fetch = naver_news_fetch(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                 name_by_symbol=NAME_BY_SYMBOL, http_get=http_get)
        self.assertEqual(fetch("999999", date(2026, 7, 5), SIGNAL_DATE), ())
        self.assertEqual(http_get.calls, [])

    def test_query_url_encodes_korean_name_with_sort_and_display(self):
        http_get = _FakeHttpGet(_payload())
        fetch = naver_news_fetch(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                 name_by_symbol=NAME_BY_SYMBOL, http_get=http_get)
        fetch("005930", date(2026, 7, 5), SIGNAL_DATE)
        url, headers = http_get.calls[0]
        base, _, query = url.partition("?")
        self.assertEqual(base, NAVER_NEWS_URL)
        parsed = urllib.parse.parse_qs(query)
        self.assertEqual(parsed["query"], ["삼성전자"])
        self.assertEqual(parsed["sort"], ["date"])
        self.assertEqual(parsed["display"], ["20"])

    def test_credentials_sent_in_headers(self):
        http_get = _FakeHttpGet(_payload())
        fetch = naver_news_fetch(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                 name_by_symbol=NAME_BY_SYMBOL, http_get=http_get)
        fetch("005930", date(2026, 7, 5), SIGNAL_DATE)
        _, headers = http_get.calls[0]
        self.assertEqual(headers["X-Naver-Client-Id"], CLIENT_ID)
        self.assertEqual(headers["X-Naver-Client-Secret"], CLIENT_SECRET)

    def test_secret_never_in_raised_error(self):
        http_get = _FakeHttpGet({"items": "not-a-list"})
        fetch = naver_news_fetch(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                 name_by_symbol=NAME_BY_SYMBOL, http_get=http_get)
        with self.assertRaises(ValueError) as ctx:
            fetch("005930", date(2026, 7, 5), SIGNAL_DATE)
        self.assertNotIn(CLIENT_SECRET, str(ctx.exception))
        self.assertNotIn(CLIENT_ID, str(ctx.exception))

    def test_rejects_blank_client_id(self):
        with self.assertRaises(ValueError):
            naver_news_fetch(client_id="", client_secret=CLIENT_SECRET,
                             name_by_symbol=NAME_BY_SYMBOL, http_get=_FakeHttpGet(_payload()))

    def test_rejects_non_callable_http_get(self):
        with self.assertRaises(ValueError):
            naver_news_fetch(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                             name_by_symbol=NAME_BY_SYMBOL, http_get="nope")


class NaverNewsProviderThroughBriefTests(unittest.TestCase):
    def test_item_becomes_news_evidence_item(self):
        http_get = _FakeHttpGet(_payload(_naver_item()))
        provider = naver_news_provider_or_none(
            symbols=["005930"], name_by_symbol=NAME_BY_SYMBOL,
            env={"NAVER_CLIENT_ID": CLIENT_ID, "NAVER_CLIENT_SECRET": CLIENT_SECRET},
            http_get=http_get,
        )
        self.assertIsNotNone(provider)
        items = provider("005930", SIGNAL_DATE)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIsInstance(item, EvidenceItem)
        self.assertEqual(item.kind, "news")
        self.assertEqual(item.symbol, "005930")
        self.assertEqual(item.summary, "삼성전자 실적 발표")
        self.assertIsNotNone(item.published_at.tzinfo)


class NaverNewsProviderOrNoneTests(unittest.TestCase):
    def test_missing_client_id_returns_none(self):
        provider = naver_news_provider_or_none(
            symbols=["005930"], name_by_symbol=NAME_BY_SYMBOL,
            env={"NAVER_CLIENT_SECRET": CLIENT_SECRET},
        )
        self.assertIsNone(provider)

    def test_missing_client_secret_returns_none(self):
        provider = naver_news_provider_or_none(
            symbols=["005930"], name_by_symbol=NAME_BY_SYMBOL,
            env={"NAVER_CLIENT_ID": CLIENT_ID},
        )
        self.assertIsNone(provider)

    def test_blank_credentials_return_none(self):
        provider = naver_news_provider_or_none(
            symbols=["005930"], name_by_symbol=NAME_BY_SYMBOL,
            env={"NAVER_CLIENT_ID": "", "NAVER_CLIENT_SECRET": ""},
        )
        self.assertIsNone(provider)

    def test_both_credentials_present_builds_provider(self):
        provider = naver_news_provider_or_none(
            symbols=["005930"], name_by_symbol=NAME_BY_SYMBOL,
            env={"NAVER_CLIENT_ID": CLIENT_ID, "NAVER_CLIENT_SECRET": CLIENT_SECRET},
            http_get=_FakeHttpGet(_payload()),
        )
        self.assertTrue(callable(provider))


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, body):
        self._body = body
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        return _FakeResponse(self._body)


class _RaisingOpener:
    def __init__(self, exc):
        self._exc = exc

    def __call__(self, request, timeout=None):
        raise self._exc


class NaverHttpGetTests(unittest.TestCase):
    def test_parses_json_body_to_dict(self):
        opener = _FakeOpener(b'{"items": [{"title": "x"}]}')
        result = naver_http_get(NAVER_NEWS_URL + "?query=%EC%82%BC%EC%84%B1",
                                {"X-Naver-Client-Id": CLIENT_ID,
                                 "X-Naver-Client-Secret": CLIENT_SECRET},
                                url_opener=opener)
        self.assertEqual(result, {"items": [{"title": "x"}]})

    def test_headers_carried_on_request_not_url(self):
        opener = _FakeOpener(b"{}")
        naver_http_get(NAVER_NEWS_URL + "?query=x",
                       {"X-Naver-Client-Id": CLIENT_ID,
                        "X-Naver-Client-Secret": CLIENT_SECRET},
                       url_opener=opener)
        request = opener.calls[0][0]
        self.assertEqual(request.get_header("X-naver-client-secret"), CLIENT_SECRET)
        self.assertNotIn(CLIENT_SECRET, request.full_url)

    def test_http_error_raises_without_secret(self):
        opener = _RaisingOpener(OSError("boom " + CLIENT_SECRET))
        with self.assertRaises(ValueError) as ctx:
            naver_http_get(NAVER_NEWS_URL + "?query=x",
                           {"X-Naver-Client-Id": CLIENT_ID,
                            "X-Naver-Client-Secret": CLIENT_SECRET},
                           url_opener=opener)
        self.assertNotIn(CLIENT_SECRET, str(ctx.exception))

    def test_non_json_body_raises(self):
        opener = _FakeOpener(b"<html>nope</html>")
        with self.assertRaises(ValueError):
            naver_http_get(NAVER_NEWS_URL, {"X-Naver-Client-Id": CLIENT_ID,
                                            "X-Naver-Client-Secret": CLIENT_SECRET},
                           url_opener=opener)

    def test_timeout_passed_through(self):
        opener = _FakeOpener(b"{}")
        naver_http_get(NAVER_NEWS_URL, {"X-Naver-Client-Id": CLIENT_ID,
                                        "X-Naver-Client-Secret": CLIENT_SECRET},
                       url_opener=opener, timeout=3.5)
        self.assertEqual(opener.calls[0][1], 3.5)


if __name__ == "__main__":
    unittest.main()
