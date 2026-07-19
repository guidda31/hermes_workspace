import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from swing_v2.backtest import (
    Candidate,
    DailyLossGuardConfig,
    DailyLossGuardInput,
    EntryCandidate,
    ExecutionCostConfig,
    Side,
    create_entry_plans,
)


D = Decimal


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


def candidate(symbol: str, breakout: str) -> Candidate:
    return Candidate(symbol, True, D(breakout), D("0.10"))


class EntryPlanningTests(unittest.TestCase):
    def test_create_entry_plans_preserves_the_plain_signal_date(self) -> None:
        signal_date = date(2026, 2, 2)

        plans = create_entry_plans(
            candidates=(
                EntryCandidate(
                    candidate("ELIGIBLE", "0.20"), D("100"), "STOCK", signal_date
                ),
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

        self.assertEqual(plans[0].signal_date, signal_date)

    def test_entry_candidate_rejects_datetime_and_non_date_signal_dates(self) -> None:
        for signal_date in (datetime(2026, 2, 2, 15), "2026-02-02", None):
            with self.subTest(signal_date=signal_date):
                with self.assertRaisesRegex(ValueError, "signal_date"):
                    EntryCandidate(candidate("AAA", "0.10"), D("100"), "STOCK", signal_date)

    def test_daily_loss_guard_blocks_all_new_entry_plans(self) -> None:
        plans = create_entry_plans(
            candidates=(EntryCandidate(candidate("ELIGIBLE", "0.20"), D("100"), "STOCK", date(2026, 2, 2)),),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=1,
            nav=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
            daily_loss_guard_input=DailyLossGuardInput(
                day_start_equity=D("100000"),
                realized_pnl=D("-3000"),
                unrealized_pnl=D("0"),
            ),
            daily_loss_guard_config=DailyLossGuardConfig(max_daily_loss_pct=D("0.03")),
        )

        self.assertEqual(plans, ())

    def test_daily_loss_guard_allows_entry_planning_above_the_loss_limit(self) -> None:
        plans = create_entry_plans(
            candidates=(EntryCandidate(candidate("ELIGIBLE", "0.20"), D("100"), "STOCK", date(2026, 2, 2)),),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=1,
            nav=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
            daily_loss_guard_input=DailyLossGuardInput(
                day_start_equity=D("100000"),
                realized_pnl=D("-2999"),
                unrealized_pnl=D("0"),
            ),
            daily_loss_guard_config=DailyLossGuardConfig(max_daily_loss_pct=D("0.03")),
        )

        self.assertEqual(tuple(plan.symbol for plan in plans), ("ELIGIBLE",))

    def test_daily_loss_guard_input_and_config_must_be_provided_together(self) -> None:
        kwargs = dict(
            candidates=(),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=0,
            nav=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )
        with self.assertRaises(ValueError):
            create_entry_plans(
                **kwargs,
                daily_loss_guard_input=DailyLossGuardInput(D("100000"), D("0"), D("0")),
            )
        with self.assertRaises(ValueError):
            create_entry_plans(
                **kwargs,
                daily_loss_guard_config=DailyLossGuardConfig(D("0.03")),
            )

    def test_validates_sizing_contract_before_empty_slot_or_excluded_candidate_exit(self) -> None:
        invalid_inputs = (
            ("nav", D("NaN")),
            ("nav", D("-1")),
            ("available_cash", D("NaN")),
            ("available_cash", D("-1")),
            ("risk_per_position", D("NaN")),
            ("risk_per_position", D("-0.01")),
            ("risk_per_position", D("1.01")),
            ("max_position_notional_pct", D("NaN")),
            ("max_position_notional_pct", D("-0.01")),
            ("max_position_notional_pct", D("1.01")),
            ("initial_stop_pct", D("NaN")),
            ("initial_stop_pct", D("0")),
            ("costs", replace(COSTS, buy_slippage_bps=D("NaN"))),
            ("costs", replace(COSTS, buy_commission_bps=D("Infinity"))),
            ("costs", replace(COSTS, fixed_fee_per_order=D("-1"))),
        )
        eligible = EntryCandidate(candidate("ELIGIBLE", "0.20"), D("100"), "STOCK", date(2026, 2, 2))
        early_exit_contexts = (
            ((), set(), set(), 1),
            ((eligible,), set(), set(), 0),
            ((eligible,), {"ELIGIBLE"}, set(), 1),
        )

        for candidates, active_symbols, pending_entry_symbols, max_positions in early_exit_contexts:
            for argument, value in invalid_inputs:
                with self.subTest(
                    candidates=candidates,
                    max_positions=max_positions,
                    argument=argument,
                    value=value,
                ):
                    kwargs = dict(
                        candidates=candidates,
                        active_symbols=active_symbols,
                        pending_entry_symbols=pending_entry_symbols,
                        max_positions=max_positions,
                        nav=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                        risk_per_position=D("0.20"),
                        max_position_notional_pct=D("0.20"),
                        initial_stop_pct=D("0.05"),
                    )
                    kwargs[argument] = value
                    with self.assertRaises(ValueError):
                        create_entry_plans(**kwargs)

    def test_returns_no_plans_for_valid_empty_slot_or_excluded_candidate_inputs(self) -> None:
        eligible = EntryCandidate(candidate("ELIGIBLE", "0.20"), D("100"), "STOCK", date(2026, 2, 2))
        for candidates, active_symbols, pending_entry_symbols, max_positions in (
            ((), set(), set(), 1),
            ((eligible,), set(), set(), 0),
            ((eligible,), {"ELIGIBLE"}, set(), 1),
        ):
            with self.subTest(candidates=candidates, max_positions=max_positions):
                self.assertEqual(
                    create_entry_plans(
                        candidates=candidates,
                        active_symbols=active_symbols,
                        pending_entry_symbols=pending_entry_symbols,
                        max_positions=max_positions,
                        nav=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                        risk_per_position=D("0.20"),
                        max_position_notional_pct=D("0.20"),
                        initial_stop_pct=D("0.05"),
                    ),
                    (),
                )

    def test_rejects_non_finite_fill_returned_while_building_a_plan(self) -> None:
        fill_prices = iter((D("NaN"),))
        costs = replace(COSTS, tick_rounder=lambda price, side: next(fill_prices))

        with self.assertRaises(ValueError):
            create_entry_plans(
                candidates=(EntryCandidate(candidate("ELIGIBLE", "0.20"), D("100"), "STOCK", date(2026, 2, 2)),),
                active_symbols=set(),
                pending_entry_symbols=set(),
                max_positions=1,
                nav=D("10000"),
                available_cash=D("10000"),
                costs=costs,
                risk_per_position=D("0.20"),
                max_position_notional_pct=D("0.20"),
                initial_stop_pct=D("0.05"),
            )

    def test_uses_one_tick_rounded_fill_for_sizing_and_planned_cash_cost(self) -> None:
        tick_rounder_calls = 0

        def stateful_tick_rounder(price: Decimal, side: Side) -> Decimal:
            nonlocal tick_rounder_calls
            tick_rounder_calls += 1
            return D("100") if tick_rounder_calls == 1 else D("200")

        plans = create_entry_plans(
            candidates=(EntryCandidate(candidate("ELIGIBLE", "0.20"), D("100"), "STOCK", date(2026, 2, 2)),),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=1,
            nav=D("10000"),
            available_cash=D("150"),
            costs=replace(COSTS, tick_rounder=stateful_tick_rounder),
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        self.assertEqual(tick_rounder_calls, 1)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].quantity, 1)
        self.assertEqual(plans[0].expected_fill_price, D("100"))
        self.assertEqual(plans[0].expected_cash_cost, D("107.15"))
        self.assertLessEqual(plans[0].expected_cash_cost, D("150"))

    def test_skips_zero_quantity_candidate_and_backfills_slot_with_next_ranked_candidate(self) -> None:
        plans = create_entry_plans(
            candidates=(
                EntryCandidate(candidate("TOO_EXPENSIVE", "0.30"), D("10000"), "STOCK", date(2026, 2, 2)),
                EntryCandidate(candidate("AFFORDABLE", "0.20"), D("100"), "STOCK", date(2026, 2, 2)),
            ),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=1,
            nav=D("1000"),
            available_cash=D("1000"),
            costs=COSTS,
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        self.assertEqual(tuple(plan.symbol for plan in plans), ("AFFORDABLE",))
        self.assertEqual(plans[0].quantity, 1)
        self.assertEqual(plans[0].expected_open_price, D("100"))
        self.assertEqual(plans[0].asset_type, "STOCK")
        self.assertEqual(plans[0].nav, D("1000"))
        self.assertEqual(plans[0].risk_per_position, D("0.20"))
        self.assertEqual(plans[0].max_position_notional_pct, D("0.20"))
        self.assertEqual(plans[0].initial_stop_pct, D("0.05"))

    def test_reserves_actual_cash_cost_before_sizing_the_next_plan(self) -> None:
        plans = create_entry_plans(
            candidates=(
                EntryCandidate(candidate("FIRST", "0.30"), D("1000"), "STOCK", date(2026, 2, 2)),
                EntryCandidate(candidate("SECOND", "0.20"), D("100"), "STOCK", date(2026, 2, 2)),
            ),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=2,
            nav=D("10000"),
            available_cash=D("2500"),
            costs=COSTS,
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        self.assertEqual(tuple(plan.quantity for plan in plans), (1, 14))
        self.assertEqual(plans[0].expected_cash_cost, D("1009.5015"))
        self.assertEqual(plans[1].expected_cash_cost, D("1423.121"))
        self.assertLessEqual(sum(plan.expected_cash_cost for plan in plans), D("2500"))

    def test_preserves_rank_order_and_slot_cap(self) -> None:
        plans = create_entry_plans(
            candidates=(
                EntryCandidate(candidate("THIRD", "0.10"), D("100"), "STOCK", date(2026, 2, 2)),
                EntryCandidate(candidate("FIRST", "0.30"), D("100"), "STOCK", date(2026, 2, 2)),
                EntryCandidate(candidate("SECOND", "0.20"), D("100"), "STOCK", date(2026, 2, 2)),
            ),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=2,
            nav=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        self.assertEqual(tuple(plan.symbol for plan in plans), ("FIRST", "SECOND"))

    def test_excludes_active_and_pending_symbols_before_planning(self) -> None:
        plans = create_entry_plans(
            candidates=(
                EntryCandidate(candidate("HELD", "0.40"), D("100"), "STOCK", date(2026, 2, 2)),
                EntryCandidate(candidate("PENDING", "0.30"), D("100"), "STOCK", date(2026, 2, 2)),
                EntryCandidate(candidate("ELIGIBLE", "0.20"), D("100"), "STOCK", date(2026, 2, 2)),
            ),
            active_symbols={"HELD"},
            pending_entry_symbols={"PENDING"},
            max_positions=5,
            nav=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        self.assertEqual(tuple(plan.symbol for plan in plans), ("ELIGIBLE",))

    def test_rejects_invalid_expected_open_price_and_asset_type(self) -> None:
        for expected_open_price, asset_type in (
            (D("0"), "STOCK"),
            (D("NaN"), "STOCK"),
            (D("100"), ""),
            (D("100"), None),
        ):
            with self.subTest(expected_open_price=expected_open_price, asset_type=asset_type):
                with self.assertRaises(ValueError):
                    EntryCandidate(candidate("AAA", "0.10"), expected_open_price, asset_type, date(2026, 2, 2))


if __name__ == "__main__":
    unittest.main()
