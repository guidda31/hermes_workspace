"""TDD coverage for the read-only KIS KOSPI daily-index adapter and collector."""

from datetime import date
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from swing_v2.contracts import DailyBar
from swing_v2.kis import KisClient, KisCredentials, PageRequestBudget
from swing_v2.kis_market_index import (
    KOSPI_INDEX_CODE,
    KOSPI_MARKET_SYMBOL,
    collect_kospi_market_index_snapshot,
    load_kospi_market_index_snapshot,
)


D = Decimal


def _row(day: str, price: str) -> dict[str, str]:
    return {
        "stck_bsop_date": day,
        "bstp_nmix_oprc": price,
        "bstp_nmix_hgpr": price,
        "bstp_nmix_lwpr": price,
        "bstp_nmix_prpr": price,
        "acml_vol": "123",
        "acml_tr_pbmn": "456",
    }


class KisKospiDailyIndexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credentials = KisCredentials(app_key="test-app-key", app_secret="test-app-secret")

    def test_load_kospi_daily_bars_sends_official_chart_request_and_normalizes_output(self) -> None:
        response = MagicMock()
        response.json.return_value = {"rt_cd": "0", "output2": [_row("20240103", "2510.50"), _row("20240102", "2500.25")]}
        session = MagicMock()
        session.get.return_value = response

        bars = KisClient(credentials=self.credentials, session=session).load_kospi_daily_bars(
            "test-access-token", date(2024, 1, 2), date(2024, 1, 3)
        )

        self.assertEqual(KOSPI_INDEX_CODE, "0001")
        self.assertEqual(KOSPI_MARKET_SYMBOL, "KOSPI")
        self.assertEqual([bar.trade_date for bar in bars], [date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(bars[0], DailyBar(date(2024, 1, 2), "KOSPI", "INDEX", D("2500.25"), D("2500.25"), D("2500.25"), D("2500.25"), 123, D("456"), True))
        session.get.assert_called_once_with(
            "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
            headers={"authorization": "Bearer test-access-token", "appkey": "test-app-key", "appsecret": "test-app-secret", "tr_id": "FHKUP03500100"},
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001", "FID_INPUT_DATE_1": "20240102", "FID_INPUT_DATE_2": "20240103", "FID_PERIOD_DIV_CODE": "D"},
        )
        response.raise_for_status.assert_called_once_with()

    def test_kospi_pages_backwards_and_applies_budget_to_each_http_get(self) -> None:
        newest = [_row("20240103", "3"), _row("20240102", "2")] + [_row("20240102", "2")] * 98
        older = [_row("20240101", "1")]
        session = MagicMock()
        responses = []
        for page in (newest, older):
            response = MagicMock()
            response.json.return_value = {"rt_cd": "0", "output2": page}
            responses.append(response)
        session.get.side_effect = responses
        slept: list[float] = []

        bars = KisClient(credentials=self.credentials, session=session).load_kospi_daily_bars(
            "test-access-token", date(2024, 1, 1), date(2024, 1, 3),
            page_request_limiter=PageRequestBudget(max_requests=2, delay_seconds=0.25, sleep=slept.append),
        )

        self.assertEqual([bar.trade_date for bar in bars], [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["FID_INPUT_DATE_2"], "20240101")
        self.assertEqual(slept, [0.25])

    def test_kospi_continues_after_a_short_page_when_the_oldest_bar_is_after_start(self) -> None:
        first = MagicMock()
        first.json.return_value = {"rt_cd": "0", "output2": [_row("20240103", "3"), _row("20240102", "2")]}
        second = MagicMock()
        second.json.return_value = {"rt_cd": "0", "output2": [_row("20240101", "1")]}
        session = MagicMock()
        session.get.side_effect = (first, second)

        bars = KisClient(credentials=self.credentials, session=session).load_kospi_daily_bars(
            "test-access-token", date(2024, 1, 1), date(2024, 1, 3),
            page_request_limiter=PageRequestBudget(max_requests=2, delay_seconds=0.25, sleep=lambda _: None),
        )

        self.assertEqual([bar.trade_date for bar in bars], [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["FID_INPUT_DATE_2"], "20240101")

    def test_kospi_rejects_malformed_or_error_payload_before_returning_bars(self) -> None:
        for payload, message in (
            ({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "bad request"}, "EGW00123"),
            ({"rt_cd": "0", "output2": [{"stck_bsop_date": "20240102"}]}, "invalid OHLCV"),
        ):
            with self.subTest(payload=payload):
                response = MagicMock()
                response.json.return_value = payload
                session = MagicMock()
                session.get.return_value = response
                with self.assertRaisesRegex(ValueError, message):
                    KisClient(credentials=self.credentials, session=session).load_kospi_daily_bars(
                        "test-access-token", date(2024, 1, 2), date(2024, 1, 3)
                    )


class KospiMarketIndexCollectorTests(unittest.TestCase):
    def test_collector_writes_immutable_kospi_artifact_with_identity_source_bounds_and_digest(self) -> None:
        class Client:
            def load_kospi_daily_bars(self, token: str, start: date, end: date, *, page_request_limiter):
                self.call = (token, start, end, page_request_limiter)
                page_request_limiter.before_page_request()
                return (DailyBar(date(2024, 1, 2), "KOSPI", "INDEX", D("2500"), D("2501"), D("2499"), D("2500.5"), 123, D("456"), True),)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = Client()
            result = collect_kospi_market_index_snapshot(
                client=client, access_token="token", start=date(2024, 1, 1), end=date(2024, 1, 3),
                output_path=root, delay_seconds=0.25, max_requests=1,
            )
            payload = json.loads(result.path.read_text(encoding="utf-8"))

            self.assertTrue(result.complete)
            self.assertEqual(payload["source"], "KIS OpenAPI domestic daily index chart (KOSPI code 0001)")
            self.assertEqual(payload["market_symbol"], "KOSPI")
            self.assertEqual(payload["index_code"], "0001")
            self.assertEqual(payload["requested_start"], "2024-01-01")
            self.assertEqual(payload["requested_end"], "2024-01-03")
            self.assertEqual(payload["observed_start"], "2024-01-02")
            self.assertEqual(payload["observed_end"], "2024-01-02")
            self.assertEqual(result.sha256, hashlib.sha256(result.path.read_bytes()).hexdigest())
            self.assertEqual(client.call[:3], ("token", date(2024, 1, 1), date(2024, 1, 3)))
            self.assertEqual(load_kospi_market_index_snapshot(result.path)[0].close, D("2500.5"))

    def test_collector_fails_closed_without_overwriting_existing_artifact_or_network(self) -> None:
        class Client:
            def load_kospi_daily_bars(self, *args, **kwargs):
                raise AssertionError("must not fetch")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "KOSPI.json"
            path.write_text("immutable", encoding="utf-8")
            original = path.read_bytes()
            result = collect_kospi_market_index_snapshot(
                client=Client(), access_token="token", start=date(2024, 1, 1), end=date(2024, 1, 3),
                output_path=root, delay_seconds=0.25, max_requests=1,
            )

            self.assertFalse(result.complete)
            self.assertIn("immutable", result.error)
            self.assertEqual(path.read_bytes(), original)


class KospiMarketIndexCliPreflightTests(unittest.TestCase):
    def _write_valid_artifact(self, root: Path, *, start: date = date(2024, 1, 1), end: date = date(2024, 1, 3)) -> Path:
        class Client:
            def load_kospi_daily_bars(self, *args, **kwargs):
                return (DailyBar(date(2024, 1, 2), "KOSPI", "INDEX", D("2500"), D("2501"), D("2499"), D("2500.5"), 123, D("456"), True),)

        result = collect_kospi_market_index_snapshot(
            client=Client(), access_token="token", start=start, end=end,
            output_path=root, delay_seconds=0.25, max_requests=1,
        )
        self.assertTrue(result.complete)
        return result.path

    def test_cli_reuses_exact_valid_output_directory_artifact_without_dotenv_client_or_token(self) -> None:
        from swing_v2 import kis_market_index

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "requested-output-directory"
            path = self._write_valid_artifact(root)
            forbidden_dotenv = types.ModuleType("dotenv")
            forbidden_dotenv.load_dotenv = MagicMock(side_effect=AssertionError("dotenv must not load"))
            client_class = MagicMock(side_effect=AssertionError("client must not be created"))
            stdout = io.StringIO()
            with patch.dict(sys.modules, {"dotenv": forbidden_dotenv}), patch("swing_v2.kis.KisClient", client_class), redirect_stdout(stdout):
                exit_code = kis_market_index.main(["--start", "2024-01-01", "--end", "2024-01-03", "--output", str(root)])

            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(result["complete"])
            self.assertEqual(result["path"], str(path.resolve()))
            self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            forbidden_dotenv.load_dotenv.assert_not_called()
            client_class.assert_not_called()

    def test_cli_rejects_existing_artifact_for_different_request_without_dotenv_client_token_or_overwrite(self) -> None:
        from swing_v2 import kis_market_index

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "requested-output-directory"
            path = self._write_valid_artifact(root, start=date(2024, 1, 1), end=date(2024, 1, 3))
            original = path.read_bytes()
            forbidden_dotenv = types.ModuleType("dotenv")
            forbidden_dotenv.load_dotenv = MagicMock(side_effect=AssertionError("dotenv must not load"))
            client_class = MagicMock(side_effect=AssertionError("client must not be created"))
            stdout = io.StringIO()
            with patch.dict(sys.modules, {"dotenv": forbidden_dotenv}), patch("swing_v2.kis.KisClient", client_class), redirect_stdout(stdout):
                exit_code = kis_market_index.main(["--start", "2024-01-02", "--end", "2024-01-03", "--output", str(root)])

            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(result["complete"])
            self.assertIn("request", result["error"])
            self.assertEqual(path.read_bytes(), original)
            forbidden_dotenv.load_dotenv.assert_not_called()
            client_class.assert_not_called()
    def test_cli_rejects_corrupt_existing_artifact_without_dotenv_client_token_or_overwrite(self) -> None:
        from swing_v2 import kis_market_index

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "requested-output-directory"
            root.mkdir()
            path = root / "KOSPI.json"
            path.write_text("not JSON", encoding="utf-8")
            original = path.read_bytes()
            forbidden_dotenv = types.ModuleType("dotenv")
            forbidden_dotenv.load_dotenv = MagicMock(side_effect=AssertionError("dotenv must not load"))
            client_class = MagicMock(side_effect=AssertionError("client must not be created"))
            stdout = io.StringIO()
            with patch.dict(sys.modules, {"dotenv": forbidden_dotenv}), patch("swing_v2.kis.KisClient", client_class), redirect_stdout(stdout):
                exit_code = kis_market_index.main(["--start", "2024-01-01", "--end", "2024-01-03", "--output", str(root)])

            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(result["complete"])
            self.assertIn("invalid immutable", result["error"])
            self.assertEqual(path.read_bytes(), original)
            forbidden_dotenv.load_dotenv.assert_not_called()
            client_class.assert_not_called()

    def test_cli_passes_kis_token_cache_environment_path_to_cache_aware_token_method(self) -> None:
        from swing_v2 import kis_market_index

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            fake_client = MagicMock()
            fake_client.get_access_token.return_value = "token"
            client_class = MagicMock(return_value=fake_client)
            result = kis_market_index.MarketIndexCollectionResult(True, root / "KOSPI.json", None, None, None, None)
            with patch.dict(os.environ, {
                "KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret", "KIS_TOKEN_CACHE": ".cache/kis_token.json",
            }, clear=True), patch("dotenv.load_dotenv"), patch("swing_v2.kis.KisClient", client_class), patch.object(kis_market_index, "collect_kospi_market_index_snapshot", return_value=result):
                exit_code = kis_market_index.main(["--start", "2024-01-01", "--end", "2024-01-03", "--output", str(root)])

        self.assertEqual(exit_code, 0)
        fake_client.get_access_token.assert_called_once_with(cache_path=Path(".cache/kis_token.json"))


if __name__ == "__main__":
    unittest.main()
