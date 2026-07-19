"""Fixture-only coverage for the reproducible local daily-bar snapshot adapter."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from swing_v2.backtest.backtest_engine import BacktestConfig, BacktestRiskConfig, BacktestRunner
from swing_v2.backtest.engine import ExecutionCostConfig
from swing_v2.backtest_data import (
    DailyBarSnapshot,
    SnapshotBacktestData,
    SnapshotMetadata,
    build_snapshot_from_fdr,
    load_snapshot,
    save_snapshot,
)
from swing_v2.contracts import DailyBar
from swing_v2.public_data import FinanceDataReaderAdapter
from swing_v2.universe_metadata import AssetType, MetadataProvenance, UniverseMetadataRecord, UniverseMetadataSnapshot


D = Decimal


def _universe_metadata(as_of: date, symbol: str) -> UniverseMetadataSnapshot:
    provenance = MetadataProvenance("fixture.universe", "v1", "sha256:" + "1" * 64, as_of)
    return UniverseMetadataSnapshot((
        UniverseMetadataRecord(symbol, AssetType.STOCK, as_of, None, frozenset(), None, provenance),
    ))


def _bar(day: date, symbol: str, asset_type: str, close: str) -> DailyBar:
    price = D(close)
    return DailyBar(day, symbol, asset_type, price, price, price, price, 100, price * 100, True)


class SnapshotSerializationTests(unittest.TestCase):
    def test_roundtrip_preserves_decimal_bars_and_metadata(self) -> None:
        d1 = date(2024, 1, 2)
        snapshot = DailyBarSnapshot(
            metadata=SnapshotMetadata(
                source="FinanceDataReader",
                retrieved_at="2024-02-01T00:00:00+00:00",
                data_as_of="2024-01-03",
                trading_value_is_close_times_volume_proxy=True,
            ),
            market_symbol="KOSPI",
            asset_types={"AAA": "STOCK"},
            trade_calendar=(d1, date(2024, 1, 3)),
            histories={"AAA": (_bar(d1, "AAA", "STOCK", "10.25"), _bar(date(2024, 1, 3), "AAA", "STOCK", "11.50"))},
            market_history=(_bar(d1, "KOSPI", "INDEX", "2500.25"), _bar(date(2024, 1, 3), "KOSPI", "INDEX", "2510.50")),
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            save_snapshot(snapshot, path)
            loaded = load_snapshot(path)

        self.assertEqual(loaded, snapshot)
        self.assertEqual(loaded.histories["AAA"][0].close, D("10.25"))

    def test_load_rejects_malformed_normalized_json(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.json"
            path.write_text('{"format_version": 1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot JSON fields"):
                load_snapshot(path)


class SnapshotValidationTests(unittest.TestCase):
    def _metadata(self, data_as_of: str = "2024-01-03") -> SnapshotMetadata:
        return SnapshotMetadata("fixture", "2024-02-01T00:00:00+00:00", data_as_of, True)

    def test_rejects_duplicate_calendar_and_nonmonotonic_history(self) -> None:
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        common = dict(metadata=self._metadata(), market_symbol="KOSPI", asset_types={"AAA": "STOCK"}, market_history=(_bar(d1, "KOSPI", "INDEX", "2500"),))
        with self.assertRaisesRegex(ValueError, "strictly ascending"):
            DailyBarSnapshot(trade_calendar=(d1, d1), histories={"AAA": (_bar(d1, "AAA", "STOCK", "10"),)}, **common)
        with self.assertRaisesRegex(ValueError, "strictly ascending"):
            DailyBarSnapshot(trade_calendar=(d1, d2), histories={"AAA": (_bar(d2, "AAA", "STOCK", "11"), _bar(d1, "AAA", "STOCK", "10"))}, **common)

    def test_rejects_bar_later_than_declared_data_as_of(self) -> None:
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        with self.assertRaisesRegex(ValueError, "data_as_of"):
            DailyBarSnapshot(
                metadata=self._metadata("2024-01-02"), market_symbol="KOSPI", asset_types={"AAA": "STOCK"},
                trade_calendar=(d1, d2), histories={"AAA": (_bar(d1, "AAA", "STOCK", "10"), _bar(d2, "AAA", "STOCK", "11"))},
                market_history=(_bar(d1, "KOSPI", "INDEX", "2500"), _bar(d2, "KOSPI", "INDEX", "2510")),
            )


class SnapshotBacktestDataTests(unittest.TestCase):
    def _snapshot(self) -> DailyBarSnapshot:
        d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)
        return DailyBarSnapshot(
            metadata=SnapshotMetadata("fixture", "2024-02-01T00:00:00+00:00", "2024-01-05", True),
            market_symbol="KOSPI", asset_types={"AAA": "STOCK"}, trade_calendar=(d1, d2, d3),
            histories={"AAA": (_bar(d1, "AAA", "STOCK", "10"), _bar(d3, "AAA", "STOCK", "12"))},
            market_history=(_bar(d1, "KOSPI", "INDEX", "2500"), _bar(d2, "KOSPI", "INDEX", "2510"), _bar(d3, "KOSPI", "INDEX", "2520")),
        )

    def test_exact_session_maps_calendar_gaps_and_never_leaks_future_history(self) -> None:
        d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)
        data = SnapshotBacktestData(self._snapshot())
        self.assertEqual(data.get_trade_calendar(d1, d3), (d1, d2, d3))
        self.assertEqual(data.get_bars(d1)["AAA"].close, D("10"))
        self.assertIsNone(data.get_bars(d2)["AAA"])
        self.assertEqual(data.get_market_index_bar(d2).close, D("2510"))
        self.assertEqual(data.get_asset_type("AAA"), "STOCK")
        self.assertEqual([bar.trade_date for bar in data.get_historical_bars("AAA", d2, 10)], [d1])
        self.assertEqual(data.get_historical_closes("AAA", d3, 1), (D("12"),))
        self.assertEqual(data.get_historical_closes("AAA", d2, 10), (D("10"),))

    def test_unknown_symbols_and_invalid_windows_are_rejected(self) -> None:
        data = SnapshotBacktestData(self._snapshot())
        with self.assertRaisesRegex(ValueError, "unknown snapshot symbol"):
            data.get_asset_type("NOPE")
        with self.assertRaisesRegex(ValueError, "positive int"):
            data.get_historical_bars("AAA", date(2024, 1, 2), 0)

    def test_backtest_runner_accepts_a_local_fixture_snapshot(self) -> None:
        start = date(2024, 1, 1)
        calendar = tuple(start + timedelta(days=index) for index in range(205))
        snapshot = DailyBarSnapshot(
            metadata=SnapshotMetadata("fixture", "2024-08-01T00:00:00+00:00", calendar[-1].isoformat(), True),
            market_symbol="KOSPI", asset_types={"AAA": "STOCK"}, trade_calendar=calendar,
            histories={"AAA": tuple(_bar(day, "AAA", "STOCK", str(100 + index)) for index, day in enumerate(calendar))},
            market_history=tuple(_bar(day, "KOSPI", "INDEX", str(2500 + index)) for index, day in enumerate(calendar)),
        )
        costs = ExecutionCostConfig(D("0"), D("0"), D("0"), D("0"), {"STOCK": D("0")}, D("0"), lambda price, _side: price)
        result = BacktestRunner().run(
            BacktestConfig(calendar[0], calendar[-1], ("AAA",), "KOSPI", D("100000"), costs, BacktestRiskConfig(D("0.01"), 1, D("0.2"), D("0.05"), D("0.5")), _universe_metadata(calendar[0], "AAA")),
            SnapshotBacktestData(snapshot),
        )
        self.assertEqual(len(result.all_day_results), len(calendar))


class SnapshotFdrBuilderTests(unittest.TestCase):
    def test_builder_uses_only_explicit_symbols_asset_types_and_bounded_dates(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def fake_reader(symbol: str, start: str, end: str):
            calls.append((symbol, start, end))
            import pandas as pd
            return pd.DataFrame({"Open": [10], "High": [10], "Low": [10], "Close": [10], "Volume": [100]}, index=pd.to_datetime(["2024-01-02"]))

        snapshot = build_snapshot_from_fdr(
            adapter=FinanceDataReaderAdapter(fake_reader), symbols=("AAA",), asset_types={"AAA": "STOCK"}, market_symbol="KOSPI",
            start=date(2024, 1, 2), end=date(2024, 1, 3), metadata=SnapshotMetadata("FinanceDataReader", "2024-02-01T00:00:00+00:00", "2024-01-03", True),
        )
        self.assertEqual(calls, [("AAA", "2024-01-02", "2024-01-03"), ("KOSPI", "2024-01-02", "2024-01-03")])
        self.assertEqual(snapshot.asset_types, {"AAA": "STOCK"})
        self.assertEqual(snapshot.trade_calendar, (date(2024, 1, 2),))


if __name__ == "__main__":
    unittest.main()
