"""TDD coverage for local-only KIS forward-observation dataset assembly."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from swing_v2.backtest_data import SnapshotBacktestData
from swing_v2.forward_observation_dataset import (
    assemble_forward_observation_dataset,
    load_forward_observation_dataset,
)


REQUESTED_SYMBOLS = ("005930", "000660")
START = date(2025, 9, 1)
AS_OF = date(2026, 7, 16)


def _bar(day: str, symbol: str, asset_type: str, close: str) -> dict[str, object]:
    return {
        "trade_date": day, "symbol": symbol, "asset_type": asset_type,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 100, "trading_value": "1000", "is_tradable": True,
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resign_integrity(payload: dict[str, object]) -> None:
    unsigned = {key: value for key, value in payload.items() if key != "integrity"}
    canonical = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload["integrity"] = {"algorithm": "sha256", "digest": sha256(canonical).hexdigest()}


def _fixture_inputs(root: Path) -> tuple[Path, Path, Path]:
    stocks = root / "stocks"
    first, last = "2025-09-01", "2026-07-16"
    manifest_symbols: dict[str, object] = {}
    for symbol, close in (("005930", "70000"), ("000660", "250000")):
        payload = {
            "schema_version": 1, "source": "KIS OpenAPI domestic daily price (adjusted)",
            "symbol": symbol, "asset_type": "STOCK", "requested_start": first,
            "requested_end": "2026-07-17", "observed_start": first, "observed_end": last,
            "bars": [_bar(first, symbol, "STOCK", close), _bar(last, symbol, "STOCK", close)],
        }
        path = stocks / f"{symbol}.json"
        _write_json(path, payload)
        manifest_symbols[symbol] = {
            "symbol": symbol, "asset_type": "STOCK", "file": path.name, "status": "complete",
            "requested_start": first, "requested_end": "2026-07-17", "observed_start": first,
            "observed_end": last, "sha256": sha256(path.read_bytes()).hexdigest(),
        }
    manifest = stocks / "manifest.json"
    _write_json(manifest, {
        "schema_version": 1, "source": "KIS OpenAPI domestic daily price (adjusted)", "complete": True,
        "requested_start": first, "requested_end": "2026-07-17", "symbols": manifest_symbols,
    })
    kospi = root / "kospi" / "KOSPI.json"
    _write_json(kospi, {
        "schema_version": 1, "source": "KIS OpenAPI domestic daily index chart (KOSPI code 0001)",
        "market_symbol": "KOSPI", "index_code": "0001", "asset_type": "INDEX",
        "requested_start": first, "requested_end": "2026-07-17", "observed_start": first,
        "observed_end": last, "bars": [_bar(first, "KOSPI", "INDEX", "3000"), _bar(last, "KOSPI", "INDEX", "3100")],
    })
    universe = root / "krx.json"
    _write_json(universe, {"format_version": 1, "records": [{
        "symbol": "005930", "asset_type": "STOCK", "effective_from": "2026-07-18", "effective_to": None,
        "flags": [], "etf_exposure": None, "source": "fixture KRX", "version": "v1",
        "content_hash": "sha256:" + "0" * 64, "as_of": "2026-07-18",
    }]})
    return manifest, kospi, universe


class ForwardObservationDatasetTests(unittest.TestCase):
    def test_actual_v2_and_v3_artifacts_load_with_canonical_provenance_paths(self) -> None:
        data_dir = Path(__file__).resolve().parents[2] / "data"
        for name in (
            "forward-observation-v2-2026-07-16.json",
            "forward-observation-v3-2026-07-16.json",
        ):
            with self.subTest(name=name):
                snapshot = load_forward_observation_dataset(data_dir / name)
                self.assertEqual(snapshot.metadata.data_as_of, "2026-07-16")

    def test_assembles_strict_local_snapshot_with_kospi_index_and_no_future_leakage(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v3-2026-07-16.json"

            snapshot = assemble_forward_observation_dataset(
                stock_manifest_path=manifest, kospi_artifact_path=kospi,
                krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
            )
            loaded = load_forward_observation_dataset(output)
            data = SnapshotBacktestData(loaded)
            payload = json.loads(output.read_text(encoding="utf-8"))
            manifest_hash = sha256(manifest.read_bytes()).hexdigest()

        self.assertEqual(snapshot, loaded)
        self.assertEqual(data.get_asset_type("KOSPI"), "INDEX")
        self.assertEqual(data.get_historical_bars("005930", date(2026, 7, 15), 10)[-1].trade_date, date(2025, 9, 1))
        self.assertEqual(data.get_historical_bars("005930", AS_OF, 10)[-1].trade_date, AS_OF)
        self.assertEqual(payload["snapshot"]["metadata"]["data_as_of"], "2026-07-16")
        self.assertEqual(payload["provenance"]["stock_manifest"]["sha256"], manifest_hash)
        self.assertEqual(set(payload["provenance"]["stock_payloads"]), set(REQUESTED_SYMBOLS))
        self.assertEqual(payload["krx_universe_metadata"]["lifecycle"], "forward_observation_only")
        self.assertIn("must not be used", payload["krx_universe_metadata"]["historical_backtest_prohibition"])

    def test_loader_rejects_stale_data_as_of_after_final_market_and_stock_bars_are_removed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-16.json"
            assemble_forward_observation_dataset(
                stock_manifest_path=manifest, kospi_artifact_path=kospi,
                krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            snapshot = payload["snapshot"]
            snapshot["trade_calendar"].pop()
            snapshot["market_history"].pop()
            for bars in snapshot["histories"].values():
                bars.pop()
            _resign_integrity(payload)
            _write_json(output, payload)

            with self.assertRaisesRegex(ValueError, "actual final"):
                load_forward_observation_dataset(output)

    def test_loader_rejects_provenance_mutation_by_detached_integrity_digest(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-16.json"
            assemble_forward_observation_dataset(
                stock_manifest_path=manifest, kospi_artifact_path=kospi,
                krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["provenance"]["stock_manifest"]["sha256"] = "f" * 64
            _write_json(output, payload)

            with self.assertRaisesRegex(ValueError, "integrity"):
                load_forward_observation_dataset(output)

    def test_loader_rejects_re_signed_malformed_provenance_hash_and_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-16.json"
            assemble_forward_observation_dataset(
                stock_manifest_path=manifest, kospi_artifact_path=kospi,
                krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["provenance"]["stock_manifest"] = {"path": "", "sha256": "A" * 64}
            _resign_integrity(payload)
            _write_json(output, payload)

            with self.assertRaisesRegex(ValueError, "provenance path or sha256"):
                load_forward_observation_dataset(output)

    def test_loader_rejects_re_signed_noncanonical_provenance_paths(self) -> None:
        cases = (
            ("provenance.stock_manifest", "relative/input.json"),
            ("provenance.kospi_artifact", "/tmp/../unsafe.json"),
            ("provenance.stock_payloads.005930", "/tmp//unsafe.json"),
            ("krx_universe_metadata", "/tmp/unsafe\x00.json"),
            ("provenance.stock_manifest", "C:\\unsafe.json"),
            ("provenance.kospi_artifact", "\\\\server\\share\\unsafe.json"),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-16.json"
            assemble_forward_observation_dataset(
                stock_manifest_path=manifest, kospi_artifact_path=kospi,
                krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
            )
            original = json.loads(output.read_text(encoding="utf-8"))
            for field, unsafe_path in cases:
                payload = json.loads(json.dumps(original))
                target: dict[str, object] = payload
                for key in field.split("."):
                    target = target[key]  # type: ignore[assignment,index]
                target["path"] = unsafe_path
                _resign_integrity(payload)
                _write_json(output, payload)

                with self.subTest(field=field, unsafe_path=unsafe_path):
                    with self.assertRaisesRegex(ValueError, "provenance path"):
                        load_forward_observation_dataset(output)

    def test_loader_rejects_snapshot_mutation_by_detached_integrity_digest(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-16.json"
            assemble_forward_observation_dataset(
                stock_manifest_path=manifest, kospi_artifact_path=kospi,
                krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["snapshot"]["histories"]["005930"][0]["close"] = "1"
            _write_json(output, payload)

            with self.assertRaisesRegex(ValueError, "integrity"):
                load_forward_observation_dataset(output)

    def test_loader_rejects_invalid_integrity_digest_format(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-16.json"
            assemble_forward_observation_dataset(
                stock_manifest_path=manifest, kospi_artifact_path=kospi,
                krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["integrity"]["digest"] = "UPPERCASE"
            _write_json(output, payload)

            with self.assertRaisesRegex(ValueError, "integrity digest"):
                load_forward_observation_dataset(output)

    def test_loader_rejects_legacy_v1_envelope(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-16.json"
            assemble_forward_observation_dataset(
                stock_manifest_path=manifest, kospi_artifact_path=kospi,
                krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["format_version"] = 1
            payload.pop("integrity")
            _write_json(output, payload)

            with self.assertRaisesRegex(ValueError, "legacy formats are not approved"):
                load_forward_observation_dataset(output)

    def test_tampered_stock_hash_fails_closed_without_overwriting_existing_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-16.json"
            original = b"do-not-overwrite"
            output.write_bytes(original)
            stock = manifest.parent / "005930.json"
            payload = json.loads(stock.read_text(encoding="utf-8"))
            payload["bars"][0]["close"] = "1"
            _write_json(stock, payload)

            with self.assertRaisesRegex(ValueError, "sha256"):
                assemble_forward_observation_dataset(
                    stock_manifest_path=manifest, kospi_artifact_path=kospi,
                    krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                    requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                    requested_start=START, requested_end=AS_OF, data_as_of=AS_OF, output_path=output,
                )

            self.assertEqual(output.read_bytes(), original)

    def test_rejects_data_as_of_that_is_not_actual_latest_observation_without_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi, universe = _fixture_inputs(root)
            output = root / "forward-observation-v2-2026-07-17.json"

            with self.assertRaisesRegex(ValueError, "latest observed"):
                assemble_forward_observation_dataset(
                    stock_manifest_path=manifest, kospi_artifact_path=kospi,
                    krx_universe_metadata_path=universe, requested_symbols=REQUESTED_SYMBOLS,
                    requested_asset_types={"005930": "STOCK", "000660": "STOCK"},
                    requested_start=START, requested_end=date(2026, 7, 17), data_as_of=date(2026, 7, 17), output_path=output,
                )

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
