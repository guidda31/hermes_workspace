"""Tests for the injected-transport DART disclosure evidence provider.

The provider turns opendart.fss.or.kr ``list.json`` records into PIT
``EvidenceItem`` values for the brief layer. It never touches the network:
the HTTP transport is an injected callable and every test feeds it canned
dicts. The provider must map ``stock_code`` -> symbol correctly, fail closed on
malformed or error responses, and never leak the injected API key.
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.dart_disclosures import (
    DART_LIST_URL,
    make_dart_disclosure_provider,
)


KST = timezone(timedelta(hours=9))
API_KEY = "0123456789abcdef0123456789abcdef01234567"
SIGNAL_DATE = date(2024, 5, 20)
CORP_CODES = {"005930": "00126380", "000660": "00164779"}


def _record(*, rcept_no="20240517000001", rcept_dt="20240517",
            report_nm="주요사항보고서(유상증자결정)", stock_code="005930",
            corp_name="삼성전자"):
    return {
        "rcept_no": rcept_no,
        "rcept_dt": rcept_dt,
        "report_nm": report_nm,
        "stock_code": stock_code,
        "corp_name": corp_name,
    }


def _payload(records, *, status="000"):
    body = {"status": status, "message": "정상"}
    if records is not None:
        body["list"] = records
    return body


def _capturing_transport(payload):
    """Fake http_get returning a canned dict and recording its arguments."""
    calls = []

    def http_get(url, params):
        calls.append((url, dict(params)))
        return payload

    return http_get, calls


def _provider(payload, *, window_days=60):
    http_get, calls = _capturing_transport(payload)
    provider = make_dart_disclosure_provider(
        http_get=http_get,
        api_key=API_KEY,
        corp_code_by_symbol=CORP_CODES,
        window_days=window_days,
    )
    return provider, calls


class DartDisclosureProviderTests(unittest.TestCase):
    def test_valid_record_maps_to_evidence_item(self):
        provider, _ = _provider(_payload([_record()]))
        items = provider("005930", SIGNAL_DATE)
        self.assertIsInstance(items, tuple)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIsInstance(item, EvidenceItem)
        self.assertEqual(item.evidence_id, "dart:005930:20240517000001")
        self.assertEqual(item.kind, "disclosure")
        self.assertEqual(item.symbol, "005930")
        self.assertEqual(item.summary, "주요사항보고서(유상증자결정)")
        self.assertEqual(item.published_at, datetime(2024, 5, 17, tzinfo=KST))

    def test_published_at_is_kst_midnight_of_rcept_dt(self):
        provider, _ = _provider(_payload([_record(rcept_dt="20240101")]))
        item = provider("005930", SIGNAL_DATE)[0]
        self.assertIsNotNone(item.published_at.tzinfo)
        self.assertEqual(item.published_at.utcoffset(), timedelta(hours=9))
        self.assertEqual(item.published_kst_date(), date(2024, 1, 1))

    def test_no_data_status_returns_empty(self):
        provider, _ = _provider(_payload(None, status="013"))
        self.assertEqual(provider("005930", SIGNAL_DATE), ())

    def test_error_status_raises(self):
        provider, _ = _provider(_payload(None, status="800"))
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_missing_status_raises(self):
        provider, _ = _provider({"message": "정상", "list": [_record()]})
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_malformed_record_missing_field_raises(self):
        bad = _record()
        del bad["report_nm"]
        provider, _ = _provider(_payload([bad]))
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_blank_field_raises(self):
        provider, _ = _provider(_payload([_record(rcept_no="")]))
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_bad_rcept_dt_raises(self):
        provider, _ = _provider(_payload([_record(rcept_dt="2024-05-17")]))
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_symbol_filtering_excludes_other_stock_codes(self):
        records = [
            _record(rcept_no="A1", stock_code="005930"),
            _record(rcept_no="B2", stock_code="000660", corp_name="SK하이닉스"),
            _record(rcept_no="A3", stock_code="005930"),
        ]
        provider, _ = _provider(_payload(records))
        items = provider("005930", SIGNAL_DATE)
        self.assertEqual({i.evidence_id for i in items},
                         {"dart:005930:A1", "dart:005930:A3"})
        self.assertTrue(all(i.symbol == "005930" for i in items))

    def test_api_key_passed_as_crtfc_key_and_never_leaked(self):
        provider, calls = _provider(_payload([_record()]))
        item = provider("005930", SIGNAL_DATE)[0]
        url, params = calls[0]
        self.assertEqual(url, DART_LIST_URL)
        self.assertEqual(params["crtfc_key"], API_KEY)
        for value in (item.evidence_id, item.symbol, item.summary,
                      item.kind, repr(item)):
            self.assertNotIn(API_KEY, value)

    def test_request_uses_corp_code_and_window(self):
        provider, calls = _provider(_payload([_record()]), window_days=30)
        provider("000660", SIGNAL_DATE)
        _, params = calls[0]
        self.assertEqual(params["corp_code"], "00164779")
        self.assertEqual(params["end_de"], "20240520")
        self.assertEqual(params["bgn_de"], "20240420")

    def test_unknown_symbol_raises(self):
        provider, _ = _provider(_payload([_record()]))
        with self.assertRaises(ValueError):
            provider("999999", SIGNAL_DATE)

    def test_bad_signal_date_type_raises(self):
        provider, _ = _provider(_payload([_record()]))
        with self.assertRaises(ValueError):
            provider("005930", "2024-05-20")

    def test_bad_symbol_type_raises(self):
        provider, _ = _provider(_payload([_record()]))
        with self.assertRaises(ValueError):
            provider(5930, SIGNAL_DATE)

    def test_missing_list_with_ok_status_raises(self):
        provider, _ = _provider({"status": "000", "message": "정상"})
        with self.assertRaises(ValueError):
            provider("005930", SIGNAL_DATE)

    def test_factory_rejects_blank_api_key(self):
        with self.assertRaises(ValueError):
            make_dart_disclosure_provider(
                http_get=lambda url, params: {},
                api_key="",
                corp_code_by_symbol=CORP_CODES,
            )

    def test_factory_rejects_nonpositive_window(self):
        with self.assertRaises(ValueError):
            make_dart_disclosure_provider(
                http_get=lambda url, params: {},
                api_key=API_KEY,
                corp_code_by_symbol=CORP_CODES,
                window_days=0,
            )


if __name__ == "__main__":
    unittest.main()
