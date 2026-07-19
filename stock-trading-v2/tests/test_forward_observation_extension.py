"""TDD coverage for immutable one-session forward-observation extensions."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from swing_v2.forward_observation_dataset import load_forward_observation_dataset
from swing_v2.forward_observation_extension import assemble_one_session_extension


BASE_ARTIFACT = Path(__file__).resolve().parents[2] / "data" / "forward-observation-v3-2026-07-16.json"
NEW_DAY = date(2026, 7, 20)
SYMBOLS = ("005930", "000660", "035420")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _bar(symbol: str, asset_type: str, close: str, day: date = NEW_DAY) -> dict[str, object]:
    return {
        "trade_date": day.isoformat(), "symbol": symbol, "asset_type": asset_type,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 100, "trading_value": "1000", "is_tradable": True,
    }


def _extension_sources(root: Path, day: date = NEW_DAY) -> tuple[Path, Path]:
    stocks = root / "stocks"
    entries: dict[str, object] = {}
    for number, symbol in enumerate(SYMBOLS, start=1):
        payload = {
            "schema_version": 1, "source": "KIS OpenAPI domestic daily price (adjusted)",
            "symbol": symbol, "asset_type": "STOCK", "requested_start": day.isoformat(),
            "requested_end": day.isoformat(), "observed_start": day.isoformat(),
            "observed_end": day.isoformat(), "bars": [_bar(symbol, "STOCK", str(number))],
        }
        file = stocks / f"{symbol}.json"
        _write_json(file, payload)
        entries[symbol] = {
            "symbol": symbol, "asset_type": "STOCK", "file": file.name, "status": "complete",
            "requested_start": day.isoformat(), "requested_end": day.isoformat(),
            "observed_start": day.isoformat(), "observed_end": day.isoformat(),
            "sha256": sha256(file.read_bytes()).hexdigest(),
        }
    manifest = stocks / "manifest.json"
    _write_json(manifest, {
        "schema_version": 1, "source": "KIS OpenAPI domestic daily price (adjusted)", "complete": True,
        "requested_start": day.isoformat(), "requested_end": day.isoformat(), "symbols": entries,
    })
    kospi = root / "kospi" / "KOSPI.json"
    _write_json(kospi, {
        "schema_version": 1, "source": "KIS OpenAPI domestic daily index chart (KOSPI code 0001)",
        "market_symbol": "KOSPI", "index_code": "0001", "asset_type": "INDEX",
        "requested_start": day.isoformat(), "requested_end": day.isoformat(),
        "observed_start": day.isoformat(), "observed_end": day.isoformat(),
        "bars": [_bar("KOSPI", "INDEX", "3000", day)],
    })
    return manifest, kospi


class ForwardObservationExtensionTests(unittest.TestCase):
    def test_appends_one_validated_session_to_a_new_v3_dataset(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi = _extension_sources(root)
            output = root / f"forward-observation-v3-{NEW_DAY.isoformat()}.json"

            snapshot = assemble_one_session_extension(
                base_dataset_path=BASE_ARTIFACT,
                stock_manifest_path=manifest,
                kospi_artifact_path=kospi,
                data_as_of=NEW_DAY,
                output_path=output,
            )
            loaded = load_forward_observation_dataset(output)

            self.assertEqual(snapshot, loaded)
            self.assertEqual(loaded.metadata.data_as_of, NEW_DAY.isoformat())
            self.assertEqual(loaded.trade_calendar[-1], NEW_DAY)
            self.assertEqual(loaded.market_history[-1].trade_date, NEW_DAY)
            self.assertEqual({bars[-1].trade_date for bars in loaded.histories.values()}, {NEW_DAY})
            self.assertEqual(set(loaded.histories), set(SYMBOLS))

    def test_rejects_valid_v2_named_base_before_creating_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "forward-observation-v2-2026-07-16.json"
            base.write_bytes(BASE_ARTIFACT.read_bytes())
            manifest, kospi = _extension_sources(root)
            output = root / f"forward-observation-v3-{NEW_DAY.isoformat()}.json"

            with self.assertRaisesRegex(ValueError, "approved v3 base"):
                assemble_one_session_extension(
                    base_dataset_path=base, stock_manifest_path=manifest,
                    kospi_artifact_path=kospi, data_as_of=NEW_DAY, output_path=output,
                )

            self.assertFalse(output.exists())

    def test_rejects_v3_named_symlink_to_valid_v2_base_before_reading_sources(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            v2_base = root / "forward-observation-v2-2026-07-16.json"
            v2_base.write_bytes(BASE_ARTIFACT.read_bytes())
            base = root / "forward-observation-v3-2026-07-16.json"
            base.symlink_to(v2_base)
            output = root / f"forward-observation-v3-{NEW_DAY.isoformat()}.json"

            with self.assertRaisesRegex(ValueError, "symlink"):
                assemble_one_session_extension(
                    base_dataset_path=base,
                    stock_manifest_path=root / "missing-manifest.json",
                    kospi_artifact_path=root / "missing-kospi.json",
                    data_as_of=NEW_DAY,
                    output_path=output,
                )

            self.assertFalse(output.exists())

    def test_rejects_duplicate_or_non_incrementing_extension_date_without_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi = _extension_sources(root, date(2026, 7, 16))
            output = root / "forward-observation-v3-2026-07-16.json"

            with self.assertRaisesRegex(ValueError, "strictly later"):
                assemble_one_session_extension(
                    base_dataset_path=BASE_ARTIFACT, stock_manifest_path=manifest,
                    kospi_artifact_path=kospi, data_as_of=date(2026, 7, 16), output_path=output,
                )

            self.assertFalse(output.exists())

    def test_rejects_mismatched_market_date_without_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi = _extension_sources(root)
            payload = json.loads(kospi.read_text(encoding="utf-8"))
            wrong_day = date(2026, 7, 21).isoformat()
            for field in ("requested_start", "requested_end", "observed_start", "observed_end"):
                payload[field] = wrong_day
            payload["bars"][0]["trade_date"] = wrong_day
            _write_json(kospi, payload)
            output = root / f"forward-observation-v3-{NEW_DAY.isoformat()}.json"

            with self.assertRaisesRegex(ValueError, "extension date"):
                assemble_one_session_extension(
                    base_dataset_path=BASE_ARTIFACT, stock_manifest_path=manifest,
                    kospi_artifact_path=kospi, data_as_of=NEW_DAY, output_path=output,
                )

            self.assertFalse(output.exists())

    def test_rejects_tampered_base_integrity_without_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "forward-observation-v3-2026-07-16.json"
            payload = json.loads(BASE_ARTIFACT.read_text(encoding="utf-8"))
            payload["snapshot"]["market_history"][0]["close"] = "1"
            _write_json(base, payload)
            manifest, kospi = _extension_sources(root)
            output = root / f"forward-observation-v3-{NEW_DAY.isoformat()}.json"

            with self.assertRaisesRegex(ValueError, "integrity"):
                assemble_one_session_extension(
                    base_dataset_path=base, stock_manifest_path=manifest,
                    kospi_artifact_path=kospi, data_as_of=NEW_DAY, output_path=output,
                )

            self.assertFalse(output.exists())

    def test_rejects_tampered_stock_source_without_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi = _extension_sources(root)
            stock = manifest.parent / "005930.json"
            payload = json.loads(stock.read_text(encoding="utf-8"))
            payload["bars"][0]["close"] = "999"
            _write_json(stock, payload)
            output = root / f"forward-observation-v3-{NEW_DAY.isoformat()}.json"

            with self.assertRaisesRegex(ValueError, "sha256"):
                assemble_one_session_extension(
                    base_dataset_path=BASE_ARTIFACT, stock_manifest_path=manifest,
                    kospi_artifact_path=kospi, data_as_of=NEW_DAY, output_path=output,
                )

            self.assertFalse(output.exists())

    def test_refuses_to_overwrite_an_existing_extension_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi = _extension_sources(root)
            output = root / f"forward-observation-v3-{NEW_DAY.isoformat()}.json"
            original = b"immutable-existing-output"
            output.write_bytes(original)

            with self.assertRaisesRegex(ValueError, "already exists"):
                assemble_one_session_extension(
                    base_dataset_path=BASE_ARTIFACT, stock_manifest_path=manifest,
                    kospi_artifact_path=kospi, data_as_of=NEW_DAY, output_path=output,
                )

            self.assertEqual(output.read_bytes(), original)

    def test_output_has_no_history_after_its_explicit_data_as_of(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, kospi = _extension_sources(root)
            output = root / f"forward-observation-v3-{NEW_DAY.isoformat()}.json"
            loaded = assemble_one_session_extension(
                base_dataset_path=BASE_ARTIFACT, stock_manifest_path=manifest,
                kospi_artifact_path=kospi, data_as_of=NEW_DAY, output_path=output,
            )

            self.assertTrue(all(bar.trade_date <= NEW_DAY for bar in loaded.market_history))
            self.assertTrue(all(bar.trade_date <= NEW_DAY for bars in loaded.histories.values() for bar in bars))
            self.assertEqual(load_forward_observation_dataset(output).metadata.data_as_of, NEW_DAY.isoformat())


if __name__ == "__main__":
    unittest.main()
