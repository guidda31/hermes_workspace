"""Fixture-only contracts for dated, provenance-backed KRX universe metadata."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from swing_v2.universe_metadata import (
    AssetType,
    ClassificationFlag,
    EtfExposure,
    MetadataProvenance,
    UniverseMetadataRecord,
    UniverseMetadataSnapshot,
    load_universe_metadata,
    select_eligible_universe,
)


PROVENANCE = MetadataProvenance(
    source="fixture.krx-classifications",
    version="2026.07.fixture",
    content_hash="sha256:" + "a" * 64,
    as_of=date(2024, 1, 1),
)


def record(
    symbol: str,
    asset_type: AssetType,
    *,
    effective_from: date = date(2024, 1, 1),
    effective_to: date | None = None,
    flags: frozenset[ClassificationFlag] = frozenset(),
    etf_exposure: EtfExposure | None = None,
) -> UniverseMetadataRecord:
    return UniverseMetadataRecord(
        symbol=symbol,
        asset_type=asset_type,
        effective_from=effective_from,
        effective_to=effective_to,
        flags=flags,
        etf_exposure=etf_exposure,
        provenance=PROVENANCE,
    )


class UniverseEligibilityTest(unittest.TestCase):
    def test_selects_only_records_effective_at_t_in_deterministic_symbol_order(self) -> None:
        snapshot = UniverseMetadataSnapshot(
            records=(
                record("OLD", AssetType.STOCK, effective_to=date(2024, 6, 30)),
                record("NEW", AssetType.STOCK, effective_from=date(2024, 7, 1)),
                record("ZETA", AssetType.STOCK),
                record("ALPHA", AssetType.ETF, etf_exposure=EtfExposure.DOMESTIC_INDEX_OR_SECTOR),
            )
        )

        june = select_eligible_universe(snapshot, date(2024, 6, 30))
        july = select_eligible_universe(snapshot, date(2024, 7, 1))

        self.assertEqual(june.symbols, ("ALPHA", "OLD", "ZETA"))
        self.assertEqual(july.symbols, ("ALPHA", "NEW", "ZETA"))
        self.assertEqual(july.exclusions, ())

    def test_conservatively_excludes_every_disallowed_or_unclassified_record_with_reason(self) -> None:
        snapshot = UniverseMetadataSnapshot(
            records=(
                record("PREF", AssetType.PREFERRED),
                record("SPAC", AssetType.SPAC),
                record("MGMT", AssetType.STOCK, flags=frozenset({ClassificationFlag.MANAGEMENT_ISSUE})),
                record("HALT", AssetType.STOCK, flags=frozenset({ClassificationFlag.TRADING_HALTED})),
                record("ETN", AssetType.ETN),
                record("UNKNOWN", AssetType.UNKNOWN),
                record("LEV", AssetType.ETF, flags=frozenset({ClassificationFlag.ETF_LEVERAGED}), etf_exposure=EtfExposure.DOMESTIC_INDEX_OR_SECTOR),
                record("INV", AssetType.ETF, flags=frozenset({ClassificationFlag.ETF_INVERSE}), etf_exposure=EtfExposure.DOMESTIC_INDEX_OR_SECTOR),
                record("FOREIGN", AssetType.ETF, flags=frozenset({ClassificationFlag.ETF_FOREIGN_INDEX}), etf_exposure=EtfExposure.DOMESTIC_INDEX_OR_SECTOR),
                record("UNCLASSIFIED_ETF", AssetType.ETF),
            )
        )

        selection = select_eligible_universe(snapshot, date(2024, 1, 1))

        self.assertEqual(selection.symbols, ())
        self.assertEqual(
            {(item.symbol, item.reason) for item in selection.exclusions},
            {
                ("PREF", "ASSET_TYPE_PREFERRED"),
                ("SPAC", "ASSET_TYPE_SPAC"),
                ("MGMT", "FLAG_MANAGEMENT_ISSUE"),
                ("HALT", "FLAG_TRADING_HALTED"),
                ("ETN", "ASSET_TYPE_ETN"),
                ("UNKNOWN", "ASSET_TYPE_UNKNOWN"),
                ("LEV", "FLAG_ETF_LEVERAGED"),
                ("INV", "FLAG_ETF_INVERSE"),
                ("FOREIGN", "FLAG_ETF_FOREIGN_INDEX"),
                ("UNCLASSIFIED_ETF", "ETF_EXPOSURE_UNKNOWN"),
            },
        )

    def test_rejects_overlaps_duplicates_invalid_dates_and_missing_provenance(self) -> None:
        with self.assertRaises(ValueError):
            UniverseMetadataSnapshot(records=(record("A", AssetType.STOCK), record("A", AssetType.STOCK)))
        with self.assertRaises(ValueError):
            UniverseMetadataSnapshot(records=(
                record("A", AssetType.STOCK, effective_to=date(2024, 12, 31)),
                record("A", AssetType.STOCK, effective_from=date(2024, 6, 1)),
            ))
        with self.assertRaises(ValueError):
            record("A", AssetType.STOCK, effective_from=date(2024, 2, 2), effective_to=date(2024, 2, 1))
        with self.assertRaises(ValueError):
            MetadataProvenance(source="", version="v1", content_hash="sha256:" + "a" * 64, as_of=date(2024, 1, 1))
        with self.assertRaises(ValueError):
            MetadataProvenance(source="fixture", version="v1", content_hash="not-a-hash", as_of=date(2024, 1, 1))
        with self.assertRaises(ValueError):
            UniverseMetadataRecord(
                symbol="A", asset_type=AssetType.STOCK, effective_from=date(2024, 1, 1),
                effective_to=None, flags=frozenset(), etf_exposure=None, provenance=None,  # type: ignore[arg-type]
            )

    def test_rejects_record_that_begins_before_its_provenance_snapshot(self) -> None:
        later_provenance = MetadataProvenance(
            "fixture", "v1", "sha256:" + "e" * 64, date(2026, 7, 17),
        )

        with self.assertRaisesRegex(ValueError, "provenance as_of"):
            UniverseMetadataRecord(
                "BACKFILLED", AssetType.STOCK, date(2020, 1, 1), None,
                frozenset(), None, later_provenance,
            )

    def test_record_becomes_selectable_on_its_provenance_snapshot_date(self) -> None:
        provenance = MetadataProvenance("fixture", "v1", "sha256:" + "f" * 64, date(2024, 7, 17))
        snapshot = UniverseMetadataSnapshot(records=(
            UniverseMetadataRecord("LATE", AssetType.STOCK, date(2024, 7, 17), None, frozenset(), None, provenance),
        ))

        before = select_eligible_universe(snapshot, date(2024, 7, 16), requested_symbols=("LATE",))
        on_or_after = select_eligible_universe(snapshot, date(2024, 7, 17), requested_symbols=("LATE",))

        self.assertEqual(before.symbols, ())
        self.assertEqual(
            tuple((item.symbol, item.reason) for item in before.exclusions),
            (("LATE", "METADATA_MISSING_OR_NOT_EFFECTIVE"),),
        )
        self.assertEqual(on_or_after.symbols, ("LATE",))

    def test_requested_symbols_without_active_metadata_are_auditably_denied(self) -> None:
        snapshot = UniverseMetadataSnapshot(records=(record("KNOWN", AssetType.STOCK),))

        selection = select_eligible_universe(snapshot, date(2024, 1, 1), requested_symbols=("MISSING", "KNOWN", "EXPIRED"))

        self.assertEqual(selection.symbols, ("KNOWN",))
        self.assertEqual(
            tuple((item.symbol, item.reason) for item in selection.exclusions),
            (("EXPIRED", "METADATA_MISSING_OR_NOT_EFFECTIVE"), ("MISSING", "METADATA_MISSING_OR_NOT_EFFECTIVE")),
        )

    def test_loads_normalized_json_and_csv_fixture_records_with_provenance(self) -> None:
        rows = [{
            "symbol": "ETF1", "asset_type": "ETF", "effective_from": "2024-01-01", "effective_to": "",
            "flags": "", "etf_exposure": "DOMESTIC_INDEX_OR_SECTOR", "source": "fixture.source",
            "version": "fixture-v1", "content_hash": "sha256:" + "b" * 64, "as_of": "2024-01-01",
        }]
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            json_path = directory / "metadata.json"
            json_path.write_text(json.dumps({"format_version": 1, "records": rows}), encoding="utf-8")
            csv_path = directory / "metadata.csv"
            csv_path.write_text(
                "symbol,asset_type,effective_from,effective_to,flags,etf_exposure,source,version,content_hash,as_of\n"
                + ",".join(rows[0][field] for field in ("symbol", "asset_type", "effective_from", "effective_to", "flags", "etf_exposure", "source", "version", "content_hash", "as_of")) + "\n",
                encoding="utf-8",
            )

            from_json = load_universe_metadata(json_path)
            from_csv = load_universe_metadata(csv_path)

        self.assertEqual(from_json, from_csv)
        self.assertEqual(select_eligible_universe(from_json, date(2024, 1, 1)).symbols, ("ETF1",))


if __name__ == "__main__":
    unittest.main()
