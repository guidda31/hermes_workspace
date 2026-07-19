import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from swing_v2.backtest import (
    Candidate,
    EntryCandidate,
    EntryPlan,
    ExecutionCostConfig,
    Side,
    create_entry_plans,
    execute_entry_plans_ioc as _execute_entry_plans_ioc,
    evaluate_exit_signals,
)
from swing_v2.contracts import DailyBar


D = Decimal


def execute_entry_plans_ioc(**kwargs: Any):
    """Inject the fixture's known next market session unless a test overrides it."""
    kwargs.setdefault("execution_id", "entry-test-execution")
    kwargs.setdefault(
        "scheduled_trade_dates_by_symbol",
        {
            plan.symbol: date(2026, 2, 3)
            for plan in kwargs["plans"]
            if isinstance(plan.symbol, str)
        },
    )
    return _execute_entry_plans_ioc(**kwargs)


def round_to_won(price: Decimal, side: Side) -> Decimal:
    return price.quantize(D("1"), rounding=ROUND_CEILING if side is Side.BUY else ROUND_FLOOR)


COSTS = ExecutionCostConfig(
    buy_slippage_bps=D("10"),
    sell_slippage_bps=D("10"),
    buy_commission_bps=D("15"),
    sell_commission_bps=D("15"),
    sell_tax_bps_by_asset_type={"STOCK": D("20")},
    fixed_fee_per_order=D("7"),
    tick_rounder=round_to_won,
)


def plan(
    symbol: str,
    *,
    quantity: int = 10,
    expected_open: str = "100",
    stop_pct: str = "0.05",
    signal_date: date = date(2026, 2, 2),
) -> EntryPlan:
    expected_fill = D(expected_open) * D("1.001")
    expected_fill = round_to_won(expected_fill, Side.BUY)
    cash_cost = expected_fill * quantity
    cash_cost += cash_cost * COSTS.buy_commission_bps / D("10000")
    cash_cost += COSTS.fixed_fee_per_order
    return EntryPlan(
        symbol=symbol,
        asset_type="STOCK",
        signal_date=signal_date,
        expected_open_price=D(expected_open),
        quantity=quantity,
        expected_fill_price=expected_fill,
        expected_cash_cost=cash_cost,
        nav=D("100000"),
        risk_per_position=D("0.01"),
        max_position_notional_pct=D("0.20"),
        initial_stop_pct=D(stop_pct),
        costs=COSTS,
    )


def bar(symbol: str, *, open_price: str = "100", close_price: str | None = None, is_tradable: bool = True, volume: int = 100, trading_value: str = "10000", asset_type: str = "STOCK", trade_date: date = date(2026, 2, 3)) -> DailyBar:
    open_decimal = D(open_price)
    close_decimal = D(close_price) if close_price is not None else open_decimal
    return DailyBar(
        trade_date=trade_date,
        symbol=symbol,
        asset_type=asset_type,
        open=open_decimal,
        high=max(open_decimal, close_decimal) + D("1"),
        low=min(open_decimal, close_decimal) - D("1"),
        close=close_decimal,
        volume=volume,
        trading_value=D(trading_value),
        is_tradable=is_tradable,
    )


class EntryPlanExecutionTests(unittest.TestCase):
    def test_execution_id_namespaces_all_generated_entry_identities(self) -> None:
        result = execute_entry_plans_ioc(
            execution_id="entry-2026-02-03-batch-a",
            plans=(plan("FIRST"),),
            next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
        )

        order, fill, position = result.orders[0], result.fills[0], result.positions[0]
        for identity in (
            order.order_id,
            order.signal_id,
            order.position_id,
            fill.fill_id,
            fill.order_id,
            fill.position_id,
            position.position_id,
        ):
            self.assertIn("entry-2026-02-03-batch-a", identity)

    def test_rejects_non_plain_or_empty_execution_id_before_empty_plan_early_path(self) -> None:
        class StringSubclass(str):
            pass

        for execution_id in ("", 1, None, StringSubclass("not-plain")):
            with self.subTest(execution_id=execution_id):
                with self.assertRaisesRegex(ValueError, "execution_id"):
                    _execute_entry_plans_ioc(
                        execution_id=execution_id,
                        plans=(),
                        next_day_bars=[],
                        scheduled_trade_dates_by_symbol=[],
                        initial_cash=D("0"),
                        available_cash=D("0"),
                        costs=COSTS,
                    )

    def test_executes_injected_holiday_session_with_plan_signal_date_in_order(self) -> None:
        entry_plan = plan("HOLIDAY", signal_date=date(2026, 2, 6))
        scheduled_date = date(2026, 2, 10)

        result = execute_entry_plans_ioc(
            plans=(entry_plan,),
            next_day_bars={"HOLIDAY": bar("HOLIDAY", trade_date=scheduled_date)},
            scheduled_trade_dates_by_symbol={"HOLIDAY": scheduled_date},
            initial_cash=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "FILLED")
        self.assertEqual(result.orders[0].signal_date, date(2026, 2, 6))
        self.assertEqual(result.orders[0].scheduled_trade_date, scheduled_date)
        self.assertEqual(result.fills[0].trade_date, scheduled_date)

    def test_delayed_entry_bar_cancels_its_single_ioc(self) -> None:
        entry_plan = plan("LATE", signal_date=date(2026, 2, 2))

        result = execute_entry_plans_ioc(
            plans=(entry_plan,),
            next_day_bars={"LATE": bar("LATE", trade_date=date(2026, 2, 10))},
            scheduled_trade_dates_by_symbol={"LATE": date(2026, 2, 3)},
            initial_cash=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "CANCELED_UNFILLED")
        self.assertEqual(result.orders[0].unfilled_reason, "SCHEDULED_DATE_MISMATCH")

    def test_invalid_entry_bar_keeps_its_existing_ioc_cancellation_reason(self) -> None:
        entry_plan = plan("HALTED", signal_date=date(2026, 2, 2))

        result = execute_entry_plans_ioc(
            plans=(entry_plan,),
            next_day_bars={
                "HALTED": bar("HALTED", is_tradable=False, trade_date=date(2026, 2, 10))
            },
            scheduled_trade_dates_by_symbol={"HALTED": date(2026, 2, 3)},
            initial_cash=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "CANCELED_UNFILLED")
        self.assertEqual(result.orders[0].unfilled_reason, "NOT_TRADABLE")

    def test_rejects_scheduled_date_on_or_before_plan_signal_date(self) -> None:
        entry_plan = plan("FIRST", signal_date=date(2026, 2, 2))
        for scheduled_date in (date(2026, 2, 2), date(2026, 2, 1)):
            with self.subTest(scheduled_date=scheduled_date):
                with self.assertRaisesRegex(ValueError, "strictly after"):
                    execute_entry_plans_ioc(
                        plans=(entry_plan,),
                        next_day_bars={"FIRST": None},
                        scheduled_trade_dates_by_symbol={"FIRST": scheduled_date},
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )

    def test_requires_a_plain_scheduled_date_for_every_plan(self) -> None:
        entry_plan = plan("FIRST")
        for scheduled_dates in ({}, [], {"FIRST": datetime(2026, 2, 3, 9)}):
            with self.subTest(scheduled_dates=scheduled_dates):
                with self.assertRaisesRegex(ValueError, "scheduled"):
                    execute_entry_plans_ioc(
                        plans=(entry_plan,),
                        next_day_bars={"FIRST": None},
                        scheduled_trade_dates_by_symbol=scheduled_dates,
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )

    def test_rejects_invalid_plan_and_scheduled_signal_dates(self) -> None:
        valid_plan = plan("FIRST")
        for signal_date in (datetime(2026, 2, 2, 9), "2026-02-02", None):
            with self.subTest(signal_date=signal_date):
                with self.assertRaisesRegex(ValueError, "signal_date"):
                    execute_entry_plans_ioc(
                        plans=(replace(valid_plan, signal_date=signal_date),),
                        next_day_bars={"FIRST": None},
                        scheduled_trade_dates_by_symbol={"FIRST": date(2026, 2, 3)},
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )
        for scheduled_date in (datetime(2026, 2, 3, 9), "2026-02-03", None):
            with self.subTest(scheduled_date=scheduled_date):
                with self.assertRaisesRegex(ValueError, "plain date"):
                    execute_entry_plans_ioc(
                        plans=(valid_plan,),
                        next_day_bars={"FIRST": None},
                        scheduled_trade_dates_by_symbol={"FIRST": scheduled_date},
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )

    def test_filled_entry_position_can_be_evaluated_at_its_same_day_valid_close(self) -> None:
        entry_plan = plan("FIRST")
        today = bar("FIRST", close_price="105")
        entry_result = execute_entry_plans_ioc(
            plans=(entry_plan,),
            next_day_bars={"FIRST": today},
            initial_cash=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
        )

        evaluation = evaluate_exit_signals(
            positions=entry_result.positions,
            bars_by_symbol={"FIRST": today},
            historical_closes_by_symbol={"FIRST": (D("99"), D("100"))},
            pending_exit_symbols=set(),
        )

        self.assertEqual(evaluation.positions[0].age_sessions, 1)
        self.assertEqual(evaluation.exit_intents, ())

    def test_executes_multiple_plans_in_input_rank_order_and_updates_cash(self) -> None:
        first, second = plan("FIRST"), plan("SECOND", quantity=5)

        result = execute_entry_plans_ioc(
            plans=(first, second),
            next_day_bars={"FIRST": bar("FIRST"), "SECOND": bar("SECOND")},
            initial_cash=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
        )

        self.assertEqual(tuple(order.status for order in result.orders), ("FILLED", "FILLED"))
        self.assertEqual(tuple(fill.symbol for fill in result.fills), ("FIRST", "SECOND"))
        self.assertEqual(tuple(position.symbol for position in result.positions), ("FIRST", "SECOND"))
        self.assertEqual(result.fills[0].fill_price, first.expected_fill_price)
        self.assertEqual(result.fills[0].commission, D("1.515"))
        self.assertEqual(result.fills[0].fixed_fee, COSTS.fixed_fee_per_order)
        self.assertEqual(-result.fills[0].cash_delta, first.expected_cash_cost)
        self.assertEqual(result.cash, D("8468.7275"))

    def test_invalid_bar_cancels_only_its_ioc_and_does_not_block_other_plan(self) -> None:
        first, second = plan("FIRST"), plan("SECOND")

        result = execute_entry_plans_ioc(
            plans=(first, second),
            next_day_bars={"FIRST": None, "SECOND": bar("SECOND")},
            initial_cash=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "CANCELED_UNFILLED")
        self.assertEqual(result.orders[0].unfilled_reason, "MISSING_BAR")
        self.assertEqual(result.orders[1].status, "FILLED")
        self.assertEqual(tuple(fill.symbol for fill in result.fills), ("SECOND",))
        self.assertEqual(result.cash, D("8981.485"))

    def test_insufficient_available_cash_fills_earlier_plan_and_cancels_later_plan(self) -> None:
        first, second = plan("FIRST"), plan("SECOND")

        result = execute_entry_plans_ioc(
            plans=(first, second),
            next_day_bars={"FIRST": bar("FIRST"), "SECOND": bar("SECOND")},
            initial_cash=D("10000"),
            available_cash=first.expected_cash_cost,
            costs=COSTS,
        )

        self.assertEqual(tuple(order.status for order in result.orders), ("FILLED", "CANCELED_UNFILLED"))
        self.assertEqual(result.orders[1].unfilled_reason, "CASH_UNAVAILABLE")
        self.assertEqual(tuple(fill.symbol for fill in result.fills), ("FIRST",))

    def test_rejects_available_cash_above_initial_cash(self) -> None:
        with self.assertRaisesRegex(ValueError, "available_cash"):
            execute_entry_plans_ioc(
                plans=(plan("FIRST"),),
                next_day_bars={"FIRST": bar("FIRST")},
                initial_cash=D("0"),
                available_cash=D("100"),
                costs=COSTS,
            )

    def test_rejects_malformed_plan_quantities_at_api_boundary(self) -> None:
        valid_plan = plan("FIRST")

        for quantity in (0, -1, 1.5, True):
            with self.subTest(quantity=quantity):
                with self.assertRaisesRegex(ValueError, "quantity"):
                    execute_entry_plans_ioc(
                        plans=(replace(valid_plan, quantity=quantity),),
                        next_day_bars={"FIRST": None},
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )

    def test_rejects_malformed_plan_expected_fill_price_at_api_boundary(self) -> None:
        valid_plan = plan("FIRST")

        for expected_fill_price in (D("0"), D("-1"), D("NaN"), D("Infinity"), 101):
            with self.subTest(expected_fill_price=expected_fill_price):
                with self.assertRaisesRegex(ValueError, "expected_fill_price"):
                    execute_entry_plans_ioc(
                        plans=(replace(valid_plan, expected_fill_price=expected_fill_price),),
                        next_day_bars={"FIRST": None},
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )

    def test_rejects_malformed_plan_expected_cash_cost_at_api_boundary(self) -> None:
        valid_plan = plan("FIRST")

        for expected_cash_cost in (D("0"), D("-1"), D("NaN"), D("Infinity"), 1):
            with self.subTest(expected_cash_cost=expected_cash_cost):
                with self.assertRaisesRegex(ValueError, "expected_cash_cost"):
                    execute_entry_plans_ioc(
                        plans=(replace(valid_plan, expected_cash_cost=expected_cash_cost),),
                        next_day_bars={"FIRST": None},
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )

    def test_rejects_malformed_plan_identity_fields_before_missing_bar_cancellation(self) -> None:
        valid_plan = plan("FIRST")

        for field, values in (
            ("symbol", ("", 1, [])),
            ("asset_type", ("", 1, [])),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        execute_entry_plans_ioc(
                            plans=(replace(valid_plan, **{field: value}),),
                            next_day_bars={"FIRST": None},
                            initial_cash=D("10000"),
                            available_cash=D("10000"),
                            costs=COSTS,
                        )

    def test_rejects_invalid_decimal_initial_stop_pct_at_api_boundary(self) -> None:
        valid_plan = plan("FIRST")

        for initial_stop_pct in (D("NaN"), D("Infinity"), D("-Infinity"), D("0"), D("-0.01"), D("1"), D("1.01")):
            with self.subTest(initial_stop_pct=initial_stop_pct):
                with self.assertRaisesRegex(ValueError, "initial_stop_pct"):
                    execute_entry_plans_ioc(
                        plans=(replace(valid_plan, initial_stop_pct=initial_stop_pct),),
                        next_day_bars={"FIRST": None},
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )

    def test_rejects_non_decimal_initial_stop_pct_at_api_boundary(self) -> None:
        valid_plan = plan("FIRST")

        for initial_stop_pct in (0.05, "0.05", None, True):
            with self.subTest(initial_stop_pct=initial_stop_pct):
                with self.assertRaisesRegex(ValueError, "initial_stop_pct"):
                    execute_entry_plans_ioc(
                        plans=(replace(valid_plan, initial_stop_pct=initial_stop_pct),),
                        next_day_bars={"FIRST": None},
                        initial_cash=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                    )

    def test_executes_plan_created_by_create_entry_plans(self) -> None:
        plans = create_entry_plans(
            candidates=(
                EntryCandidate(Candidate("FIRST", True, D("0.20"), D("0.10")), D("100"), "STOCK", date(2026, 2, 2)),
            ),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=1,
            nav=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        result = execute_entry_plans_ioc(
            plans=plans,
            next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "FILLED")
        self.assertGreater(result.positions[0].quantity, 0)

    def test_rejects_duplicate_plan_symbols_and_bar_identity_mismatches(self) -> None:
        first = plan("FIRST")
        with self.assertRaises(ValueError):
            execute_entry_plans_ioc(
                plans=(first, first), next_day_bars={"FIRST": bar("FIRST")},
                initial_cash=D("10000"), available_cash=D("10000"), costs=COSTS,
            )
        with self.assertRaises(ValueError):
            execute_entry_plans_ioc(
                plans=(first,), next_day_bars={"FIRST": bar("OTHER")},
                initial_cash=D("10000"), available_cash=D("10000"), costs=COSTS,
            )
        with self.assertRaises(ValueError):
            execute_entry_plans_ioc(
                plans=(first,), next_day_bars={"FIRST": bar("FIRST", asset_type="ETF")},
                initial_cash=D("10000"), available_cash=D("10000"), costs=COSTS,
            )

    def test_actual_fill_cost_is_authoritative_over_close_time_estimate(self) -> None:
        inconsistent = replace(plan("FIRST"), expected_cash_cost=D("1"))

        result = execute_entry_plans_ioc(
            plans=(inconsistent,), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=D("10000"), available_cash=D("10000"), costs=COSTS,
        )

        self.assertEqual(result.orders[0].status, "FILLED")
        self.assertEqual(result.fills[0].cash_delta, -plan("FIRST").expected_cash_cost)

    def test_creates_initial_stop_from_actual_fill_and_plan_stop_pct(self) -> None:
        entry_plan = plan("FIRST", expected_open="100", stop_pct="0.10")

        result = execute_entry_plans_ioc(
            plans=(entry_plan,), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=D("10000"), available_cash=D("10000"), costs=COSTS,
        )

        self.assertEqual(result.positions[0].entry_price, D("101"))
        self.assertEqual(result.positions[0].initial_stop_price, D("90.90"))


if __name__ == "__main__":
    unittest.main()
