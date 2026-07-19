import unittest
from dataclasses import replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from swing_v2.backtest import ExecutionCostConfig, Side, calculate_entry_quantity


D = Decimal


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


class EntryQuantityTests(unittest.TestCase):
    def test_uses_slipped_and_tick_rounded_fill_price_for_risk_boundary(self) -> None:
        quantity = calculate_entry_quantity(
            expected_open_price=D("100"),
            nav=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
            asset_type="STOCK",
            risk_per_position=D("0.01"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        # Buy fill is ceil(100 * 1.001) = 101; 1% risk / (101 - 95.95) = 19.80...
        self.assertEqual(quantity, 19)

    def test_rejects_non_positive_or_non_finite_expected_open_price(self) -> None:
        for expected_open_price in (D("0"), D("-1"), D("NaN"), D("Infinity"), D("-Infinity")):
            with self.subTest(expected_open_price=expected_open_price):
                with self.assertRaises(ValueError):
                    calculate_entry_quantity(
                        expected_open_price=expected_open_price,
                        nav=D("10000"),
                        available_cash=D("10000"),
                        costs=COSTS,
                        asset_type="STOCK",
                        risk_per_position=D("0.01"),
                        max_position_notional_pct=D("0.20"),
                        initial_stop_pct=D("0.05"),
                    )

    def test_caps_quantity_at_maximum_position_notional(self) -> None:
        quantity = calculate_entry_quantity(
            expected_open_price=D("100"),
            nav=D("10000"),
            available_cash=D("10000"),
            costs=COSTS,
            asset_type="STOCK",
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        self.assertEqual(quantity, 19)

    def test_caps_quantity_at_cash_after_commission_and_fixed_fee(self) -> None:
        quantity = calculate_entry_quantity(
            expected_open_price=D("100"),
            nav=D("10000"),
            available_cash=D("320"),
            costs=COSTS,
            asset_type="STOCK",
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        # 3 shares cost 3 * (101 + 0.1515) + 7 = 310.4545; 4 shares exceed cash.
        self.assertEqual(quantity, 3)

    def test_returns_zero_when_cash_cannot_cover_one_share_and_fixed_fee(self) -> None:
        quantity = calculate_entry_quantity(
            expected_open_price=D("100"),
            nav=D("10000"),
            available_cash=D("6"),
            costs=COSTS,
            asset_type="STOCK",
            risk_per_position=D("0.20"),
            max_position_notional_pct=D("0.20"),
            initial_stop_pct=D("0.05"),
        )

        # One share costs 101 + 0.1515 + 7 = 108.1515.
        self.assertEqual(quantity, 0)

    def test_rejects_negative_or_non_finite_nav_and_available_cash(self) -> None:
        for argument in ("nav", "available_cash"):
            for value in (D("-1"), D("NaN"), D("Infinity"), D("-Infinity")):
                with self.subTest(argument=argument, value=value):
                    with self.assertRaises(ValueError):
                        calculate_entry_quantity(
                            expected_open_price=D("100"), nav=value if argument == "nav" else D("10000"),
                            available_cash=value if argument == "available_cash" else D("10000"),
                            costs=COSTS, asset_type="STOCK", risk_per_position=D("0.01"),
                            max_position_notional_pct=D("0.20"), initial_stop_pct=D("0.05"),
                        )

    def test_rejects_out_of_range_or_non_finite_risk_and_notional_limits(self) -> None:
        for argument in ("risk_per_position", "max_position_notional_pct"):
            for value in (D("-0.01"), D("1.01"), D("NaN"), D("Infinity"), D("-Infinity")):
                with self.subTest(argument=argument, value=value):
                    with self.assertRaises(ValueError):
                        calculate_entry_quantity(
                            expected_open_price=D("100"), nav=D("10000"), available_cash=D("10000"),
                            costs=COSTS, asset_type="STOCK",
                            risk_per_position=value if argument == "risk_per_position" else D("0.01"),
                            max_position_notional_pct=(
                                value if argument == "max_position_notional_pct" else D("0.20")
                            ),
                            initial_stop_pct=D("0.05"),
                        )

    def test_rejects_invalid_initial_stop_pct(self) -> None:
        for initial_stop_pct in (D("-0.01"), D("0"), D("1"), D("1.01"), D("NaN"), D("Infinity"), D("-Infinity")):
            with self.subTest(initial_stop_pct=initial_stop_pct):
                with self.assertRaises(ValueError):
                    calculate_entry_quantity(
                        expected_open_price=D("100"), nav=D("10000"), available_cash=D("10000"),
                        costs=COSTS, asset_type="STOCK", risk_per_position=D("0.01"),
                        max_position_notional_pct=D("0.20"), initial_stop_pct=initial_stop_pct,
                    )

    def test_rejects_negative_or_non_finite_buy_costs(self) -> None:
        for argument in ("buy_slippage_bps", "buy_commission_bps", "fixed_fee_per_order"):
            for value in (D("-1"), D("NaN"), D("Infinity"), D("-Infinity")):
                with self.subTest(argument=argument, value=value):
                    with self.assertRaises(ValueError):
                        calculate_entry_quantity(
                            expected_open_price=D("100"), nav=D("10000"), available_cash=D("10000"),
                            costs=replace(COSTS, **{argument: value}), asset_type="STOCK",
                            risk_per_position=D("0.01"), max_position_notional_pct=D("0.20"),
                            initial_stop_pct=D("0.05"),
                        )

    def test_rejects_non_positive_or_non_finite_tick_rounded_fill_price(self) -> None:
        for tick_rounder in (
            lambda price, side: D("0"), lambda price, side: D("-1"),
            lambda price, side: D("NaN"), lambda price, side: D("Infinity"),
        ):
            with self.subTest(tick_rounder=tick_rounder):
                with self.assertRaises(ValueError):
                    calculate_entry_quantity(
                        expected_open_price=D("100"), nav=D("10000"), available_cash=D("10000"),
                        costs=replace(COSTS, tick_rounder=tick_rounder), asset_type="STOCK",
                        risk_per_position=D("0.01"), max_position_notional_pct=D("0.20"),
                        initial_stop_pct=D("0.05"),
                    )

    def test_returns_zero_for_zero_nav_cash_risk_or_notional_limit(self) -> None:
        for argument in ("nav", "available_cash", "risk_per_position", "max_position_notional_pct"):
            with self.subTest(argument=argument):
                quantity = calculate_entry_quantity(
                    expected_open_price=D("100"), nav=D("0") if argument == "nav" else D("10000"),
                    available_cash=D("0") if argument == "available_cash" else D("10000"),
                    costs=COSTS, asset_type="STOCK",
                    risk_per_position=D("0") if argument == "risk_per_position" else D("0.01"),
                    max_position_notional_pct=(
                        D("0") if argument == "max_position_notional_pct" else D("0.20")
                    ),
                    initial_stop_pct=D("0.05"),
                )
                self.assertEqual(quantity, 0)
