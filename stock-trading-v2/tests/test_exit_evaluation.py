import unittest
from datetime import date
from decimal import Decimal

from swing_v2.backtest import ExitIntent, Position, evaluate_exit_signals
from swing_v2.contracts import DailyBar


D = Decimal


def position(symbol: str, *, age: int = 1, stop: str = "90", status: str = "OPEN") -> Position:
    return Position(
        position_id=f"position-{symbol}", symbol=symbol, asset_type="STOCK",
        entry_order_id=f"entry-order-{symbol}", entry_fill_id=f"entry-fill-{symbol}",
        entry_price=D("100"), initial_stop_price=D(stop), quantity=10,
        exit_order_id=None, exit_fill_id=None, exit_price=None, exit_reason=None,
        status=status, age_sessions=age,
    )


def bar(
    symbol: str,
    *,
    close: str,
    trade_date: date = date(2026, 2, 2),
    is_tradable: bool = True,
    volume: int = 100,
    trading_value: str = "10000",
    asset_type: str = "STOCK",
) -> DailyBar:
    close_decimal = D(close)
    return DailyBar(
        trade_date=trade_date, symbol=symbol, asset_type=asset_type, open=close_decimal,
        high=close_decimal, low=close_decimal, close=close_decimal,
        volume=volume, trading_value=D(trading_value), is_tradable=is_tradable,
    )


class ExitEvaluationTests(unittest.TestCase):
    def test_first_valid_close_after_entry_advances_age_zero_position_to_one(self) -> None:
        newly_filled = position("NEW", age=0)

        result = evaluate_exit_signals(
            positions=(newly_filled,),
            bars_by_symbol={"NEW": bar("NEW", close="105")},
            historical_closes_by_symbol={"NEW": (D("100"),)},
            pending_exit_symbols=set(),
        )

        self.assertEqual(result.positions[0].age_sessions, 1)
        self.assertEqual(result.exit_intents, ())

    def test_first_valid_close_after_entry_generates_stop_intent(self) -> None:
        newly_filled = position("STOPPED", age=0, stop="90")

        result = evaluate_exit_signals(
            positions=(newly_filled,),
            bars_by_symbol={"STOPPED": bar("STOPPED", close="90")},
            historical_closes_by_symbol={"STOPPED": (D("100"),)},
            pending_exit_symbols=set(),
        )

        self.assertEqual(result.positions[0].age_sessions, 1)
        self.assertEqual(
            result.exit_intents,
            (ExitIntent("STOPPED", 10, "STOP_CLOSE", date(2026, 2, 2)),),
        )

    def test_evaluates_open_positions_independently_and_returns_stop_intent(self) -> None:
        stopped = position("STOPPED", stop="90")
        held = position("HELD")

        result = evaluate_exit_signals(
            positions=(stopped, held),
            bars_by_symbol={"STOPPED": bar("STOPPED", close="90"), "HELD": bar("HELD", close="105")},
            historical_closes_by_symbol={"STOPPED": (D("100"),), "HELD": (D("100"),)},
            pending_exit_symbols=set(),
        )

        self.assertEqual(tuple(item.symbol for item in result.positions), ("STOPPED", "HELD"))
        self.assertEqual(tuple(item.age_sessions for item in result.positions), (2, 2))
        self.assertEqual(len(result.exit_intents), 1)
        intent = result.exit_intents[0]
        self.assertEqual((intent.symbol, intent.quantity, intent.reason, intent.signal_date), ("STOPPED", 10, "STOP_CLOSE", date(2026, 2, 2)))

    def test_uses_stop_then_max_hold_then_trend_priority(self) -> None:
        stopped = position("STOP", age=19, stop="100")
        max_hold = position("MAX", age=19)
        trend = position("TREND", age=9, stop="80")

        result = evaluate_exit_signals(
            positions=(stopped, max_hold, trend),
            bars_by_symbol={
                "STOP": bar("STOP", close="90"),
                "MAX": bar("MAX", close="100"),
                "TREND": bar("TREND", close="90"),
            },
            historical_closes_by_symbol={
                "STOP": (D("100"),),
                "MAX": (D("100"),),
                "TREND": tuple(D("100") for _ in range(19)),
            },
            pending_exit_symbols=set(),
        )

        self.assertEqual(
            tuple(intent.reason for intent in result.exit_intents),
            ("STOP_CLOSE", "MAX_HOLD", "TREND_BREAK"),
        )

    def test_trend_uses_current_close_and_only_the_most_recent_twenty_valid_closes(self) -> None:
        trend = position("TREND", age=9)

        result = evaluate_exit_signals(
            positions=(trend,), bars_by_symbol={"TREND": bar("TREND", close="100")},
            historical_closes_by_symbol={"TREND": (D("1000"),) + tuple(D("100") for _ in range(19))},
            pending_exit_symbols=set(),
        )

        self.assertEqual(result.positions[0].age_sessions, 10)
        self.assertEqual(result.exit_intents, ())

    def test_invalid_or_untradable_bar_preserves_age_zero_open_position_without_signal(self) -> None:
        open_position = position("INVALID", age=0, stop="100")
        for name, invalid_bar in (
            ("untradable", bar("INVALID", close="90", is_tradable=False)),
            ("zero volume", bar("INVALID", close="90", volume=0)),
            ("missing", None),
        ):
            with self.subTest(name=name):
                result = evaluate_exit_signals(
                    positions=(open_position,),
                    bars_by_symbol={"INVALID": invalid_bar},
                    historical_closes_by_symbol={"INVALID": tuple(D("100") for _ in range(19))},
                    pending_exit_symbols=set(),
                )

                self.assertEqual(result.positions, (open_position,))
                self.assertEqual(result.exit_intents, ())

    def test_pending_exit_symbol_suppresses_new_intent_but_updates_age(self) -> None:
        open_position = position("PENDING", age=19)

        result = evaluate_exit_signals(
            positions=(open_position,), bars_by_symbol={"PENDING": bar("PENDING", close="100")},
            historical_closes_by_symbol={"PENDING": (D("100"),)},
            pending_exit_symbols={"PENDING"},
        )

        self.assertEqual(result.positions[0].age_sessions, 20)
        self.assertEqual(result.exit_intents, ())

    def test_closed_position_is_unchanged_and_not_evaluated(self) -> None:
        closed = position("CLOSED", age=0, status="CLOSED")

        result = evaluate_exit_signals(
            positions=(closed,), bars_by_symbol={}, historical_closes_by_symbol={}, pending_exit_symbols=set(),
        )

        self.assertEqual(result.positions, (closed,))
        self.assertEqual(result.exit_intents, ())

    def test_rejects_invalid_portfolio_evaluation_inputs(self) -> None:
        open_position = position("ONE")
        cases = (
            (
                "bar identity",
                (open_position,),
                {"ONE": bar("OTHER", close="100")},
                {"ONE": (D("100"),)},
                set(),
            ),
            (
                "asset identity",
                (open_position,),
                {"ONE": bar("ONE", close="100", asset_type="ETF")},
                {"ONE": (D("100"),)},
                set(),
            ),
            (
                "duplicate symbols",
                (open_position, position("ONE")),
                {"ONE": bar("ONE", close="100")},
                {"ONE": (D("100"),)},
                set(),
            ),
            (
                "invalid history",
                (open_position,),
                {"ONE": bar("ONE", close="100")},
                {"ONE": (D("NaN"),)},
                set(),
            ),
            (
                "missing history",
                (open_position,),
                {"ONE": bar("ONE", close="100")},
                {},
                set(),
            ),
            (
                "negative age",
                (position("ONE", age=-1),),
                {"ONE": bar("ONE", close="100")},
                {"ONE": (D("100"),)},
                set(),
            ),
            (
                "invalid pending set",
                (open_position,),
                {"ONE": bar("ONE", close="100")},
                {"ONE": (D("100"),)},
                ("ONE",),
            ),
        )
        for name, positions, bars, histories, pending in cases:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    evaluate_exit_signals(
                        positions=positions,
                        bars_by_symbol=bars,
                        historical_closes_by_symbol=histories,
                        pending_exit_symbols=pending,
                    )


if __name__ == "__main__":
    unittest.main()
