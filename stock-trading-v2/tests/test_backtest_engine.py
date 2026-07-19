"""Canonical end-to-end regression coverage for BacktestRunner."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping, Sequence
import unittest

from swing_v2.backtest.backtest_engine import BacktestConfig, BacktestRiskConfig, BacktestRunner
from swing_v2.backtest.engine import ExecutionCostConfig, Side
from swing_v2.contracts import DailyBar
from swing_v2.universe_metadata import (
    AssetType,
    ClassificationFlag,
    MetadataProvenance,
    UniverseMetadataRecord,
    UniverseMetadataSnapshot,
)


D = Decimal


def _metadata(as_of: date, *records: tuple[str, AssetType, frozenset[ClassificationFlag]]) -> UniverseMetadataSnapshot:
    provenance = MetadataProvenance("fixture.universe", "v1", "sha256:" + "d" * 64, as_of)
    return UniverseMetadataSnapshot(tuple(
        UniverseMetadataRecord(symbol, asset_type, as_of, None, flags, None, provenance)
        for symbol, asset_type, flags in records
    ))


def _bar(day: date, symbol: str, open_price: str, close: str) -> DailyBar:
    price = D(close)
    return DailyBar(
        trade_date=day,
        symbol=symbol,
        asset_type="STOCK",
        open=D(open_price),
        high=max(D(open_price), price),
        low=min(D(open_price), price),
        close=price,
        volume=10_000_000,
        trading_value=D("1000000000"),
        is_tradable=True,
    )


class _NoLookaheadData:
    """In-memory source that records every historical observation request."""

    def __init__(self, d1: date, d2: date, symbol: str) -> None:
        self.d1, self.d2, self.symbol = d1, d2, symbol
        self.historical_requests: list[tuple[str, date, int]] = []
        self._bars = {
            d1: {symbol: _bar(d1, symbol, "145", "150")},
            # This gap must not be known while the d1 plan is constructed.
            d2: {symbol: _bar(d2, symbol, "200", "205")},
        }

    def get_trade_calendar(self, start_date: date, end_date: date) -> Sequence[date]:
        return (self.d1, self.d2)

    def get_bars(self, trade_date: date) -> Mapping[str, DailyBar | None]:
        return self._bars[trade_date]

    def get_asset_type(self, symbol: str) -> str:
        assert symbol == self.symbol
        return "STOCK"

    def get_historical_closes(self, symbol: str, end_date: date, window: int) -> Sequence[Decimal]:
        self.historical_requests.append((symbol, end_date, window))
        # Monotone closes make the configured market risk-on at either close.
        return tuple(D("100") + D(index) for index in range(window))

    def get_historical_bars(self, symbol: str, end_date: date, window: int) -> Sequence[DailyBar]:
        self.historical_requests.append((symbol, end_date, window))
        start = end_date - timedelta(days=window - 1)
        bars = []
        for index in range(window):
            close = D("1000") + D(index)
            bars.append(_bar(start + timedelta(days=index), symbol, str(close), str(close)))
        return tuple(bars)


class BacktestRunnerRegressionTests(unittest.TestCase):
    def test_close_plan_uses_only_t_data_and_gapped_next_open_fills_at_actual_cost(self) -> None:
        d1, d2 = date(2024, 1, 2), date(2024, 1, 5)  # calendar gap is intentional
        symbol = "GAP"
        data = _NoLookaheadData(d1, d2, symbol)
        costs = ExecutionCostConfig(
            buy_slippage_bps=D("0"), sell_slippage_bps=D("0"),
            buy_commission_bps=D("0"), sell_commission_bps=D("0"),
            sell_tax_bps_by_asset_type={"STOCK": D("0")}, fixed_fee_per_order=D("0"),
            tick_rounder=lambda price, _side: price,
        )
        config = BacktestConfig(
            start_date=d1, end_date=d2, universe=(symbol,), market_symbol="MARKET",
            initial_cash=D("10000"), costs=costs,
            risk=BacktestRiskConfig(D("0.20"), 1, D("0.20"), D("0.05"), D("0.50")),
            universe_metadata=_metadata(d1, (symbol, AssetType.STOCK, frozenset())),
        )

        result = BacktestRunner().run(config, data)

        buys = [fill for fill in result.fills if fill.side is Side.BUY]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].trade_date, d2)
        self.assertEqual(buys[0].reference_open, D("200"))
        self.assertEqual(buys[0].fill_price, D("200"))
        # The d1 close is the estimate used for quantity sizing, not the d2 fill price.
        self.assertEqual(buys[0].quantity, 13)
        self.assertEqual(buys[0].cash_delta, D("-2600"))
        self.assertTrue(any(request == (symbol, d1, 201) for request in data.historical_requests))
        self.assertEqual(result.signals[0].scheduled_trade_date, d2)

    def test_point_in_time_universe_filter_excludes_qualifying_symbol_before_history_and_orders(self) -> None:
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        allowed, excluded = "ALLOW", "SPAC"
        data = _NoLookaheadData(d1, d2, allowed)
        data._bars = {
            d1: {allowed: _bar(d1, allowed, "145", "150"), excluded: _bar(d1, excluded, "145", "150")},
            d2: {allowed: _bar(d2, allowed, "200", "205"), excluded: _bar(d2, excluded, "200", "205")},
        }
        costs = ExecutionCostConfig(D("0"), D("0"), D("0"), D("0"), {"STOCK": D("0")}, D("0"), lambda price, _side: price)
        config = BacktestConfig(
            d1, d2, (allowed, excluded), "MARKET", D("10000"), costs,
            BacktestRiskConfig(D("0.20"), 2, D("0.20"), D("0.05"), D("0.50")),
            _metadata(d1, (allowed, AssetType.STOCK, frozenset()), (excluded, AssetType.SPAC, frozenset())),
        )

        result = BacktestRunner().run(config, data)

        self.assertEqual({order.symbol for order in result.orders}, {allowed})
        self.assertEqual({fill.symbol for fill in result.fills}, {allowed})
        self.assertNotIn(excluded, {request[0] for request in data.historical_requests})
        self.assertEqual(
            tuple((item.signal_date, item.symbol, item.reason) for item in result.universe_exclusions),
            ((d1, excluded, "ASSET_TYPE_SPAC"),),
        )


if __name__ == "__main__":
    unittest.main()
