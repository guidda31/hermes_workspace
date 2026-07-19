"""TDD coverage for resumable, read-only KIS daily-price collection."""

from datetime import date
from decimal import Decimal
import io
import os
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from swing_v2.contracts import DailyBar
from swing_v2.kis_snapshot_collector import collect_daily_snapshot


D = Decimal


def bar(day: date, symbol: str, asset_type: str = "STOCK") -> DailyBar:
    return DailyBar(day, symbol, asset_type, D("10"), D("11"), D("9"), D("10"), 1, D("10"), True)


class FakeDailyClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str, date, date]] = []

    def load_domestic_daily_bars(self, token: str, symbol: str, asset_type: str, start: date, end: date):
        self.calls.append((token, symbol, asset_type, start, end))
        response = self.responses[symbol]
        if isinstance(response, Exception):
            raise response
        return response


class KisSnapshotCollectorTests(unittest.TestCase):
    def test_cli_rejects_expected_atomic_temporary_artifact_without_dotenv_client_token_or_overwrite(self) -> None:
        from swing_v2 import kis_snapshot_collector

        for temporary_name in ("manifest.json.tmp", "005930.json.tmp", "000660.json.tmp"):
            with self.subTest(temporary_name=temporary_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "requested-output-directory"
                root.mkdir()
                temporary_path = root / temporary_name
                temporary_path.write_bytes(b"incomplete atomic write")
                original = temporary_path.read_bytes()
                forbidden_dotenv = types.ModuleType("dotenv")
                forbidden_dotenv.load_dotenv = MagicMock(side_effect=AssertionError("dotenv must not load"))
                client_class = MagicMock(side_effect=AssertionError("client/token must not be created"))
                stdout = io.StringIO()
                with patch.dict(sys.modules, {"dotenv": forbidden_dotenv}), patch("swing_v2.kis.KisClient", client_class), redirect_stdout(stdout):
                    exit_code = kis_snapshot_collector.main([
                        "--symbol", "005930:STOCK", "--symbol", "000660:STOCK", "--start", "2024-01-01", "--end", "2024-01-03", "--output", str(root),
                    ])

                result = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 2)
                self.assertFalse(result["complete"])
                self.assertIn("temporary", result["errors"]["collection"])
                self.assertEqual(temporary_path.read_bytes(), original)
                forbidden_dotenv.load_dotenv.assert_not_called()
                client_class.assert_not_called()

    def test_cli_reuses_exact_valid_completed_collection_without_dotenv_client_or_token(self) -> None:
        from swing_v2 import kis_snapshot_collector

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "requested-output-directory"
            collect_daily_snapshot(
                client=FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),)}), access_token="seed-token",
                symbols=("005930",), asset_types={"005930": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 3),
                output_path=root, delay_seconds=0.1,
            )
            forbidden_dotenv = types.ModuleType("dotenv")
            forbidden_dotenv.load_dotenv = MagicMock(side_effect=AssertionError("dotenv must not load"))
            client_class = MagicMock(side_effect=AssertionError("client must not be created"))
            stdout = io.StringIO()
            with patch.dict(sys.modules, {"dotenv": forbidden_dotenv}), patch("swing_v2.kis.KisClient", client_class), redirect_stdout(stdout):
                exit_code = kis_snapshot_collector.main(["--symbol", "005930:STOCK", "--start", "2024-01-01", "--end", "2024-01-03", "--output", str(root)])

            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(result["complete"])
            self.assertEqual(result["manifest"], str((root / "manifest.json")))
            self.assertEqual(result["completed_symbols"], ["005930"])
            forbidden_dotenv.load_dotenv.assert_not_called()
            client_class.assert_not_called()

    def test_cli_rejects_hash_mismatched_completed_artifact_without_dotenv_client_token_or_overwrite(self) -> None:
        from swing_v2 import kis_snapshot_collector

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "requested-output-directory"
            collect_daily_snapshot(
                client=FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),)}), access_token="seed-token",
                symbols=("005930",), asset_types={"005930": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 3),
                output_path=root, delay_seconds=0.1,
            )
            symbol_path = root / "005930.json"
            symbol_path.write_text("tampered", encoding="utf-8")
            original = symbol_path.read_bytes()
            forbidden_dotenv = types.ModuleType("dotenv")
            forbidden_dotenv.load_dotenv = MagicMock(side_effect=AssertionError("dotenv must not load"))
            client_class = MagicMock(side_effect=AssertionError("client must not be created"))
            stdout = io.StringIO()
            with patch.dict(sys.modules, {"dotenv": forbidden_dotenv}), patch("swing_v2.kis.KisClient", client_class), redirect_stdout(stdout):
                exit_code = kis_snapshot_collector.main(["--symbol", "005930:STOCK", "--start", "2024-01-01", "--end", "2024-01-03", "--output", str(root)])

            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(result["complete"])
            self.assertIn("invalid immutable", result["errors"]["collection"])
            self.assertEqual(symbol_path.read_bytes(), original)
            forbidden_dotenv.load_dotenv.assert_not_called()
            client_class.assert_not_called()

    def test_cli_rejects_manifest_for_different_request_without_dotenv_client_token_or_overwrite(self) -> None:
        from swing_v2 import kis_snapshot_collector

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "requested-output-directory"
            collect_daily_snapshot(
                client=FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),)}), access_token="seed-token",
                symbols=("005930",), asset_types={"005930": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 3),
                output_path=root, delay_seconds=0.1,
            )
            original = (root / "manifest.json").read_bytes()
            forbidden_dotenv = types.ModuleType("dotenv")
            forbidden_dotenv.load_dotenv = MagicMock(side_effect=AssertionError("dotenv must not load"))
            client_class = MagicMock(side_effect=AssertionError("client must not be created"))
            stdout = io.StringIO()
            with patch.dict(sys.modules, {"dotenv": forbidden_dotenv}), patch("swing_v2.kis.KisClient", client_class), redirect_stdout(stdout):
                exit_code = kis_snapshot_collector.main(["--symbol", "005930:STOCK", "--start", "2024-01-02", "--end", "2024-01-03", "--output", str(root)])

            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(result["complete"])
            self.assertIn("does not match", result["errors"]["collection"])
            self.assertEqual((root / "manifest.json").read_bytes(), original)
            forbidden_dotenv.load_dotenv.assert_not_called()
            client_class.assert_not_called()

    def test_cli_rejects_incomplete_manifest_without_dotenv_client_token_or_overwrite(self) -> None:
        from swing_v2 import kis_snapshot_collector

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "requested-output-directory"
            collect_daily_snapshot(
                client=FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),)}), access_token="seed-token",
                symbols=("005930",), asset_types={"005930": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 3),
                output_path=root, delay_seconds=0.1,
            )
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["complete"] = False
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            original = manifest_path.read_bytes()
            forbidden_dotenv = types.ModuleType("dotenv")
            forbidden_dotenv.load_dotenv = MagicMock(side_effect=AssertionError("dotenv must not load"))
            client_class = MagicMock(side_effect=AssertionError("client must not be created"))
            stdout = io.StringIO()
            with patch.dict(sys.modules, {"dotenv": forbidden_dotenv}), patch("swing_v2.kis.KisClient", client_class), redirect_stdout(stdout):
                exit_code = kis_snapshot_collector.main(["--symbol", "005930:STOCK", "--start", "2024-01-01", "--end", "2024-01-03", "--output", str(root)])

            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(result["complete"])
            self.assertIn("exactly match", result["errors"]["collection"])
            self.assertEqual(manifest_path.read_bytes(), original)
            forbidden_dotenv.load_dotenv.assert_not_called()
            client_class.assert_not_called()

    def test_cli_passes_kis_token_cache_environment_path_to_cache_aware_token_method(self) -> None:
        from swing_v2 import kis_snapshot_collector

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            root.mkdir()
            fake_client = MagicMock()
            fake_client.get_access_token.return_value = "token"
            client_class = MagicMock(return_value=fake_client)
            result = kis_snapshot_collector.CollectionResult(True, root / "manifest.json", ("005930",), ("005930",), {})
            with patch.dict(os.environ, {
                "KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret", "KIS_TOKEN_CACHE": ".cache/kis_token.json",
            }, clear=True), patch("dotenv.load_dotenv"), patch("swing_v2.kis.KisClient", client_class), patch.object(kis_snapshot_collector, "collect_daily_snapshot", return_value=result):
                exit_code = kis_snapshot_collector.main(["--symbol", "005930:STOCK", "--start", "2024-01-01", "--end", "2024-01-03", "--output", str(root)])

        self.assertEqual(exit_code, 0)
        fake_client.get_access_token.assert_called_once_with(cache_path=Path(".cache/kis_token.json"))

    def test_passes_shared_page_budget_to_rate_aware_client_and_records_page_cap_failure(self) -> None:
        class PageAwareClient:
            def __init__(self) -> None:
                self.limiters = []

            def load_domestic_daily_bars(self, token: str, symbol: str, asset_type: str, start: date, end: date, *, page_request_limiter):
                self.limiters.append(page_request_limiter)
                page_request_limiter.before_page_request()
                page_request_limiter.before_page_request()
                return ()

        with tempfile.TemporaryDirectory() as temporary:
            client = PageAwareClient()
            result = collect_daily_snapshot(
                client=client, access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"},
                start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0.25,
                max_requests=1,
            )

        self.assertFalse(result.complete)
        self.assertEqual(len(client.limiters), 1)
        self.assertIn("page request cap", result.errors["005930"])

    def test_collects_explicit_symbol_to_immutable_file_and_records_observed_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeDailyClient({"005930": (bar(date(2024, 1, 3), "005930"),)})
            result = collect_daily_snapshot(
                client=client, access_token="test-token", symbols=("005930",), asset_types={"005930": "STOCK"},
                start=date(2024, 1, 1), end=date(2024, 1, 5), output_path=Path(temporary), delay_seconds=0.1,
            )

            manifest = json.loads((Path(temporary) / "manifest.json").read_text())
            entry = manifest["symbols"]["005930"]
            self.assertTrue(result.complete)
            self.assertEqual(client.calls, [("test-token", "005930", "STOCK", date(2024, 1, 1), date(2024, 1, 5))])
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["source"], "KIS OpenAPI domestic daily price (adjusted)")
            self.assertEqual(entry["requested_start"], "2024-01-01")
            self.assertEqual(entry["requested_end"], "2024-01-05")
            self.assertEqual(entry["observed_start"], "2024-01-03")
            self.assertEqual(entry["observed_end"], "2024-01-03")
            self.assertTrue(entry["sha256"])
            self.assertTrue((Path(temporary) / entry["file"]).is_file())

    def test_resume_skips_verified_complete_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),)})
            second = FakeDailyClient({"005930": AssertionError("must not fetch")})
            kwargs = dict(access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0.1)
            collect_daily_snapshot(client=first, **kwargs)
            result = collect_daily_snapshot(client=second, **kwargs)
            self.assertTrue(result.complete)
            self.assertEqual(second.calls, [])

    def test_resume_repairs_verified_orphan_file_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "schema_version": 1, "source": "KIS OpenAPI domestic daily price (adjusted)",
                "symbol": "005930", "asset_type": "STOCK", "requested_start": "2024-01-01",
                "requested_end": "2024-01-02", "observed_start": None,
                "observed_end": None, "bars": [],
            }
            orphan = root / "005930.json"
            orphan.write_text(json.dumps(payload), encoding="utf-8")
            client = FakeDailyClient({"005930": AssertionError("must not fetch")})

            result = collect_daily_snapshot(
                client=client, access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"},
                start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=root, delay_seconds=0.1,
            )

            manifest = json.loads((root / "manifest.json").read_text())
            self.assertTrue(result.complete)
            self.assertEqual(client.calls, [])
            self.assertEqual(manifest["symbols"]["005930"]["sha256"], hashlib.sha256(orphan.read_bytes()).hexdigest())

    def test_malformed_completed_bar_payload_fails_closed_without_network_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "schema_version": 1, "source": "KIS OpenAPI domestic daily price (adjusted)",
                "symbol": "005930", "asset_type": "STOCK", "requested_start": "2024-01-01",
                "requested_end": "2024-01-02", "observed_start": "not-a-date",
                "observed_end": "2024-01-02", "bars": [{"not": "a DailyBar JSON record"}],
            }
            symbol_file = root / "005930.json"
            symbol_file.write_text(json.dumps(payload), encoding="utf-8")
            digest = hashlib.sha256(symbol_file.read_bytes()).hexdigest()
            (root / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "source": "KIS OpenAPI domestic daily price (adjusted)",
                "requested_start": "2024-01-01", "requested_end": "2024-01-02", "complete": True,
                "symbols": {"005930": {
                    "status": "complete", "symbol": "005930", "asset_type": "STOCK", "file": "005930.json",
                    "sha256": digest, "requested_start": "2024-01-01", "requested_end": "2024-01-02",
                }},
            }), encoding="utf-8")
            client = FakeDailyClient({"005930": AssertionError("must not fetch")})

            result = collect_daily_snapshot(
                client=client, access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"},
                start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=root, delay_seconds=0.1,
            )

            manifest = json.loads((root / "manifest.json").read_text())
            self.assertFalse(result.complete)
            self.assertEqual(client.calls, [])
            self.assertIn("immutable", result.errors["005930"])
            self.assertEqual(json.loads(symbol_file.read_text(encoding="utf-8")), payload)
            self.assertEqual(manifest["symbols"]["005930"]["status"], "error")

    def test_orphan_with_empty_bars_and_observed_bounds_fails_closed_without_network_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "schema_version": 1, "source": "KIS OpenAPI domestic daily price (adjusted)",
                "symbol": "005930", "asset_type": "STOCK", "requested_start": "2024-01-01",
                "requested_end": "2024-01-02", "observed_start": "2024-01-01",
                "observed_end": "2024-01-02", "bars": [],
            }
            symbol_file = root / "005930.json"
            symbol_file.write_text(json.dumps(payload), encoding="utf-8")
            client = FakeDailyClient({"005930": AssertionError("must not fetch")})

            result = collect_daily_snapshot(
                client=client, access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"},
                start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=root, delay_seconds=0.1,
            )

            manifest = json.loads((root / "manifest.json").read_text())
            self.assertFalse(result.complete)
            self.assertEqual(client.calls, [])
            self.assertIn("immutable", result.errors["005930"])
            self.assertEqual(json.loads(symbol_file.read_text(encoding="utf-8")), payload)
            self.assertEqual(manifest["symbols"]["005930"]["status"], "error")

    def test_invalid_orphan_file_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orphan = root / "005930.json"
            orphan.write_text('{"symbol": "WRONG"}', encoding="utf-8")
            client = FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),)})

            result = collect_daily_snapshot(
                client=client, access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"},
                start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=root, delay_seconds=0.1,
            )

            self.assertFalse(result.complete)
            self.assertIn("immutable", result.errors["005930"])
            self.assertEqual(orphan.read_text(encoding="utf-8"), '{"symbol": "WRONG"}')

    def test_rejects_unsafe_and_non_krx_symbols_before_filesystem_or_network_access(self) -> None:
        for symbol in ("../outside", "005930/child", "삼성전자", "005930 ", "00593"):
            with self.subTest(symbol=symbol), tempfile.TemporaryDirectory() as temporary:
                client = FakeDailyClient({symbol: ()})
                with self.assertRaisesRegex(ValueError, "KRX"):
                    collect_daily_snapshot(
                        client=client, access_token="token", symbols=(symbol,), asset_types={symbol: "STOCK"},
                        start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0.1,
                    )
                self.assertEqual(client.calls, [])
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_hash_mismatch_is_not_trusted_and_never_overwrites_immutable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),)})
            kwargs = dict(access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0.1)
            collect_daily_snapshot(client=client, **kwargs)
            (Path(temporary) / "005930.json").write_text("tampered", encoding="utf-8")
            result = collect_daily_snapshot(client=client, **kwargs)
            self.assertFalse(result.complete)
            self.assertIn("005930", result.errors)
            self.assertEqual(len(client.calls), 1)

    def test_error_and_malformed_source_leave_global_manifest_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),), "000660": (bar(date(2024, 1, 2), "WRONG"),)})
            result = collect_daily_snapshot(client=client, access_token="token", symbols=("005930", "000660"), asset_types={"005930": "STOCK", "000660": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0.1)
            manifest = json.loads((Path(temporary) / "manifest.json").read_text())
            self.assertFalse(result.complete)
            self.assertEqual(manifest["symbols"]["000660"]["status"], "error")
            self.assertFalse((Path(temporary) / "000660.json").exists())

    def test_enforces_nonzero_delay_and_request_cap_with_injected_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            slept: list[float] = []
            client = FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),), "000660": (bar(date(2024, 1, 2), "000660"),)})
            result = collect_daily_snapshot(client=client, access_token="token", symbols=("005930", "000660"), asset_types={"005930": "STOCK", "000660": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0.25, max_requests=1, sleep=slept.append)
            self.assertFalse(result.complete)
            self.assertEqual(slept, [])
            self.assertEqual(len(client.calls), 1)
            with self.assertRaisesRegex(ValueError, "nonzero"):
                collect_daily_snapshot(client=client, access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0)

    def test_waits_between_source_requests_with_injected_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            slept: list[float] = []
            client = FakeDailyClient({"005930": (bar(date(2024, 1, 2), "005930"),), "000660": (bar(date(2024, 1, 2), "000660"),)})
            result = collect_daily_snapshot(client=client, access_token="token", symbols=("005930", "000660"), asset_types={"005930": "STOCK", "000660": "STOCK"}, start=date(2024, 1, 1), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0.25, max_requests=2, sleep=slept.append)
            self.assertTrue(result.complete)
            self.assertEqual(slept, [0.25])

    def test_historical_bounds_are_enforced_before_network_access(self) -> None:
        client = FakeDailyClient({"005930": ()})
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(ValueError, "start"):
            collect_daily_snapshot(client=client, access_token="token", symbols=("005930",), asset_types={"005930": "STOCK"}, start=date(2024, 1, 3), end=date(2024, 1, 2), output_path=temporary, delay_seconds=0.1)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
