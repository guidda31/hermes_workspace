import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from swing_v2.backtest import (
    EntryPlan,
    ExecutionCostConfig,
    ExitIntent,
    Fill,
    Order,
    PortfolioState,
    Position,
    RunResult,
    Side,
    apply_entry_execution,
    apply_exit_execution,
    create_portfolio_state,
    execute_entry_plans_ioc as _execute_entry_plans_ioc,
    execute_exit_intents_ioc as _execute_exit_intents_ioc,
)
from swing_v2.contracts import DailyBar


D = Decimal


def execute_entry_plans_ioc(**kwargs: Any):
    """Inject the fixture's known next market session unless a test overrides it."""
    kwargs.setdefault("execution_id", "portfolio-entry-test-execution")
    kwargs.setdefault(
        "scheduled_trade_dates_by_symbol",
        {
            plan.symbol: date(2026, 1, 5)
            for plan in kwargs["plans"]
            if isinstance(plan.symbol, str)
        },
    )
    return _execute_entry_plans_ioc(**kwargs)


def execute_exit_intents_ioc(**kwargs: Any):
    """Inject an execution namespace unless a test exercises a specific one."""
    kwargs.setdefault("execution_id", "portfolio-exit-test-execution")
    return _execute_exit_intents_ioc(**kwargs)


def round_to_won(price: Decimal, side: Side) -> Decimal:
    return price.quantize(D("1"), rounding=ROUND_CEILING if side is Side.BUY else ROUND_FLOOR)


COSTS = ExecutionCostConfig(
    buy_slippage_bps=D("10"), sell_slippage_bps=D("10"),
    buy_commission_bps=D("15"), sell_commission_bps=D("15"),
    sell_tax_bps_by_asset_type={"STOCK": D("20")}, fixed_fee_per_order=D("7"),
    tick_rounder=round_to_won,
)


def plan(symbol: str, quantity: int = 10) -> EntryPlan:
    fill_price = round_to_won(D("100") * D("1.001"), Side.BUY)
    cost = fill_price * quantity
    cost += cost * COSTS.buy_commission_bps / D("10000") + COSTS.fixed_fee_per_order
    return EntryPlan(
        symbol=symbol, asset_type="STOCK", expected_open_price=D("100"), quantity=quantity,
        signal_date=date(2026, 1, 4),
        expected_fill_price=fill_price, expected_cash_cost=cost, nav=D("10000"),
        risk_per_position=D("0.01"), max_position_notional_pct=D("0.20"),
        initial_stop_pct=D("0.05"), costs=COSTS,
    )


def bar(symbol: str, open_price: str = "100", trade_date: date = date(2026, 1, 5)) -> DailyBar:
    opened = D(open_price)
    return DailyBar(
        trade_date=trade_date, symbol=symbol, asset_type="STOCK", open=opened,
        high=opened + D("1"), low=opened - D("1"), close=opened, volume=100,
        trading_value=D("10000"), is_tradable=True,
    )


class PortfolioStateTests(unittest.TestCase):
    def test_distinct_entry_execution_ids_append_to_state_and_reused_namespace_is_rejected(self) -> None:
        initial = create_portfolio_state(D("10000"))
        first = execute_entry_plans_ioc(
            execution_id="entries-2026-01-05-a",
            plans=(plan("FIRST"),),
            next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=initial.cash,
            available_cash=initial.cash,
            costs=COSTS,
        )
        after_first = apply_entry_execution(initial, first)
        second = execute_entry_plans_ioc(
            execution_id="entries-2026-01-05-b",
            plans=(plan("SECOND"),),
            next_day_bars={"SECOND": bar("SECOND")},
            initial_cash=after_first.cash,
            available_cash=after_first.cash,
            costs=COSTS,
        )
        after_second = apply_entry_execution(after_first, second)

        self.assertEqual(tuple(position.symbol for position in after_second.positions), ("FIRST", "SECOND"))
        self.assertEqual(len({order.order_id for order in after_second.orders}), 2)
        self.assertEqual(len({fill.fill_id for fill in after_second.fills}), 2)
        self.assertEqual(len({position.position_id for position in after_second.positions}), 2)

        reused_namespace = execute_entry_plans_ioc(
            execution_id="entries-2026-01-05-b",
            plans=(plan("THIRD"),),
            next_day_bars={"THIRD": bar("THIRD")},
            initial_cash=after_second.cash,
            available_cash=after_second.cash,
            costs=COSTS,
        )
        with self.assertRaisesRegex(ValueError, "order_id"):
            apply_entry_execution(after_second, reused_namespace)

    def test_successive_exit_execution_ids_append_unique_ledger_identities(self) -> None:
        initial = create_portfolio_state(D("10000"))
        entries = execute_entry_plans_ioc(
            execution_id="entries-2026-01-05",
            plans=(plan("FIRST"), plan("SECOND")),
            next_day_bars={"FIRST": bar("FIRST"), "SECOND": bar("SECOND")},
            initial_cash=initial.cash,
            available_cash=initial.cash,
            costs=COSTS,
        )
        invested = apply_entry_execution(initial, entries)
        first_exit = execute_exit_intents_ioc(
            execution_id="exits-2026-01-06",
            positions=invested.positions,
            exit_intents=(ExitIntent("FIRST", 10, "STOP_CLOSE", date(2026, 1, 5)),),
            next_day_bars={"FIRST": bar("FIRST", "90", date(2026, 1, 6))},
            scheduled_trade_dates_by_symbol={"FIRST": date(2026, 1, 6)},
            initial_cash=invested.cash,
            costs=COSTS,
        )
        after_first_exit = apply_exit_execution(invested, first_exit)
        reused_namespace = execute_exit_intents_ioc(
            execution_id="exits-2026-01-06",
            positions=after_first_exit.positions,
            exit_intents=(ExitIntent("SECOND", 10, "MAX_HOLD", date(2026, 1, 6)),),
            next_day_bars={"SECOND": bar("SECOND", "90", date(2026, 1, 7))},
            scheduled_trade_dates_by_symbol={"SECOND": date(2026, 1, 7)},
            initial_cash=after_first_exit.cash,
            costs=COSTS,
        )
        with self.assertRaisesRegex(ValueError, "order_id"):
            apply_exit_execution(after_first_exit, reused_namespace)
        second_exit = execute_exit_intents_ioc(
            execution_id="exits-2026-01-07",
            positions=after_first_exit.positions,
            exit_intents=(ExitIntent("SECOND", 10, "MAX_HOLD", date(2026, 1, 6)),),
            next_day_bars={"SECOND": bar("SECOND", "90", date(2026, 1, 7))},
            scheduled_trade_dates_by_symbol={"SECOND": date(2026, 1, 7)},
            initial_cash=after_first_exit.cash,
            costs=COSTS,
        )
        settled = apply_exit_execution(after_first_exit, second_exit)

        self.assertEqual(tuple(position.status for position in settled.positions), ("CLOSED", "CLOSED"))
        self.assertEqual(len({order.order_id for order in settled.orders}), 4)
        self.assertEqual(len({fill.fill_id for fill in settled.fills}), 4)

    def test_create_initial_state_is_immutable_and_validates_cash(self) -> None:
        state = create_portfolio_state(D("1000"))

        self.assertEqual((state.cash, state.positions, state.orders, state.fills), (D("1000"), (), (), ()))
        with self.assertRaises(FrozenInstanceError):
            state.cash = D("1")  # type: ignore[misc]
        for cash in (D("-1"), D("NaN"), D("Infinity"), 1000):
            with self.subTest(cash=cash):
                with self.assertRaises(ValueError):
                    create_portfolio_state(cash)

    def test_entry_appends_filled_positions_and_full_ledger_without_mutating_input(self) -> None:
        initial = create_portfolio_state(D("10000"))
        result = execute_entry_plans_ioc(
            plans=(plan("FIRST"), plan("MISSING")),
            next_day_bars={"FIRST": bar("FIRST"), "MISSING": None},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )

        updated = apply_entry_execution(initial, result)

        self.assertEqual(initial.positions, ())
        self.assertEqual(updated.cash, result.cash)
        self.assertEqual(tuple(position.symbol for position in updated.positions), ("FIRST",))
        self.assertEqual(tuple(order.status for order in updated.orders), ("FILLED", "CANCELED_UNFILLED"))
        self.assertEqual(updated.fills, result.fills)
        self.assertEqual(updated.orders[0].signal_date, date(2026, 1, 4))
        self.assertEqual(updated.orders[0].scheduled_trade_date, date(2026, 1, 5))
        self.assertEqual(updated.fills[0].trade_date, date(2026, 1, 5))

    def test_entry_with_only_cancellations_keeps_positions_unchanged(self) -> None:
        initial = create_portfolio_state(D("10000"))
        result = execute_entry_plans_ioc(
            plans=(plan("MISSING"),), next_day_bars={"MISSING": None},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )

        updated = apply_entry_execution(initial, result)

        self.assertEqual(updated.positions, initial.positions)
        self.assertEqual(updated.cash, initial.cash)
        self.assertEqual(updated.orders, result.orders)

    def test_entry_rejects_cash_mismatch_open_symbol_or_ledger_id_collision(self) -> None:
        initial = create_portfolio_state(D("10000"))
        result = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )
        populated = apply_entry_execution(initial, result)

        with self.assertRaisesRegex(ValueError, "cash"):
            apply_entry_execution(initial, replace(result, cash=result.cash + D("1")))
        with self.assertRaisesRegex(ValueError, "position_id"):
            repeated_result = replace(
                result,
                cash=populated.cash + sum((fill.cash_delta for fill in result.fills), D("0")),
            )
            apply_entry_execution(
                PortfolioState(populated.cash, populated.positions, (), ()),
                repeated_result,
            )
        new_order = replace(result.orders[0], order_id="new-order", position_id="new-position")
        new_fill = replace(
            result.fills[0], fill_id="new-fill", order_id="new-order", position_id="new-position"
        )
        new_position = replace(
            result.positions[0],
            position_id="new-position",
            entry_order_id="new-order",
            entry_fill_id="new-fill",
        )
        same_symbol_result = RunResult(
            cash=populated.cash + new_fill.cash_delta,
            orders=(new_order,), fills=(new_fill,), positions=(new_position,),
        )
        with self.assertRaisesRegex(ValueError, "OPEN symbol"):
            apply_entry_execution(
                PortfolioState(populated.cash, populated.positions, (), ()), same_symbol_result
            )
        with self.assertRaisesRegex(ValueError, "order_id"):
            apply_entry_execution(replace(initial, orders=(result.orders[0],)), result)
        with self.assertRaisesRegex(ValueError, "fill_id"):
            apply_entry_execution(replace(initial, fills=(result.fills[0],)), result)

    def test_state_rejects_duplicate_ids_and_duplicate_open_symbols(self) -> None:
        initial = create_portfolio_state(D("10000"))
        result = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )
        position = result.positions[0]

        invalid_states = (
            {"positions": (position, position)},
            {"positions": (position, replace(position, position_id="another"))},
            {"orders": (result.orders[0], result.orders[0])},
            {"fills": (result.fills[0], result.fills[0])},
        )
        for changes in invalid_states:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    PortfolioState(**{"cash": initial.cash, "positions": (), "orders": (), "fills": (), **changes})

    def test_exit_replaces_complete_position_snapshot_and_appends_ledger(self) -> None:
        initial = create_portfolio_state(D("10000"))
        entry = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )
        invested = apply_entry_execution(initial, entry)
        exit_result = execute_exit_intents_ioc(
            positions=invested.positions,
            exit_intents=(ExitIntent("FIRST", 10, "STOP_CLOSE", date(2026, 1, 5)),),
            next_day_bars={"FIRST": bar("FIRST", "90", date(2026, 1, 6))},
            scheduled_trade_dates_by_symbol={"FIRST": date(2026, 1, 6)},
            initial_cash=invested.cash, costs=COSTS,
        )

        settled = apply_exit_execution(invested, exit_result)

        self.assertEqual(settled.cash, exit_result.cash)
        self.assertEqual(settled.positions[0].status, "CLOSED")
        self.assertEqual(settled.positions[0].position_id, invested.positions[0].position_id)
        self.assertEqual(settled.orders, invested.orders + exit_result.orders)
        self.assertEqual(settled.fills, invested.fills + exit_result.fills)

    def test_exit_rejects_incomplete_new_or_identity_changed_snapshot_and_cash_mismatch(self) -> None:
        state = create_portfolio_state(D("10000"))
        entry = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=state.cash, available_cash=state.cash, costs=COSTS,
        )
        state = apply_entry_execution(state, entry)
        exit_result = execute_exit_intents_ioc(
            positions=state.positions, exit_intents=(), next_day_bars={},
            scheduled_trade_dates_by_symbol={}, initial_cash=state.cash, costs=COSTS,
        )
        position = state.positions[0]

        for invalid in (
            replace(exit_result, positions=()),
            replace(exit_result, positions=(replace(position, position_id="new"),)),
            replace(exit_result, positions=(replace(position, quantity=99),)),
            replace(exit_result, cash=exit_result.cash + D("1")),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    apply_exit_execution(state, invalid)

    def test_exit_rejects_order_and_fill_id_collisions(self) -> None:
        state = create_portfolio_state(D("10000"))
        entry = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=state.cash, available_cash=state.cash, costs=COSTS,
        )
        state = apply_entry_execution(state, entry)
        unchanged_snapshot = RunResult(state.cash, (), (), state.positions)

        with self.assertRaisesRegex(ValueError, "order_id"):
            apply_exit_execution(
                state, replace(unchanged_snapshot, orders=(state.orders[0],))
            )
        colliding_fill_result = replace(
            unchanged_snapshot,
            cash=state.cash + state.fills[0].cash_delta,
            fills=(state.fills[0],),
        )
        with self.assertRaisesRegex(ValueError, "fill_id"):
            apply_exit_execution(state, colliding_fill_result)

    def test_exit_rejects_closed_position_metadata_rewrite(self) -> None:
        state = self._invested_state()
        exit_result = self._filled_exit_result(state)
        closed = exit_result.positions[0]

        with self.assertRaisesRegex(ValueError, "CLOSED"):
            apply_exit_execution(state, replace(
                exit_result,
                positions=(replace(closed, exit_price=closed.exit_price + D("1")),),
            ))

    def test_exit_rejects_unlinked_cash_increasing_fill(self) -> None:
        state = self._invested_state()
        injected_fill = replace(
            state.fills[0], fill_id="injected-fill", order_id="injected-order", cash_delta=D("1")
        )
        malicious = RunResult(
            cash=state.cash + injected_fill.cash_delta,
            orders=(), fills=(injected_fill,), positions=state.positions,
        )

        with self.assertRaisesRegex(ValueError, "fill.*FILLED order"):
            apply_exit_execution(state, malicious)

    def test_exit_rejects_open_position_exit_metadata_rewrite(self) -> None:
        state = self._invested_state()
        injected = replace(
            state.positions[0], exit_order_id="forged-order", exit_fill_id="forged-fill",
            exit_price=D("100"), exit_reason="FORGED",
        )

        with self.assertRaisesRegex(ValueError, "OPEN.*exit metadata"):
            apply_exit_execution(state, RunResult(state.cash, (), (), (injected,)))

    def test_entry_rejects_sell_or_quantity_mismatched_filled_ledger(self) -> None:
        initial = create_portfolio_state(D("10000"))
        result = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )
        order, fill = result.orders[0], result.fills[0]

        invalid_results = (
            replace(result, orders=(replace(order, side=Side.SELL),)),
            replace(result, orders=(replace(order, filled_quantity=order.filled_quantity - 1),)),
            replace(result, fills=(replace(fill, quantity=fill.quantity - 1),)),
            replace(result, fills=(replace(fill, asset_type="OTHER"),)),
            replace(
                result,
                orders=(replace(
                    order, status="CANCELED_UNFILLED", filled_quantity=0,
                    unfilled_quantity=order.requested_quantity,
                ),),
            ),
            replace(result, fills=()),
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    apply_entry_execution(initial, invalid)

    def test_entry_rejects_forged_buy_cash_injection_and_financial_expressions(self) -> None:
        initial = create_portfolio_state(D("10000"))
        result = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )
        fill = result.fills[0]
        forged_fills = (
            replace(fill, cash_delta=D("5000000")),
            replace(fill, fill_price=D("0")),
            replace(fill, notional=fill.notional + D("1")),
            replace(fill, total_cost=fill.total_cost + D("1")),
            replace(fill, commission=D("-1")),
            replace(fill, sell_tax=D("1")),
            replace(fill, fixed_fee=D("-1")),
            replace(fill, reference_open=D("NaN")),
        )

        for forged_fill in forged_fills:
            with self.subTest(forged_fill=forged_fill):
                forged = replace(
                    result,
                    cash=initial.cash + forged_fill.cash_delta,
                    fills=(forged_fill,),
                )
                with self.assertRaises(ValueError):
                    apply_entry_execution(initial, forged)

    def test_exit_rejects_forged_sell_cash_and_cost_expressions(self) -> None:
        state = self._invested_state()
        result = self._filled_exit_result(state)
        fill = result.fills[0]
        forged_fills = (
            replace(fill, cash_delta=fill.cash_delta + D("1")),
            replace(fill, cash_delta=fill.notional + D("1")),
            replace(fill, total_cost=fill.notional),
            replace(fill, sell_tax=D("-1")),
            replace(fill, raw_slippage_price=D("Infinity")),
        )

        for forged_fill in forged_fills:
            with self.subTest(forged_fill=forged_fill):
                forged = replace(
                    result,
                    cash=state.cash + forged_fill.cash_delta,
                    fills=(forged_fill,),
                )
                with self.assertRaises(ValueError):
                    apply_exit_execution(state, forged)

    def test_apply_rejects_invalid_order_and_fill_trade_dates(self) -> None:
        initial = create_portfolio_state(D("10000"))
        entry = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )
        order, fill = entry.orders[0], entry.fills[0]
        invalid_entries = (
            replace(entry, orders=(replace(order, signal_date=datetime(2026, 1, 4, 9)),)),
            replace(entry, orders=(replace(order, scheduled_trade_date=datetime(2026, 1, 5, 9)),)),
            replace(entry, orders=(replace(order, signal_date=date(2026, 1, 5)),)),
            replace(entry, orders=(replace(order, scheduled_trade_date=date.min),)),
            replace(entry, fills=(replace(fill, trade_date=datetime(2026, 1, 5, 9)),)),
            replace(entry, fills=(replace(fill, trade_date=date(2099, 1, 1)),)),
        )

        for invalid in invalid_entries:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    apply_entry_execution(initial, invalid)

    def test_exit_rejects_arbitrary_open_to_closed_transition(self) -> None:
        state = self._invested_state()
        forged_closed = replace(
            state.positions[0], status="CLOSED", exit_order_id="forged-order",
            exit_fill_id="forged-fill", exit_price=D("100"), exit_reason="FORGED",
        )

        with self.assertRaisesRegex(ValueError, "FILLED orders must exactly match CLOSED transitions"):
            apply_exit_execution(state, RunResult(state.cash, (), (), (forged_closed,)))

    def test_exit_rejects_filled_sell_without_matching_close_transition(self) -> None:
        state = self._invested_state()
        exit_result = self._filled_exit_result(state)
        no_close_snapshot = RunResult(
            exit_result.cash, exit_result.orders, exit_result.fills, state.positions,
        )

        with self.assertRaisesRegex(ValueError, "FILLED orders must exactly match CLOSED transitions"):
            apply_exit_execution(state, no_close_snapshot)

    def _invested_state(self) -> PortfolioState:
        initial = create_portfolio_state(D("10000"))
        entry = execute_entry_plans_ioc(
            plans=(plan("FIRST"),), next_day_bars={"FIRST": bar("FIRST")},
            initial_cash=initial.cash, available_cash=initial.cash, costs=COSTS,
        )
        return apply_entry_execution(initial, entry)

    def _filled_exit_result(self, state: PortfolioState) -> RunResult:
        return execute_exit_intents_ioc(
            positions=state.positions,
            exit_intents=(ExitIntent("FIRST", 10, "STOP_CLOSE", date(2026, 1, 5)),),
            next_day_bars={"FIRST": bar("FIRST", "90", date(2026, 1, 6))},
            scheduled_trade_dates_by_symbol={"FIRST": date(2026, 1, 6)},
            initial_cash=state.cash, costs=COSTS,
        )


if __name__ == "__main__":
    unittest.main()
