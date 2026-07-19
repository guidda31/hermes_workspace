"""TDD contracts for conservative local KRX XLSX normalization."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from swing_v2.krx_xlsx_normalizer import normalize_krx_xlsx
from swing_v2.universe_metadata import load_universe_metadata, select_eligible_universe


def write_xlsx(path: Path, headers: list[str], rows: list[list[object]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(path)


class KrxXlsxNormalizerTest(unittest.TestCase):
    def test_normalizes_common_stock_with_forward_only_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            stocks = directory / "data_1847_20260718.xlsx"
            detail = directory / "data_1909_20260718.xlsx"
            basic = directory / "data_1944_20260718.xlsx"
            output = directory / "normalized.json"
            manifest = directory / "normalized.manifest.json"
            write_xlsx(stocks, ["단축코드", "상장일", "시장구분", "증권구분", "소속부", "주식종류"], [
                ["005930", "1975/06/11", "KOSPI", "주권", "", "보통주"],
            ])
            write_xlsx(detail, ["단축코드", "기초시장분류", "기초자산분류", "추적배수"], [])
            write_xlsx(basic, ["종목코드", "상장일", "분류체계"], [])

            result = normalize_krx_xlsx(stocks, detail, basic, output, manifest, as_of=date(2026, 7, 18))
            snapshot = load_universe_metadata(output)

            self.assertEqual(result.record_count, 1)
            self.assertTrue(manifest.is_file())
            self.assertEqual(snapshot.records[0].symbol, "005930")
            self.assertEqual(snapshot.records[0].asset_type.value, "STOCK")
            self.assertEqual(snapshot.records[0].effective_from, date(2026, 7, 18))
            self.assertEqual(snapshot.records[0].provenance.as_of, date(2026, 7, 18))
            self.assertEqual(select_eligible_universe(snapshot, date(2026, 7, 17)).symbols, ())
            self.assertEqual(select_eligible_universe(snapshot, date(2026, 7, 18)).symbols, ("005930",))

    def test_preserves_conservative_stock_and_etf_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            stocks = directory / "data_1847_20260718.xlsx"
            detail = directory / "data_1909_20260718.xlsx"
            basic = directory / "data_1944_20260718.xlsx"
            output = directory / "normalized.json"
            manifest = directory / "normalized.manifest.json"
            write_xlsx(stocks, ["단축코드", "상장일", "시장구분", "증권구분", "소속부", "주식종류"], [
                ["000001", "", "KOSPI", "주권", "", "보통주"],
                ["000002", "", "KOSPI", "주권", "관리종목(소속부없음)", "보통주"],
                ["000003", "", "KOSDAQ", "주권", "SPAC(소속부없음)", "보통주"],
                ["000004", "", "KOSPI", "ETN", "", ""],
            ])
            write_xlsx(detail, ["단축코드", "기초시장분류", "기초자산분류", "추적배수"], [
                ["100001", "국내", "주식", "일반"],
                ["100002", "국내", "주식", "2X 레버리지"],
                ["100003", "해외", "주식", "일반"],
                ["100004", "국내", "주식", "일반"],
            ])
            write_xlsx(basic, ["종목코드", "상장일", "분류체계"], [
                ["100001", "", "주식-시장대표"],
                ["100002", "", "주식-업종섹터-반도체"],
                ["100003", "", "주식-시장대표"],
                ["100004", "", "채권-국공채"],
            ])

            normalize_krx_xlsx(stocks, detail, basic, output, manifest, as_of=date(2026, 7, 18))
            records = {record.symbol: record for record in load_universe_metadata(output).records}
            selection = select_eligible_universe(load_universe_metadata(output), date(2026, 7, 18))

            self.assertEqual(records["000002"].asset_type.value, "STOCK")
            self.assertEqual({flag.value for flag in records["000002"].flags}, {"MANAGEMENT_ISSUE"})
            self.assertEqual(records["000003"].asset_type.value, "SPAC")
            self.assertEqual(records["000004"].asset_type.value, "UNKNOWN")
            self.assertEqual(records["100001"].asset_type.value, "ETF")
            self.assertEqual(records["100001"].etf_exposure.value, "DOMESTIC_INDEX_OR_SECTOR")
            self.assertEqual({flag.value for flag in records["100002"].flags}, {"ETF_LEVERAGED"})
            self.assertEqual({flag.value for flag in records["100003"].flags}, {"ETF_FOREIGN_INDEX"})
            self.assertEqual(records["100004"].asset_type.value, "UNKNOWN")
            self.assertEqual(selection.symbols, ("000001", "100001"))

    def test_rejects_etf_detail_and_basic_code_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            stocks = directory / "data_1847_20260718.xlsx"
            detail = directory / "data_1909_20260718.xlsx"
            basic = directory / "data_1944_20260718.xlsx"
            write_xlsx(stocks, ["단축코드", "상장일", "시장구분", "증권구분", "소속부", "주식종류"], [])
            write_xlsx(detail, ["단축코드", "기초시장분류", "기초자산분류", "추적배수"], [["100001", "국내", "주식", "일반"]])
            write_xlsx(basic, ["종목코드", "상장일", "분류체계"], [["100002", "", "주식-시장대표"]])

            with self.assertRaisesRegex(ValueError, "ETF detail/basic code mismatch"):
                normalize_krx_xlsx(stocks, detail, basic, directory / "out.json", directory / "out.manifest.json", as_of=date(2026, 7, 18))

    def test_accepts_alphanumeric_krx_etf_short_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            stocks = directory / "data_1847_20260718.xlsx"
            detail = directory / "data_1909_20260718.xlsx"
            basic = directory / "data_1944_20260718.xlsx"
            write_xlsx(stocks, ["단축코드", "상장일", "시장구분", "증권구분", "소속부", "주식종류"], [])
            write_xlsx(detail, ["단축코드", "기초시장분류", "기초자산분류", "추적배수"], [["0193M0", "국내", "주식", "일반"]])
            write_xlsx(basic, ["종목코드", "상장일", "분류체계"], [["0193M0", "", "주식-시장대표"]])
            output = directory / "out.json"

            normalize_krx_xlsx(stocks, detail, basic, output, directory / "out.manifest.json", as_of=date(2026, 7, 18))

            self.assertEqual(load_universe_metadata(output).records[0].symbol, "0193M0")
