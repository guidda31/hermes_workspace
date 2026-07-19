"""Tests for the DART ``CORPCODE.xml`` corp-code loader.

DART's ``/api/corpCode.xml`` endpoint returns a ZIP whose single ``CORPCODE.xml``
entry has a ``<result>`` root with many ``<list>`` rows. Each row carries an
8-digit ``<corp_code>``, a ``<corp_name>``, a 6-digit ``<stock_code>`` for LISTED
companies (blank/space for unlisted), and a ``<modify_date>``. Only listed rows
matter, and they produce the ``{stock_code: corp_code}`` mapping consumed by
``make_dart_disclosure_provider``. These tests build the XML/zip bytes inline and
never touch the network.
"""

import io
import unittest
import zipfile

from swing_v2.llm.dart_corp_codes import (
    load_corp_codes_from_zip,
    parse_corp_code_xml,
)


def _row(*, corp_code, corp_name, stock_code, modify_date="20240517"):
    return (
        "  <list>"
        f"<corp_code>{corp_code}</corp_code>"
        f"<corp_name>{corp_name}</corp_name>"
        f"<stock_code>{stock_code}</stock_code>"
        f"<modify_date>{modify_date}</modify_date>"
        "</list>"
    )


def _xml(rows):
    body = "".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<result>{body}</result>"
    ).encode("utf-8")


def _zip(xml_bytes, *, name="CORPCODE.xml"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, xml_bytes)
    return buffer.getvalue()


LISTED_SAMSUNG = _row(corp_code="00126380", corp_name="삼성전자", stock_code="005930")
LISTED_HYNIX = _row(corp_code="00164779", corp_name="SK하이닉스", stock_code="000660")
# Unlisted rows carry a blank/space stock_code and dominate the real dump.
UNLISTED_BLANK = _row(corp_code="00434003", corp_name="비상장회사", stock_code="")
UNLISTED_SPACE = _row(corp_code="00434004", corp_name="비상장회사2", stock_code=" ")


class ParseCorpCodeXmlTests(unittest.TestCase):
    def test_listed_rows_parsed_to_symbol_corp_code(self):
        mapping = parse_corp_code_xml(_xml([LISTED_SAMSUNG, LISTED_HYNIX]))
        self.assertEqual(mapping, {"005930": "00126380", "000660": "00164779"})

    def test_unlisted_rows_skipped_not_raised(self):
        mapping = parse_corp_code_xml(
            _xml([LISTED_SAMSUNG, UNLISTED_BLANK, UNLISTED_SPACE])
        )
        self.assertEqual(mapping, {"005930": "00126380"})

    def test_returns_plain_str_keys_and_values(self):
        mapping = parse_corp_code_xml(_xml([LISTED_SAMSUNG]))
        (symbol, corp_code), = mapping.items()
        self.assertIs(type(symbol), str)
        self.assertIs(type(corp_code), str)

    def test_malformed_xml_raises(self):
        with self.assertRaises(ValueError):
            parse_corp_code_xml(b"<result><list><corp_code>0012")

    def test_non_bytes_input_raises(self):
        with self.assertRaises(ValueError):
            parse_corp_code_xml("<result></result>")

    def test_wrong_root_tag_raises(self):
        bad = b'<?xml version="1.0"?><wrong></wrong>'
        with self.assertRaises(ValueError):
            parse_corp_code_xml(bad)

    def test_listed_row_missing_corp_code_raises(self):
        bad = (
            "  <list>"
            "<corp_name>삼성전자</corp_name>"
            "<stock_code>005930</stock_code>"
            "</list>"
        )
        with self.assertRaises(ValueError):
            parse_corp_code_xml(_xml([bad]))

    def test_listed_row_blank_corp_code_raises(self):
        bad = _row(corp_code="", corp_name="삼성전자", stock_code="005930")
        with self.assertRaises(ValueError):
            parse_corp_code_xml(_xml([bad]))

    def test_non_8_digit_corp_code_raises(self):
        bad = _row(corp_code="1234567", corp_name="짧은코드", stock_code="005930")
        with self.assertRaises(ValueError):
            parse_corp_code_xml(_xml([bad]))

    def test_non_digit_corp_code_raises(self):
        bad = _row(corp_code="0012638X", corp_name="비숫자", stock_code="005930")
        with self.assertRaises(ValueError):
            parse_corp_code_xml(_xml([bad]))

    def test_non_6_digit_stock_code_raises(self):
        bad = _row(corp_code="00126380", corp_name="삼성전자", stock_code="5930")
        with self.assertRaises(ValueError):
            parse_corp_code_xml(_xml([bad]))

    def test_duplicate_stock_code_same_corp_code_ok(self):
        mapping = parse_corp_code_xml(_xml([LISTED_SAMSUNG, LISTED_SAMSUNG]))
        self.assertEqual(mapping, {"005930": "00126380"})

    def test_conflicting_duplicate_stock_code_raises(self):
        conflict = _row(corp_code="99999999", corp_name="사칭", stock_code="005930")
        with self.assertRaises(ValueError):
            parse_corp_code_xml(_xml([LISTED_SAMSUNG, conflict]))

    def test_empty_result_is_empty_mapping(self):
        self.assertEqual(parse_corp_code_xml(_xml([])), {})


class LoadCorpCodesFromZipTests(unittest.TestCase):
    def test_zip_round_trip(self):
        zip_bytes = _zip(_xml([LISTED_SAMSUNG, LISTED_HYNIX, UNLISTED_BLANK]))
        mapping = load_corp_codes_from_zip(zip_bytes)
        self.assertEqual(mapping, {"005930": "00126380", "000660": "00164779"})

    def test_zip_with_differently_named_xml_entry(self):
        zip_bytes = _zip(_xml([LISTED_SAMSUNG]), name="dump.xml")
        self.assertEqual(load_corp_codes_from_zip(zip_bytes), {"005930": "00126380"})

    def test_non_bytes_zip_raises(self):
        with self.assertRaises(ValueError):
            load_corp_codes_from_zip("not-bytes")

    def test_corrupt_zip_raises(self):
        with self.assertRaises(ValueError):
            load_corp_codes_from_zip(b"not-a-zip-archive")

    def test_zip_without_xml_entry_raises(self):
        zip_bytes = _zip(b"just text", name="README.txt")
        with self.assertRaises(ValueError):
            load_corp_codes_from_zip(zip_bytes)

    def test_zip_with_multiple_xml_entries_raises(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("a.xml", _xml([LISTED_SAMSUNG]))
            archive.writestr("b.xml", _xml([LISTED_HYNIX]))
        with self.assertRaises(ValueError):
            load_corp_codes_from_zip(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
