import unittest
from datetime import date
from decimal import Decimal

from swing_v2.contracts import DailyBar
from swing_v2.signals import (
    is_momentum_breakout,
    is_risk_on,
    passes_liquidity_filter,
    should_open_position,
)


def make_daily_bar(
    *,
    close: Decimal = Decimal("1000"),
    trading_value: Decimal = Decimal("1000000000"),
    is_tradable: bool = True,
) -> DailyBar:
    return DailyBar(
        trade_date=date(2026, 1, 2),
        symbol="005930",
        asset_type="STOCK",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1,
        trading_value=trading_value,
        is_tradable=is_tradable,
    )


class RiskOnSignalTests(unittest.TestCase):
    def test_returns_false_with_fewer_than_200_closes(self) -> None:
        closes = [Decimal("100")] * 199

        self.assertFalse(is_risk_on(closes))

    def test_returns_true_when_latest_close_and_50_day_average_are_above_200_day_average(self) -> None:
        closes = [Decimal("100")] * 199 + [Decimal("101")]

        self.assertTrue(is_risk_on(closes))

    def test_returns_false_when_50_day_average_is_not_above_200_day_average(self) -> None:
        closes = [Decimal("200")] * 150 + [Decimal("100")] * 49 + [Decimal("300")]

        self.assertFalse(is_risk_on(closes))

    def test_returns_false_when_latest_close_equals_200_day_average(self) -> None:
        closes = [Decimal("100")] * 200

        self.assertFalse(is_risk_on(closes))


class MomentumBreakoutSignalTests(unittest.TestCase):
    def test_returns_false_with_exactly_60_closes(self) -> None:
        closes = [Decimal("100")] * 60

        self.assertFalse(is_momentum_breakout(closes))

    def test_returns_true_with_exactly_61_closes_when_all_conditions_are_met(self) -> None:
        closes = [Decimal("100")] * 40 + [Decimal("110")] * 20 + [Decimal("120")]

        self.assertTrue(is_momentum_breakout(closes))

    def test_returns_false_when_latest_close_equals_the_prior_20_day_high(self) -> None:
        closes = [Decimal("100")] * 40 + [Decimal("110")] * 19 + [Decimal("120")] * 2

        self.assertFalse(is_momentum_breakout(closes))
    def test_returns_false_when_latest_close_is_not_higher_than_60_trading_days_ago(self) -> None:
        closes = [Decimal("120")] + [Decimal("100")] * 40 + [Decimal("110")] * 19 + [Decimal("120")]

        self.assertFalse(is_momentum_breakout(closes))


class LiquidityFilterTests(unittest.TestCase):
    def test_returns_true_at_all_liquidity_thresholds(self) -> None:
        bars = [make_daily_bar() for _ in range(20)]

        self.assertTrue(passes_liquidity_filter(bars))

    def test_returns_false_with_fewer_than_20_bars(self) -> None:
        bars = [make_daily_bar() for _ in range(19)]

        self.assertFalse(passes_liquidity_filter(bars))

    def test_returns_false_when_a_recent_bar_is_not_tradable(self) -> None:
        bars = [make_daily_bar() for _ in range(19)] + [make_daily_bar(is_tradable=False)]

        self.assertFalse(passes_liquidity_filter(bars))

    def test_returns_false_when_latest_close_is_below_1000_won(self) -> None:
        bars = [make_daily_bar() for _ in range(19)] + [make_daily_bar(close=Decimal("999"))]

        self.assertFalse(passes_liquidity_filter(bars))

    def test_returns_false_when_average_trading_value_is_below_one_billion_won(self) -> None:
        bars = [make_daily_bar(trading_value=Decimal("999999999")) for _ in range(20)]

        self.assertFalse(passes_liquidity_filter(bars))


class PositionOpeningSignalTests(unittest.TestCase):
    def test_returns_true_when_market_liquidity_and_momentum_conditions_all_pass(self) -> None:
        market_closes = [Decimal("100")] * 199 + [Decimal("101")]
        asset_bars = [make_daily_bar(close=Decimal("1100")) for _ in range(60)] + [
            make_daily_bar(close=Decimal("1200"))
        ]

        self.assertTrue(should_open_position(market_closes, asset_bars))

    def test_returns_false_when_market_is_not_risk_on(self) -> None:
        market_closes = [Decimal("100")] * 200
        asset_bars = [make_daily_bar(close=Decimal("1100")) for _ in range(60)] + [
            make_daily_bar(close=Decimal("1200"))
        ]

        self.assertFalse(should_open_position(market_closes, asset_bars))

    def test_returns_false_when_asset_fails_liquidity_filter(self) -> None:
        market_closes = [Decimal("100")] * 199 + [Decimal("101")]
        asset_bars = [make_daily_bar(close=Decimal("1100")) for _ in range(50)] + [
            make_daily_bar(close=Decimal("1100"), is_tradable=False)
        ] + [make_daily_bar(close=Decimal("1100")) for _ in range(9)] + [
            make_daily_bar(close=Decimal("1200"))
        ]

        self.assertFalse(should_open_position(market_closes, asset_bars))

    def test_returns_false_when_asset_fails_momentum_breakout(self) -> None:
        market_closes = [Decimal("100")] * 199 + [Decimal("101")]
        asset_bars = [make_daily_bar(close=Decimal("1100")) for _ in range(61)]

        self.assertFalse(should_open_position(market_closes, asset_bars))


if __name__ == "__main__":
    unittest.main()
