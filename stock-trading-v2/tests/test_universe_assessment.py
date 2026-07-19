"""Integration: assessment is fed only the point-in-time selected universe."""

from datetime import date, timedelta
from decimal import Decimal
import unittest

from swing_v2.backtest import assess_eligible_close_time_candidates
from swing_v2.contracts import DailyBar
from swing_v2.universe_metadata import (
    AssetType,
    ClassificationFlag,
    MetadataProvenance,
    UniverseMetadataRecord,
    UniverseMetadataSnapshot,
)


D = Decimal
SIGNAL_DATE = date(2026, 7, 17)
PROVENANCE = MetadataProvenance("fixture.source", "v1", "sha256:" + "c" * 64, date(2026, 7, 17))


def bars(symbol: str) -> tuple[DailyBar, ...]:
    closes = [D("1000")] * 40 + [D("1100")] * 20 + [D("1200")]
    start = SIGNAL_DATE - timedelta(days=60)
    return tuple(
        DailyBar(start + timedelta(days=index), symbol, "STOCK", close, close, close, close, 1, D("1000000000"), True)
        for index, close in enumerate(closes)
    )


class UniverseAssessmentIntegrationTest(unittest.TestCase):
    def test_candidate_assessment_receives_only_metadata_selected_symbols(self) -> None:
        metadata = UniverseMetadataSnapshot(records=(
            UniverseMetadataRecord("ALLOW", AssetType.STOCK, SIGNAL_DATE, None, frozenset(), None, PROVENANCE),
            UniverseMetadataRecord("HALTED", AssetType.STOCK, SIGNAL_DATE, None, frozenset({ClassificationFlag.TRADING_HALTED}), None, PROVENANCE),
        ))

        result = assess_eligible_close_time_candidates(
            signal_date=SIGNAL_DATE,
            market_closes=[D("100")] * 199 + [D("101")],
            asset_types={"ALLOW": "STOCK", "HALTED": "STOCK", "MISSING": "STOCK"},
            asset_histories={"ALLOW": bars("ALLOW"), "HALTED": bars("HALTED"), "MISSING": bars("MISSING")},
            candidate_symbols=("MISSING", "HALTED", "ALLOW"),
            universe_metadata=metadata,
        )

        self.assertEqual(result.selection.symbols, ("ALLOW",))
        self.assertEqual(tuple(item.symbol for item in result.assessments), ("ALLOW",))
        self.assertEqual(
            tuple((item.symbol, item.reason) for item in result.selection.exclusions),
            (("HALTED", "FLAG_TRADING_HALTED"), ("MISSING", "METADATA_MISSING_OR_NOT_EFFECTIVE")),
        )


if __name__ == "__main__":
    unittest.main()
