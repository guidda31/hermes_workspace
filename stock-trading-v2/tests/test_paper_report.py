"""Tests for the paper-trading P&L / performance report.

A report is a pure, in-memory function of a sequence of ``PaperSessionResult`` objects.
It computes an equity curve, total return, max drawdown, realized-P&L and cost sums, and
win/loss session counts. No session is actually simulated here: results are hand-built.
No network, no real money.
"""

import unittest
from datetime import date
from decimal import Decimal

from swing_v2.backtest.engine import Fill, Side
from swing_v2.paper.report import PaperReport, build_paper_report
from swing_v2.paper.session import PaperAccount, PaperSessionResult


D = Decimal
ACCOUNT = PaperAccount(cash=D("0"))


def _fill(total_cost):
    tc = D(total_cost)
    return Fill(
        fill_id="fill-1", order_id="order-1", position_id="pos-1", trade_date=date(2026, 1, 1),
        symbol="000660", asset_type="STOCK", side=Side.BUY, quantity=1,
        reference_open=D("100"), raw_slippage_price=D("100"), fill_price=D("100"),
        notional=D("100"), commission=tc, sell_tax=D("0"), fixed_fee=D("0"),
        total_cost=tc, cash_delta=D("-100"),
    )


def _result(day, nav, realized_pnl="0", fills=()):
    return PaperSessionResult(
        trade_date=date(2026, 1, day), account=ACCOUNT, fills=tuple(fills),
        unfilled=(), realized_pnl=D(realized_pnl), nav=D(nav),
    )


class BuildPaperReportTest(unittest.TestCase):
    def test_equity_curve_in_date_order(self):
        report = build_paper_report([_result(1, "1000"), _result(2, "1100"), _result(3, "1050")])
        self.assertEqual(
            report.equity_curve,
            ((date(2026, 1, 1), D("1000")), (date(2026, 1, 2), D("1100")), (date(2026, 1, 3), D("1050"))),
        )

    def test_total_return_and_max_drawdown_rise_then_fall(self):
        # nav: 1000 -> 1200 (peak) -> 900 (trough) -> 1050
        report = build_paper_report(
            [_result(1, "1000"), _result(2, "1200"), _result(3, "900"), _result(4, "1050")]
        )
        self.assertEqual(report.starting_nav, D("1000"))
        self.assertEqual(report.ending_nav, D("1050"))
        self.assertEqual(report.total_return, D("1050") / D("1000") - 1)
        # deepest drawdown at nav 900 vs running peak 1200: 900/1200 - 1 = -0.25
        self.assertEqual(report.max_drawdown, D("900") / D("1200") - 1)
        self.assertLessEqual(report.max_drawdown, D("0"))

    def test_monotonic_rise_has_zero_drawdown(self):
        report = build_paper_report([_result(1, "1000"), _result(2, "1100"), _result(3, "1200")])
        self.assertEqual(report.max_drawdown, D("0"))

    def test_realized_pnl_and_cost_sums(self):
        report = build_paper_report(
            [
                _result(1, "1000", realized_pnl="50", fills=(_fill("3"), _fill("2"))),
                _result(2, "1100", realized_pnl="-20", fills=(_fill("5"),)),
            ]
        )
        self.assertEqual(report.total_realized_pnl, D("30"))
        self.assertEqual(report.total_costs, D("10"))
        self.assertEqual(report.fill_count, 3)
        self.assertEqual(report.session_count, 2)

    def test_winning_and_losing_session_counts(self):
        report = build_paper_report(
            [
                _result(1, "1000", realized_pnl="50"),
                _result(2, "1010", realized_pnl="-20"),
                _result(3, "1010", realized_pnl="0"),
                _result(4, "1030", realized_pnl="15"),
            ]
        )
        self.assertEqual(report.winning_sessions, 2)
        self.assertEqual(report.losing_sessions, 1)

    def test_single_session_report(self):
        report = build_paper_report([_result(1, "1000", realized_pnl="0")])
        self.assertEqual(report.session_count, 1)
        self.assertEqual(report.starting_nav, D("1000"))
        self.assertEqual(report.ending_nav, D("1000"))
        self.assertEqual(report.total_return, D("0"))
        self.assertEqual(report.max_drawdown, D("0"))
        self.assertEqual(report.equity_curve, ((date(2026, 1, 1), D("1000")),))

    def test_unsorted_dates_raise(self):
        with self.assertRaises(ValueError):
            build_paper_report([_result(2, "1000"), _result(1, "1100")])

    def test_duplicate_dates_raise(self):
        with self.assertRaises(ValueError):
            build_paper_report([_result(1, "1000"), _result(1, "1100")])

    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            build_paper_report([])

    def test_wrong_type_raises(self):
        with self.assertRaises(ValueError):
            build_paper_report(["not a session"])

    def test_report_is_frozen(self):
        report = build_paper_report([_result(1, "1000")])
        with self.assertRaises(Exception):
            report.starting_nav = D("2")  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
