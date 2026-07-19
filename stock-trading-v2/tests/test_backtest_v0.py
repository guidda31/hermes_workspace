import unittest
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from swing_v2.contracts import DailyBar
from swing_v2.backtest import (
    ExecutionCostConfig,
    Side,
    calculate_entry_quantity,
    run_single_position_backtest,
    run_two_day_backtest,
)


D = Decimal


def bar(
    trade_date: date,
    *,
    open_price: str,
    close_price: str,
    symbol: str = "005930",
    asset_type: str = "STOCK",
    is_tradable: bool = True,
    volume: int = 100,
    trading_value: str | None = None,
) -> DailyBar:
    high = max(D(open_price), D(close_price)) + D("1")
    low = min(D(open_price), D(close_price)) - D("1")
    return DailyBar(
        trade_date=trade_date,
        symbol=symbol,
        asset_type=asset_type,
        open=D(open_price),
        high=high,
        low=low,
        close=D(close_price),
        volume=volume,
        trading_value=D(trading_value) if trading_value is not None else D(close_price) * volume,
        is_tradable=is_tradable,
    )


def round_to_won(price: Decimal, side: Side) -> Decimal:
    rounding = ROUND_CEILING if side is Side.BUY else ROUND_FLOOR
    return price.quantize(D("1"), rounding=rounding)


COSTS = ExecutionCostConfig(
    buy_slippage_bps=D("10"),
    sell_slippage_bps=D("10"),
    buy_commission_bps=D("15"),
    sell_commission_bps=D("15"),
    sell_tax_bps_by_asset_type={"STOCK": D("20")},
    fixed_fee_per_order=D("7"),
    tick_rounder=round_to_won,
)


class BacktestV0Tests(unittest.TestCase):
    INVALID_INITIAL_STOP_PCTS = (
        D("-0.01"), D("0"), D("1"), D("1.01"), D("NaN"), D("Infinity"), D("-Infinity"),
    )

    def test_two_day_backtest_rejects_invalid_initial_stop_pct_on_normal_path(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="100", close_price="101")

        for initial_stop_pct in self.INVALID_INITIAL_STOP_PCTS:
            with self.subTest(initial_stop_pct=initial_stop_pct):
                with self.assertRaises(ValueError):
                    run_two_day_backtest(
                        bars=(signal_day, entry_day), initial_cash=D("10000"), quantity=10,
                        signal_at_t_close=True, costs=COSTS, initial_stop_pct=initial_stop_pct,
                    )

    def test_two_day_backtest_rejects_invalid_initial_stop_pct_before_early_return(self) -> None:
        for initial_stop_pct in self.INVALID_INITIAL_STOP_PCTS:
            with self.subTest(initial_stop_pct=initial_stop_pct, path="empty-bars"):
                with self.assertRaises(ValueError):
                    run_two_day_backtest(
                        bars=(), initial_cash=D("10000"), quantity=10,
                        signal_at_t_close=True, costs=COSTS, initial_stop_pct=initial_stop_pct,
                    )
            with self.subTest(initial_stop_pct=initial_stop_pct, path="false-signal"):
                with self.assertRaises(ValueError):
                    run_two_day_backtest(
                        bars=(bar(date(2026, 1, 2), open_price="100", close_price="101"),),
                        initial_cash=D("10000"), quantity=10,
                        signal_at_t_close=False, costs=COSTS, initial_stop_pct=initial_stop_pct,
                    )

    def test_single_position_backtest_rejects_invalid_initial_stop_pct_on_normal_path(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="100", close_price="101")

        for initial_stop_pct in self.INVALID_INITIAL_STOP_PCTS:
            with self.subTest(initial_stop_pct=initial_stop_pct):
                with self.assertRaises(ValueError):
                    run_single_position_backtest(
                        bars=(signal_day, entry_day), initial_cash=D("10000"), quantity=10,
                        costs=COSTS, initial_stop_pct=initial_stop_pct,
                    )

    def test_single_position_backtest_rejects_invalid_initial_stop_pct_before_early_return(self) -> None:
        for initial_stop_pct in self.INVALID_INITIAL_STOP_PCTS:
            with self.subTest(initial_stop_pct=initial_stop_pct, path="empty-bars"):
                with self.assertRaises(ValueError):
                    run_single_position_backtest(
                        bars=(), initial_cash=D("10000"), quantity=10,
                        costs=COSTS, initial_stop_pct=initial_stop_pct,
                    )
            with self.subTest(initial_stop_pct=initial_stop_pct, path="short-bars"):
                with self.assertRaises(ValueError):
                    run_single_position_backtest(
                        bars=(bar(date(2026, 1, 2), open_price="100", close_price="101"),),
                        initial_cash=D("10000"), quantity=10,
                        costs=COSTS, initial_stop_pct=initial_stop_pct,
                    )

    def test_two_day_backtest_uses_configured_initial_stop_pct(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="100", close_price="101")

        result = run_two_day_backtest(
            bars=(signal_day, entry_day), initial_cash=D("10000"), quantity=10,
            signal_at_t_close=True, costs=COSTS, initial_stop_pct=D("0.10"),
        )

        self.assertEqual(result.positions[0].initial_stop_price, D("90.9"))

    def test_single_position_backtest_uses_configured_initial_stop_pct(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="100", close_price="101")

        result = run_single_position_backtest(
            bars=(signal_day, entry_day), initial_cash=D("10000"), quantity=10,
            costs=COSTS, initial_stop_pct=D("0.02"),
        )

        self.assertEqual(result.positions[0].initial_stop_price, D("98.98"))

    def test_sizing_and_two_day_engine_use_the_same_initial_stop_pct(self) -> None:
        initial_stop_pct = D("0.10")
        quantity = calculate_entry_quantity(
            expected_open_price=D("100"), nav=D("10000"), available_cash=D("10000"),
            costs=COSTS, asset_type="STOCK", risk_per_position=D("0.01"),
            max_position_notional_pct=D("0.20"), initial_stop_pct=initial_stop_pct,
        )
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="100", close_price="101")

        result = run_two_day_backtest(
            bars=(signal_day, entry_day), initial_cash=D("10000"), quantity=quantity,
            signal_at_t_close=True, costs=COSTS, initial_stop_pct=initial_stop_pct,
        )

        self.assertEqual(quantity, 9)
        self.assertEqual(
            result.positions[0].initial_stop_price,
            result.positions[0].entry_price * (D("1") - initial_stop_pct),
        )

    def test_entry_fill_day_valid_close_starts_age_at_one(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="100", close_price="101")

        result = run_single_position_backtest(
            bars=(signal_day, entry_day),
            initial_cash=D("10000"),
            quantity=10,
            costs=COSTS,
        )

        self.assertEqual(result.positions[0].age_sessions, 1)

    def test_untradable_close_below_stop_does_not_create_stop_exit_intent(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="100", close_price="101")
        untradable_stop_day = bar(
            date(2026, 1, 6), open_price="100", close_price="90", is_tradable=False,
        )
        next_tradable_day = bar(date(2026, 1, 7), open_price="100", close_price="101")

        result = run_single_position_backtest(
            bars=(signal_day, entry_day, untradable_stop_day, next_tradable_day),
            initial_cash=D("10000"),
            quantity=10,
            costs=COSTS,
        )

        self.assertEqual(len(result.orders), 1)
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.positions[0].status, "OPEN")
        self.assertEqual(result.positions[0].age_sessions, 2)
        self.assertIsNone(result.positions[0].exit_reason)
        self.assertFalse(any(order.intent_reason == "STOP_CLOSE" for order in result.orders))

    def test_stop_close_wins_over_max_hold_and_exits_at_next_open(self) -> None:
        start = date(2026, 1, 2)
        bars = [bar(start, open_price="100", close_price="101")]
        bars.extend(
            bar(start + timedelta(days=offset), open_price="100", close_price="101")
            for offset in range(1, 20)
        )
        stop_and_max_hold_day = bar(
            start + timedelta(days=20), open_price="100", close_price="95"
        )
        exit_day = bar(start + timedelta(days=21), open_price="90", close_price="91")
        bars.extend((stop_and_max_hold_day, exit_day))

        result = run_single_position_backtest(
            bars=tuple(bars), initial_cash=D("10000"), quantity=10, costs=COSTS,
        )

        self.assertEqual(len(result.fills), 2)
        self.assertEqual(result.positions[0].exit_reason, "STOP_CLOSE")
        self.assertEqual(result.orders[-1].signal_date, stop_and_max_hold_day.trade_date)
        self.assertEqual(result.fills[-1].trade_date, exit_day.trade_date)

    def test_max_hold_at_twenty_valid_closes_exits_at_next_open(self) -> None:
        start = date(2026, 2, 2)
        bars = [bar(start, open_price="100", close_price="101")]
        bars.extend(
            bar(start + timedelta(days=offset), open_price="100", close_price="101")
            for offset in range(1, 21)
        )
        exit_day = bar(start + timedelta(days=21), open_price="110", close_price="111")
        bars.append(exit_day)

        result = run_single_position_backtest(
            bars=tuple(bars), initial_cash=D("10000"), quantity=10, costs=COSTS,
        )

        self.assertEqual(result.positions[0].age_sessions, 20)
        self.assertEqual(result.positions[0].exit_reason, "MAX_HOLD")
        self.assertEqual(result.fills[-1].trade_date, exit_day.trade_date)

    def test_max_hold_wins_over_trend_break_on_the_same_close(self) -> None:
        start = date(2026, 2, 2)
        bars = [bar(start, open_price="100", close_price="101")]
        bars.extend(
            bar(start + timedelta(days=offset), open_price="100", close_price="101")
            for offset in range(1, 20)
        )
        both_conditions_day = bar(
            start + timedelta(days=20), open_price="100", close_price="97"
        )
        exit_day = bar(start + timedelta(days=21), open_price="96", close_price="96")
        bars.extend((both_conditions_day, exit_day))

        result = run_single_position_backtest(
            bars=tuple(bars), initial_cash=D("10000"), quantity=10, costs=COSTS,
        )

        self.assertEqual(result.positions[0].exit_reason, "MAX_HOLD")
        self.assertEqual(result.orders[-1].signal_date, both_conditions_day.trade_date)

    def test_trend_break_after_ten_sessions_uses_current_sma20_and_next_open(self) -> None:
        start = date(2026, 3, 2)
        bars = [bar(start, open_price="100", close_price="100")]
        bars.extend(
            bar(start + timedelta(days=offset), open_price="100", close_price="100")
            for offset in range(1, 19)
        )
        trend_break_day = bar(
            start + timedelta(days=19), open_price="100", close_price="97"
        )
        exit_day = bar(start + timedelta(days=20), open_price="96", close_price="96")
        bars.extend((trend_break_day, exit_day))

        result = run_single_position_backtest(
            bars=tuple(bars), initial_cash=D("10000"), quantity=10, costs=COSTS,
        )

        self.assertEqual(result.positions[0].exit_reason, "TREND_BREAK")
        self.assertEqual(result.orders[-1].signal_date, trend_break_day.trade_date)
        self.assertEqual(result.fills[-1].trade_date, exit_day.trade_date)

    def test_signal_at_t_is_filled_only_at_t_plus_1_open_with_buy_costs(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        execution_day = bar(date(2026, 1, 5), open_price="200", close_price="205")

        result = run_two_day_backtest(
            bars=(signal_day, execution_day),
            initial_cash=D("10000"),
            quantity=10,
            signal_at_t_close=True,
            costs=COSTS,
        )

        self.assertEqual(result.orders[0].signal_date, signal_day.trade_date)
        self.assertEqual(result.orders[0].scheduled_trade_date, execution_day.trade_date)
        self.assertEqual(result.orders[0].status, "FILLED")
        self.assertEqual(len(result.fills), 1)
        fill = result.fills[0]
        self.assertEqual(fill.trade_date, execution_day.trade_date)
        self.assertEqual(fill.side, Side.BUY)
        self.assertEqual(fill.reference_open, D("200"))
        self.assertEqual(fill.raw_slippage_price, D("200.2"))
        self.assertEqual(fill.fill_price, D("201"))
        self.assertEqual(fill.notional, D("2010"))
        self.assertEqual(fill.commission, D("3.015"))
        self.assertEqual(fill.fixed_fee, D("7"))
        self.assertEqual(fill.cash_delta, D("-2020.015"))
        self.assertEqual(result.cash, D("7979.985"))
        self.assertEqual(result.positions[0].entry_price, D("201"))

    def test_non_none_bar_with_mismatched_identity_is_rejected(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        for execution_day in (
            bar(
                date(2026, 1, 5), open_price="200", close_price="205",
                symbol="000660",
            ),
            bar(
                date(2026, 1, 5), open_price="200", close_price="205",
                asset_type="ETF",
            ),
        ):
            with self.subTest(execution_day=execution_day):
                with self.assertRaises(ValueError):
                    run_two_day_backtest(
                        bars=(signal_day, execution_day),
                        initial_cash=D("10000"),
                        quantity=10,
                        signal_at_t_close=True,
                        costs=COSTS,
                    )

    def test_untradable_or_missing_t_plus_1_cancels_instead_of_delaying_fill(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        later_tradable_day = bar(date(2026, 1, 6), open_price="300", close_price="301")

        for unavailable_day in (
            bar(date(2026, 1, 5), open_price="200", close_price="201", is_tradable=False),
            None,
        ):
            with self.subTest(unavailable_day=unavailable_day):
                result = run_two_day_backtest(
                    bars=(signal_day, unavailable_day, later_tradable_day),
                    initial_cash=D("10000"),
                    quantity=10,
                    signal_at_t_close=True,
                    costs=COSTS,
                )

                self.assertEqual(len(result.fills), 0)
                self.assertEqual(len(result.orders), 1)
                self.assertEqual(result.orders[0].status, "CANCELED_UNFILLED")
                self.assertEqual(result.orders[0].filled_quantity, 0)
                self.assertEqual(result.orders[0].unfilled_quantity, 10)
                self.assertEqual(result.cash, D("10000"))

    def test_close_stop_signal_exits_next_open_not_same_close(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="200", close_price="190")
        exit_day = bar(date(2026, 1, 6), open_price="180", close_price="181")

        result = run_two_day_backtest(
            bars=(signal_day, entry_day, exit_day),
            initial_cash=D("10000"),
            quantity=10,
            signal_at_t_close=True,
            costs=COSTS,
        )

        self.assertEqual(len(result.fills), 2)
        exit_order = result.orders[1]
        exit_fill = result.fills[1]
        self.assertEqual(exit_order.intent_reason, "STOP_CLOSE")
        self.assertEqual(exit_order.signal_date, entry_day.trade_date)
        self.assertEqual(exit_order.scheduled_trade_date, exit_day.trade_date)
        self.assertEqual(exit_fill.trade_date, exit_day.trade_date)
        self.assertEqual(exit_fill.reference_open, D("180"))
        self.assertEqual(exit_fill.fill_price, D("179"))
        self.assertEqual(exit_fill.sell_tax, D("3.58"))
        self.assertEqual(exit_fill.cash_delta, D("1776.735"))
        self.assertEqual(result.cash, D("9756.72"))
        self.assertEqual(result.positions[0].status, "CLOSED")
        self.assertEqual(result.positions[0].exit_reason, "STOP_CLOSE")
        self.assertNotEqual(exit_fill.fill_price, entry_day.close)

    def test_invalid_stop_breach_close_does_not_create_ghost_stop_exit(self) -> None:
        signal_day = bar(date(2026, 1, 2), open_price="100", close_price="101")
        entry_day = bar(date(2026, 1, 5), open_price="100", close_price="101")
        next_valid_day = bar(date(2026, 1, 7), open_price="100", close_price="101")

        for invalid_stop_day in (
            bar(date(2026, 1, 6), open_price="100", close_price="90", is_tradable=False),
            bar(date(2026, 1, 6), open_price="100", close_price="90", volume=0),
            bar(date(2026, 1, 6), open_price="100", close_price="90", trading_value="0"),
        ):
            with self.subTest(invalid_stop_day=invalid_stop_day):
                result = run_two_day_backtest(
                    bars=(signal_day, entry_day, invalid_stop_day, next_valid_day),
                    initial_cash=D("10000"),
                    quantity=10,
                    signal_at_t_close=True,
                    costs=COSTS,
                )

                self.assertEqual(len(result.orders), 1)
                self.assertEqual(len(result.fills), 1)
                self.assertEqual(result.positions[0].status, "OPEN")
                self.assertIsNone(result.positions[0].exit_reason)
                self.assertFalse(any(order.intent_reason == "STOP_CLOSE" for order in result.orders))


if __name__ == "__main__":
    unittest.main()
