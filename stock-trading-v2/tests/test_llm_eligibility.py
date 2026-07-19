"""PIT KRX universe metadata -> guardrail eligible_symbols adapter."""

from __future__ import annotations

from datetime import date
import unittest

from swing_v2.llm.eligibility import eligible_symbols_as_of
from swing_v2.universe_metadata import (
    AssetType,
    ClassificationFlag,
    EtfExposure,
    MetadataProvenance,
    UniverseMetadataRecord,
    UniverseMetadataSnapshot,
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
    provenance: MetadataProvenance = PROVENANCE,
) -> UniverseMetadataRecord:
    return UniverseMetadataRecord(
        symbol=symbol,
        asset_type=asset_type,
        effective_from=effective_from,
        effective_to=effective_to,
        flags=flags,
        etf_exposure=etf_exposure,
        provenance=provenance,
    )


class EligibleSymbolsAsOfTest(unittest.TestCase):
    def test_eligible_stock_and_domestic_etf_are_included(self) -> None:
        snapshot = UniverseMetadataSnapshot(records=(
            record("GOODSTOCK", AssetType.STOCK),
            record("GOODETF", AssetType.ETF, etf_exposure=EtfExposure.DOMESTIC_INDEX_OR_SECTOR),
        ))

        eligible = eligible_symbols_as_of(
            snapshot, date(2024, 6, 1), frozenset({"GOODSTOCK", "GOODETF"})
        )

        self.assertEqual(eligible, frozenset({"GOODSTOCK", "GOODETF"}))

    def test_return_type_is_frozenset(self) -> None:
        snapshot = UniverseMetadataSnapshot(records=(record("GOODSTOCK", AssetType.STOCK),))

        eligible = eligible_symbols_as_of(snapshot, date(2024, 6, 1), frozenset({"GOODSTOCK"}))

        self.assertIs(type(eligible), frozenset)

    def test_ineligible_classifications_are_excluded(self) -> None:
        snapshot = UniverseMetadataSnapshot(records=(
            record("GOODSTOCK", AssetType.STOCK),
            record("ETN1", AssetType.ETN),
            record("PREF1", AssetType.PREFERRED),
            record("LEV1", AssetType.ETF, flags=frozenset({ClassificationFlag.ETF_LEVERAGED}), etf_exposure=EtfExposure.DOMESTIC_INDEX_OR_SECTOR),
            record("INV1", AssetType.ETF, flags=frozenset({ClassificationFlag.ETF_INVERSE}), etf_exposure=EtfExposure.DOMESTIC_INDEX_OR_SECTOR),
            record("MGMT1", AssetType.STOCK, flags=frozenset({ClassificationFlag.MANAGEMENT_ISSUE})),
            record("HALT1", AssetType.STOCK, flags=frozenset({ClassificationFlag.TRADING_HALTED})),
        ))

        candidates = frozenset({"GOODSTOCK", "ETN1", "PREF1", "LEV1", "INV1", "MGMT1", "HALT1"})
        eligible = eligible_symbols_as_of(snapshot, date(2024, 6, 1), candidates)

        self.assertEqual(eligible, frozenset({"GOODSTOCK"}))

    def test_future_dated_provenance_is_excluded(self) -> None:
        future_provenance = MetadataProvenance(
            source="fixture", version="v1", content_hash="sha256:" + "c" * 64, as_of=date(2024, 7, 1),
        )
        snapshot = UniverseMetadataSnapshot(records=(
            record("READY", AssetType.STOCK),
            record("NOTYET", AssetType.STOCK, effective_from=date(2024, 7, 1), provenance=future_provenance),
        ))

        # signal_date precedes NOTYET's provenance as_of -> not yet available (PIT).
        eligible = eligible_symbols_as_of(
            snapshot, date(2024, 6, 30), frozenset({"READY", "NOTYET"})
        )

        self.assertEqual(eligible, frozenset({"READY"}))

    def test_symbol_absent_from_metadata_is_excluded(self) -> None:
        snapshot = UniverseMetadataSnapshot(records=(record("KNOWN", AssetType.STOCK),))

        eligible = eligible_symbols_as_of(
            snapshot, date(2024, 6, 1), frozenset({"KNOWN", "UNKNOWNSYM"})
        )

        self.assertEqual(eligible, frozenset({"KNOWN"}))

    def test_empty_candidate_set_is_empty(self) -> None:
        snapshot = UniverseMetadataSnapshot(records=(record("KNOWN", AssetType.STOCK),))

        self.assertEqual(
            eligible_symbols_as_of(snapshot, date(2024, 6, 1), frozenset()), frozenset()
        )

    def test_malformed_inputs_fail_closed_with_valueerror(self) -> None:
        snapshot = UniverseMetadataSnapshot(records=(record("KNOWN", AssetType.STOCK),))
        with self.assertRaises(ValueError):
            eligible_symbols_as_of(object(), date(2024, 6, 1), frozenset({"KNOWN"}))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            eligible_symbols_as_of(snapshot, "2024-06-01", frozenset({"KNOWN"}))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            eligible_symbols_as_of(snapshot, date(2024, 6, 1), {"KNOWN"})  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            eligible_symbols_as_of(snapshot, date(2024, 6, 1), frozenset({""}))


if __name__ == "__main__":
    unittest.main()
