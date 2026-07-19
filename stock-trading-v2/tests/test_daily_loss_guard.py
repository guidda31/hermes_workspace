import unittest
from decimal import Decimal
from typing import cast

from swing_v2.backtest import (
    DailyLossGuardConfig,
    DailyLossGuardInput,
    evaluate_daily_loss_guard,
)


D = Decimal


class DailyLossGuardTests(unittest.TestCase):
    def test_blocks_entries_at_exact_loss_limit_and_reports_calculated_values(self) -> None:
        result = evaluate_daily_loss_guard(
            DailyLossGuardInput(
                day_start_equity=D("100000"),
                realized_pnl=D("-1000"),
                unrealized_pnl=D("-2000"),
            ),
            DailyLossGuardConfig(max_daily_loss_pct=D("0.03")),
        )

        self.assertFalse(result.entries_allowed)
        self.assertEqual(result.daily_pnl, D("-3000"))
        self.assertEqual(result.daily_return, D("-0.03"))
        self.assertEqual(result.reason, "daily loss limit reached")

    def test_allows_returns_above_the_loss_limit_including_profit(self) -> None:
        config = DailyLossGuardConfig(max_daily_loss_pct=D("0.03"))
        for realized_pnl, unrealized_pnl in ((D("-2999"), D("0")), (D("1000"), D("2000"))):
            with self.subTest(realized_pnl=realized_pnl, unrealized_pnl=unrealized_pnl):
                result = evaluate_daily_loss_guard(
                    DailyLossGuardInput(
                        day_start_equity=D("100000"),
                        realized_pnl=realized_pnl,
                        unrealized_pnl=unrealized_pnl,
                    ),
                    config,
                )

                self.assertTrue(result.entries_allowed)
                self.assertIsNone(result.reason)

    def test_rejects_non_finite_or_non_positive_day_start_equity(self) -> None:
        for value in (D("NaN"), D("Infinity"), D("-Infinity"), D("0"), D("-1"), 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    DailyLossGuardInput(
                        day_start_equity=value,
                        realized_pnl=D("0"),
                        unrealized_pnl=D("0"),
                    )

    def test_rejects_non_finite_realized_or_unrealized_pnl(self) -> None:
        for field_name, value in (
            ("realized_pnl", D("NaN")),
            ("realized_pnl", D("Infinity")),
            ("unrealized_pnl", D("-Infinity")),
            ("unrealized_pnl", 0),
        ):
            with self.subTest(field_name=field_name, value=value):
                values = {
                    "day_start_equity": D("100000"),
                    "realized_pnl": D("0"),
                    "unrealized_pnl": D("0"),
                }
                values[field_name] = cast(Decimal, value)
                with self.assertRaises(ValueError):
                    DailyLossGuardInput(**values)

    def test_rejects_invalid_max_daily_loss_percentage(self) -> None:
        for value in (D("NaN"), D("Infinity"), D("-Infinity"), D("0"), D("-0.01"), D("1"), 0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    DailyLossGuardConfig(max_daily_loss_pct=value)


if __name__ == "__main__":
    unittest.main()
