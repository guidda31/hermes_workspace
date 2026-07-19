import unittest
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR

from swing_v2.backtest import (
    ExecutionCostConfig,
    ExitIntent,
    Position,
    Side,
    execute_exit_intents_ioc as _execute_exit_intents_ioc,
)
from swing_v2.contracts import DailyBar


D = Decimal


def execute_exit_intents_ioc(**kwargs: object):
    """Inject an execution namespace unless a test exercises a specific one."""
    kwargs.setdefault("execution_id", "exit-test-execution")
    return _execute_exit_intents_ioc(**kwargs)  # type: ignore[arg-type]


def round_to_won(price: Decimal, side: Side) -> Decimal:
    return price.quantize(D("1"), rounding=ROUND_FLOOR)


COSTS = ExecutionCostConfig(
    buy_slippage_bps=D("10"),
    sell_slippage_bps=D("10"),
    buy_commission_bps=D("15"),
    sell_commission_bps=D("15"),
    sell_tax_bps_by_asset_type={"STOCK": D("20")},
    fixed_fee_per_order=D("7"),
    tick_rounder=round_to_won,
)


def position(symbol: str, *, quantity: int = 10, asset_type: str = "STOCK") -> Position:
    return Position(
        position_id=f"position-{symbol}", symbol=symbol, asset_type=asset_type,
        entry_order_id=f"entry-order-{symbol}", entry_fill_id=f"entry-fill-{symbol}",
        entry_price=D("100"), initial_stop_price=D("90"), quantity=quantity,
        exit_order_id=None, exit_fill_id=None, exit_price=None, exit_reason=None,
        status="OPEN", age_sessions=2,
    )


def bar(symbol: str, *, open_price: str, trade_date: date = date(2026, 2, 3), asset_type: str = "STOCK", is_tradable: bool = True, volume: int = 100, trading_value: str = "10000") -> DailyBar:
    opened = D(open_price)
    return DailyBar(
        trade_date=trade_date, symbol=symbol, asset_type=asset_type,
        open=opened, high=opened + D("1"), low=opened - D("1"), close=opened,
        volume=volume, trading_value=D(trading_value), is_tradable=is_tradable,
    )


class ExitExecutionTests(unittest.TestCase):
    def test_execution_id_namespaces_all_generated_exit_identities(self) -> None:
        result = execute_exit_intents_ioc(
            execution_id="exit-2026-02-03-batch-a",
            positions=(position("FIRST"),),
            exit_intents=(ExitIntent("FIRST", 10, "STOP_CLOSE", date(2026, 2, 2)),),
            next_day_bars={"FIRST": bar("FIRST", open_price="100")},
            scheduled_trade_dates_by_symbol={"FIRST": date(2026, 2, 3)},
            initial_cash=D("0"),
            costs=COSTS,
        )

        order, fill = result.orders[0], result.fills[0]
        for identity in (order.order_id, order.signal_id, fill.fill_id, fill.order_id):
            self.assertIn("exit-2026-02-03-batch-a", identity)

    def test_rejects_non_plain_or_empty_execution_id_before_empty_intent_early_path(self) -> None:
        class StringSubclass(str):
            pass

        for execution_id in ("", 1, None, StringSubclass("not-plain")):
            with self.subTest(execution_id=execution_id):
                with self.assertRaisesRegex(ValueError, "execution_id"):
                    execute_exit_intents_ioc(
                        execution_id=execution_id,
                        positions=(),
                        exit_intents=(),
                        next_day_bars=[],
                        scheduled_trade_dates_by_symbol=[],
                        initial_cash=D("0"),
                        costs=COSTS,
                    )

    def test_executes_multiple_exit_intents_in_order_with_sell_costs_and_closes_positions(self) -> None:
        first, second = position("FIRST", quantity=10), position("SECOND", quantity=5)
        intents = (
            ExitIntent("FIRST", 10, "STOP_CLOSE", date(2026, 2, 2)),
            ExitIntent("SECOND", 5, "MAX_HOLD", date(2026, 2, 2)),
        )

        result = execute_exit_intents_ioc(
            positions=(first, second),
            exit_intents=intents,
            next_day_bars={"FIRST": bar("FIRST", open_price="100"), "SECOND": bar("SECOND", open_price="200")},
            scheduled_trade_dates_by_symbol={"FIRST": date(2026, 2, 3), "SECOND": date(2026, 2, 3)},
            initial_cash=D("1000"),
            costs=COSTS,
        )

        self.assertEqual(tuple(order.status for order in result.orders), ("FILLED", "FILLED"))
        self.assertEqual(tuple(fill.symbol for fill in result.fills), ("FIRST", "SECOND"))
        self.assertEqual(result.fills[0].fill_price, D("99"))
        self.assertEqual(result.fills[0].commission, D("1.485"))
        self.assertEqual(result.fills[0].sell_tax, D("1.98"))
        self.assertEqual(result.fills[0].cash_delta, D("979.535"))
        self.assertEqual(result.cash, D("2964.0525"))
        self.assertEqual(tuple(item.status for item in result.positions), ("CLOSED", "CLOSED"))
        self.assertEqual(result.positions[0].exit_price, D("99"))
        self.assertEqual(result.positions[0].exit_reason, "STOP_CLOSE")
        self.assertEqual(result.positions[1].exit_reason, "MAX_HOLD")

    def test_missing_or_invalid_bar_cancels_its_ioc_without_blocking_other_exit(self) -> None:
        first, second, third = position("MISSING"), position("INVALID"), position("FILLED")
        intents = (
            ExitIntent("MISSING", 10, "STOP_CLOSE", date(2026, 2, 2)),
            ExitIntent("INVALID", 10, "MAX_HOLD", date(2026, 2, 2)),
            ExitIntent("FILLED", 10, "TREND_BREAK", date(2026, 2, 2)),
        )

        result = execute_exit_intents_ioc(
            positions=(first, second, third),
            exit_intents=intents,
            next_day_bars={
                "MISSING": None,
                "INVALID": bar("INVALID", open_price="100", is_tradable=False),
                "FILLED": bar("FILLED", open_price="100"),
            },
            scheduled_trade_dates_by_symbol={
                "MISSING": date(2026, 2, 3),
                "INVALID": date(2026, 2, 3),
                "FILLED": date(2026, 2, 3),
            },
            initial_cash=D("1000"),
            costs=COSTS,
        )

        self.assertEqual(tuple(order.status for order in result.orders), ("CANCELED_UNFILLED", "CANCELED_UNFILLED", "FILLED"))
        self.assertEqual(tuple(order.unfilled_reason for order in result.orders[:2]), ("MISSING_BAR", "NOT_TRADABLE"))
        self.assertEqual(tuple(order.unfilled_quantity for order in result.orders[:2]), (10, 10))
        self.assertEqual(tuple(fill.symbol for fill in result.fills), ("FILLED",))
        self.assertEqual(tuple(item.status for item in result.positions), ("OPEN", "OPEN", "CLOSED"))

    def test_bar_outside_injected_scheduled_session_cancels_without_carry(self) -> None:
        open_position = position("SAME_DAY")
        intent = ExitIntent("SAME_DAY", 10, "STOP_CLOSE", date(2026, 2, 3))

        for trade_date in (date(2026, 2, 2), date(2026, 2, 3)):
            with self.subTest(trade_date=trade_date):
                result = execute_exit_intents_ioc(
                    positions=(open_position,), exit_intents=(intent,),
                    next_day_bars={"SAME_DAY": bar("SAME_DAY", open_price="100", trade_date=trade_date)},
                    scheduled_trade_dates_by_symbol={"SAME_DAY": date(2026, 2, 4)},
                    initial_cash=D("1000"), costs=COSTS,
                )

                self.assertEqual(result.orders[0].status, "CANCELED_UNFILLED")
                self.assertEqual(result.orders[0].unfilled_reason, "SCHEDULED_DATE_MISMATCH")
                self.assertEqual(result.fills, ())
                self.assertEqual(result.positions, (open_position,))
                self.assertEqual(result.cash, D("1000"))

    def test_later_bar_cannot_fill_when_it_misses_injected_scheduled_session(self) -> None:
        open_position = position("LATE")
        intent = ExitIntent("LATE", 10, "STOP_CLOSE", date(2026, 2, 2))

        result = execute_exit_intents_ioc(
            positions=(open_position,),
            exit_intents=(intent,),
            next_day_bars={"LATE": bar("LATE", open_price="100", trade_date=date(2026, 2, 10))},
            scheduled_trade_dates_by_symbol={"LATE": date(2026, 2, 3)},
            initial_cash=D("1000"),
            costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "CANCELED_UNFILLED")
        self.assertEqual(result.orders[0].unfilled_reason, "SCHEDULED_DATE_MISMATCH")
        self.assertEqual(result.orders[0].scheduled_trade_date, date(2026, 2, 3))
        self.assertEqual(result.fills, ())
        self.assertEqual(result.positions, (open_position,))

    def test_injected_non_calendar_next_trading_session_fills(self) -> None:
        open_position = position("HOLIDAY")
        intent = ExitIntent("HOLIDAY", 10, "STOP_CLOSE", date(2026, 2, 6))

        result = execute_exit_intents_ioc(
            positions=(open_position,),
            exit_intents=(intent,),
            next_day_bars={"HOLIDAY": bar("HOLIDAY", open_price="100", trade_date=date(2026, 2, 10))},
            scheduled_trade_dates_by_symbol={"HOLIDAY": date(2026, 2, 10)},
            initial_cash=D("1000"),
            costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "FILLED")
        self.assertEqual(result.orders[0].scheduled_trade_date, date(2026, 2, 10))
        self.assertEqual(result.positions[0].status, "CLOSED")

    def test_exit_intent_has_no_expected_proceeds_contract_and_uses_actual_shared_fill(self) -> None:
        intent = ExitIntent("ACTUAL", 10, "STOP_CLOSE", date(2026, 2, 2))
        self.assertEqual(tuple(intent.__dataclass_fields__), ("symbol", "quantity", "reason", "signal_date"))

        result = execute_exit_intents_ioc(
            positions=(position("ACTUAL"),), exit_intents=(intent,),
            next_day_bars={"ACTUAL": bar("ACTUAL", open_price="87")},
            scheduled_trade_dates_by_symbol={"ACTUAL": date(2026, 2, 3)},
            initial_cash=D("0"), costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "FILLED")
        self.assertEqual(result.fills[0].fill_price, D("86"))
        self.assertEqual(result.fills[0].cash_delta, D("849.99"))
        self.assertEqual(result.cash, D("849.99"))

    def test_rejects_duplicate_intents_or_symbols_and_nonmatching_quantities(self) -> None:
        open_position = position("ONE")
        valid = ExitIntent("ONE", 10, "STOP_CLOSE", date(2026, 2, 2))
        cases = (
            ((valid, valid), (open_position,)),
            ((ExitIntent("ONE", 9, "STOP_CLOSE", date(2026, 2, 2)),), (open_position,)),
            ((ExitIntent("MISSING", 10, "STOP_CLOSE", date(2026, 2, 2)),), (open_position,)),
            ((valid,), (open_position, position("ONE"))),
        )
        for intents, positions in cases:
            with self.subTest(intents=intents, positions=positions):
                with self.assertRaises(ValueError):
                    execute_exit_intents_ioc(
                        positions=positions, exit_intents=intents, next_day_bars={"ONE": None},
                        scheduled_trade_dates_by_symbol={"ONE": date(2026, 2, 3)},
                        initial_cash=D("0"), costs=COSTS,
                    )

    def test_rejects_bar_position_asset_identity_and_malformed_boundary_inputs(self) -> None:
        open_position = position("ONE")
        intent = ExitIntent("ONE", 10, "STOP_CLOSE", date(2026, 2, 2))
        cases = (
            {"positions": (open_position,), "exit_intents": (intent,), "next_day_bars": {"ONE": bar("OTHER", open_price="100")}, "initial_cash": D("0")},
            {"positions": (open_position,), "exit_intents": (intent,), "next_day_bars": {"ONE": bar("ONE", open_price="100", asset_type="ETF")}, "initial_cash": D("0")},
            {"positions": (open_position,), "exit_intents": (ExitIntent("", 10, "STOP_CLOSE", date(2026, 2, 2)),), "next_day_bars": {"ONE": None}, "initial_cash": D("0")},
            {"positions": (open_position,), "exit_intents": (ExitIntent("ONE", True, "STOP_CLOSE", date(2026, 2, 2)),), "next_day_bars": {"ONE": None}, "initial_cash": D("0")},
            {"positions": (open_position,), "exit_intents": (ExitIntent("ONE", 10, "STOP_CLOSE", datetime(2026, 2, 2, 15, 30)),), "next_day_bars": {"ONE": None}, "initial_cash": D("0")},
            {"positions": (open_position,), "exit_intents": (intent,), "next_day_bars": {"ONE": "not-a-bar"}, "initial_cash": D("0")},
            {"positions": (open_position,), "exit_intents": (intent,), "next_day_bars": {"ONE": None}, "initial_cash": D("NaN")},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                kwargs["scheduled_trade_dates_by_symbol"] = {"ONE": date(2026, 2, 3)}
                with self.assertRaises(ValueError):
                    execute_exit_intents_ioc(**kwargs, costs=COSTS)

    def test_rejects_missing_invalid_or_nonforward_scheduled_trade_dates(self) -> None:
        open_position = position("ONE")
        intent = ExitIntent("ONE", 10, "STOP_CLOSE", date(2026, 2, 2))
        cases = (
            [],
            {},
            {"ONE": "2026-02-03"},
            {"ONE": datetime(2026, 2, 3, 9, 0)},
            {"ONE": date(2026, 2, 2)},
            {"ONE": date(2026, 2, 1)},
        )

        for scheduled_dates in cases:
            with self.subTest(scheduled_dates=scheduled_dates):
                with self.assertRaises(ValueError):
                    execute_exit_intents_ioc(
                        positions=(open_position,),
                        exit_intents=(intent,),
                        next_day_bars={"ONE": None},
                        scheduled_trade_dates_by_symbol=scheduled_dates,
                        initial_cash=D("0"),
                        costs=COSTS,
                    )


if __name__ == "__main__":
    unittest.main()
