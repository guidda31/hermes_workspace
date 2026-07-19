"""Tests for the injected stdlib HTTP transport for DART (opendart.fss.or.kr).

These exercise the concrete ``HttpGet``-compatible transport that lets the DART
disclosure provider and corp-code loader talk to opendart.fss.or.kr. No test
touches the network: ``url_opener`` is always a fake callable returning canned
bytes and supporting the context-manager protocol like ``urlopen`` does. The
transport must build the query string (including ``crtfc_key``), parse JSON to a
dict, return raw ZIP bytes as-is, fail closed on non-JSON / HTTP error / timeout,
and NEVER leak the secret ``crtfc_key`` in any raised message.
"""

import unittest
import urllib.parse

from swing_v2.llm.dart_transport import (
    DART_CORP_CODE_URL,
    dart_http_get,
    fetch_corp_code_zip,
)


API_KEY = "0123456789abcdef0123456789abcdef01234567"


class _FakeResponse:
    """A urlopen-like context manager whose ``.read()`` yields canned bytes."""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Callable stand-in for ``urllib.request.urlopen`` recording every call."""

    def __init__(self, body):
        self._body = body
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        return _FakeResponse(self._body)


class _RaisingOpener:
    """Opener that simulates an HTTP error / URL error / timeout."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        raise self._exc


class DartHttpGetTests(unittest.TestCase):
    def test_params_become_query_string_including_crtfc_key(self):
        opener = _FakeOpener(b'{"status": "000"}')
        params = {"crtfc_key": API_KEY, "corp_code": "00126380", "bgn_de": "20240101"}
        dart_http_get("https://opendart.fss.or.kr/api/list.json", params, url_opener=opener)
        self.assertEqual(len(opener.calls), 1)
        called_url = opener.calls[0][0]
        base, _, query = called_url.partition("?")
        self.assertEqual(base, "https://opendart.fss.or.kr/api/list.json")
        self.assertEqual(urllib.parse.parse_qs(query), {
            "crtfc_key": [API_KEY],
            "corp_code": ["00126380"],
            "bgn_de": ["20240101"],
        })

    def test_json_body_parsed_to_dict(self):
        opener = _FakeOpener(b'{"status": "000", "list": [{"rcept_no": "1"}]}')
        result = dart_http_get("https://opendart.fss.or.kr/api/list.json",
                               {"crtfc_key": API_KEY}, url_opener=opener)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "000")
        self.assertEqual(result["list"], [{"rcept_no": "1"}])

    def test_non_json_body_raises_value_error(self):
        opener = _FakeOpener(b"<html>not json</html>")
        with self.assertRaises(ValueError):
            dart_http_get("https://opendart.fss.or.kr/api/list.json",
                          {"crtfc_key": API_KEY}, url_opener=opener)

    def test_non_object_json_raises_value_error(self):
        opener = _FakeOpener(b"[1, 2, 3]")
        with self.assertRaises(ValueError):
            dart_http_get("https://opendart.fss.or.kr/api/list.json",
                          {"crtfc_key": API_KEY}, url_opener=opener)

    def test_http_error_raises_value_error_without_api_key(self):
        opener = _RaisingOpener(OSError("boom " + API_KEY))
        with self.assertRaises(ValueError) as ctx:
            dart_http_get("https://opendart.fss.or.kr/api/list.json",
                          {"crtfc_key": API_KEY}, url_opener=opener)
        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_timeout_error_raises_value_error_without_api_key(self):
        opener = _RaisingOpener(TimeoutError("timed out"))
        with self.assertRaises(ValueError) as ctx:
            dart_http_get("https://opendart.fss.or.kr/api/list.json",
                          {"crtfc_key": API_KEY}, url_opener=opener)
        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_timeout_is_passed_through(self):
        opener = _FakeOpener(b"{}")
        dart_http_get("https://opendart.fss.or.kr/api/list.json",
                      {"crtfc_key": API_KEY}, url_opener=opener, timeout=3.5)
        self.assertEqual(opener.calls[0][1], 3.5)

    def test_default_timeout_is_passed_through(self):
        opener = _FakeOpener(b"{}")
        dart_http_get("https://opendart.fss.or.kr/api/list.json",
                      {"crtfc_key": API_KEY}, url_opener=opener)
        self.assertEqual(opener.calls[0][1], 10.0)


class FetchCorpCodeZipTests(unittest.TestCase):
    def test_returns_raw_zip_bytes_as_is(self):
        zip_bytes = b"PK\x03\x04canned-zip-bytes"
        opener = _FakeOpener(zip_bytes)
        result = fetch_corp_code_zip(API_KEY, url_opener=opener)
        self.assertEqual(result, zip_bytes)

    def test_requests_corp_code_url_with_crtfc_key(self):
        opener = _FakeOpener(b"PK\x03\x04")
        fetch_corp_code_zip(API_KEY, url_opener=opener)
        called_url = opener.calls[0][0]
        base, _, query = called_url.partition("?")
        self.assertEqual(base, DART_CORP_CODE_URL)
        self.assertEqual(urllib.parse.parse_qs(query), {"crtfc_key": [API_KEY]})

    def test_timeout_is_passed_through(self):
        opener = _FakeOpener(b"PK\x03\x04")
        fetch_corp_code_zip(API_KEY, url_opener=opener, timeout=7.0)
        self.assertEqual(opener.calls[0][1], 7.0)

    def test_http_error_raises_value_error_without_api_key(self):
        opener = _RaisingOpener(OSError("boom " + API_KEY))
        with self.assertRaises(ValueError) as ctx:
            fetch_corp_code_zip(API_KEY, url_opener=opener)
        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_empty_api_key_raises_value_error(self):
        with self.assertRaises(ValueError):
            fetch_corp_code_zip("", url_opener=_FakeOpener(b"PK"))


if __name__ == "__main__":
    unittest.main()
