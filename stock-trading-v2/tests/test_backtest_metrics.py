"""Tests for the backtest performance-metrics report (build_backtest_metrics)."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from swing_v2.backtest.backtest_engine import BacktestResult, EquityCurvePoint
from swing_v2.backtest.engine import Fill, Order, Position, Side
from swing_v2.backtest.metrics import BacktestMetrics, build_backtest_metrics


def _point(
    trade_date: date,
    nav_close: Decimal,
    *,
    daily_return: Decimal = Decimal("0"),
    peak_nav: Decimal | None = None,
    drawdown: Decimal = Decimal("0"),
    position_count: int = 0,
    stale_mark_count: int = 0,
) -> EquityCurvePoint:
    peak = nav_close if peak_nav is None else peak_nav
    return EquityCurvePoint(
        trade_date=trade_date, cash=nav_close, market_value=Decimal("0"),
        nav_close=nav_close, daily_return=daily_return,
        cumulative_return=nav_close / Decimal("100") - Decimal("1"),
        peak_nav=peak, drawdown=drawdown, gross_exposure=Decimal("0"),
        position_count=position_count, stale_mark_count=stale_mark_count,
        new_entry_blocked=False, new_entry_block_reason=None,
    )


def _fill(fill_id: str, *, cash_delta: Decimal, total_cost: Decimal, side: Side) -> Fill:
    return Fill(
        fill_id=fill_id, order_id=f"order-{fill_id}", position_id=f"pos-{fill_id}",
        trade_date=date(2026, 1, 1), symbol="A", asset_type="STOCK", side=side,
        quantity=1, reference_open=Decimal("1"), raw_slippage_price=Decimal("1"),
        fill_price=Decimal("1"), notional=Decimal("1"), commission=Decimal("0"),
        sell_tax=Decimal("0"), fixed_fee=Decimal("0"), total_cost=total_cost,
        cash_delta=cash_delta,
    )


def _position(
    position_id: str, *, entry_fill_id: str, exit_fill_id: str | None,
    status: str, age_sessions: int = 5,
) -> Position:
    return Position(
        position_id=position_id, symbol="A", asset_type="STOCK",
        entry_order_id="oe", entry_fill_id=entry_fill_id, entry_price=Decimal("1"),
        initial_stop_price=Decimal("0.9"), quantity=1, exit_order_id=None,
        exit_fill_id=exit_fill_id, exit_price=None, exit_reason=None,
        status=status, age_sessions=age_sessions,
    )


def _order(order_id: str, status: str) -> Order:
    return Order(
        order_id=order_id, signal_id="s", position_id=None, symbol="A",
        asset_type="STOCK", side=Side.BUY, signal_date=date(2026, 1, 1),
        scheduled_trade_date=None, status=status, intent_reason="ENTRY_SIGNAL",
        requested_quantity=1, filled_quantity=0, unfilled_quantity=0,
        unfilled_reason=None,
    )


def _result(
    *, equity_curve: tuple[EquityCurvePoint, ...],
    fills: tuple[Fill, ...] = (), positions: tuple[Position, ...] = (),
    orders: tuple[Order, ...] = (),
) -> BacktestResult:
    return BacktestResult(
        all_day_results=(), equity_curve=equity_curve, orders=orders,
        fills=fills, positions=positions, signals=(), universe_exclusions=(),
    )


class TestReturnsAndDrawdown(unittest.TestCase):
    def test_total_return_and_max_drawdown_rise_then_fall(self) -> None:
        curve = (
            _point(date(2026, 1, 2), Decimal("100"), daily_return=Decimal("0")),
            _point(date(2026, 1, 3), Decimal("110"), daily_return=Decimal("0.1")),
            _point(date(2026, 1, 4), Decimal("121"), daily_return=Decimal("0.1")),
            _point(date(2026, 1, 5), Decimal("108.9"), daily_return=Decimal("-0.1"),
                   peak_nav=Decimal("121"), drawdown=Decimal("-0.1")),
        )
        m = build_backtest_metrics(_result(equity_curve=curve), initial_cash=Decimal("100"))
        self.assertIsInstance(m, BacktestMetrics)
        self.assertEqual(m.starting_nav, Decimal("100"))
        self.assertEqual(m.ending_nav, Decimal("108.9"))
        self.assertEqual(m.total_return, Decimal("0.089"))
        self.assertEqual(m.max_drawdown, Decimal("-0.1"))
        self.assertLessEqual(m.max_drawdown, Decimal("0"))
        self.assertEqual(m.max_drawdown_peak_date, date(2026, 1, 4))
        self.assertEqual(m.max_drawdown_trough_date, date(2026, 1, 5))

    def test_cagr_with_short_annualization(self) -> None:
        curve = (
            _point(date(2026, 1, 2), Decimal("100")),
            _point(date(2026, 1, 3), Decimal("110")),
            _point(date(2026, 1, 4), Decimal("121")),
            _point(date(2026, 1, 5), Decimal("108.9")),
        )
        m = build_backtest_metrics(
            _result(equity_curve=curve), initial_cash=Decimal("100"), annualization_days=2,
        )
        # (108.9/100) ** (2/4) - 1 = sqrt(1.089) - 1 ~= 0.043552
        self.assertIsNotNone(m.cagr)
        assert m.cagr is not None
        self.assertLess(abs(m.cagr - Decimal("0.043552")), Decimal("0.0005"))

    def test_cagr_none_when_span_shorter_than_annualization(self) -> None:
        curve = (
            _point(date(2026, 1, 2), Decimal("100")),
            _point(date(2026, 1, 3), Decimal("110")),
        )
        m = build_backtest_metrics(_result(equity_curve=curve), initial_cash=Decimal("100"))
        self.assertIsNone(m.cagr)


class TestTradeStatistics(unittest.TestCase):
    def test_win_rate_and_profit_factor(self) -> None:
        fills = (
            _fill("eA", cash_delta=Decimal("-1000"), total_cost=Decimal("5"), side=Side.BUY),
            _fill("xA", cash_delta=Decimal("1200"), total_cost=Decimal("5"), side=Side.SELL),
            _fill("eB", cash_delta=Decimal("-500"), total_cost=Decimal("3"), side=Side.BUY),
            _fill("xB", cash_delta=Decimal("600"), total_cost=Decimal("3"), side=Side.SELL),
            _fill("eC", cash_delta=Decimal("-800"), total_cost=Decimal("4"), side=Side.BUY),
            _fill("xC", cash_delta=Decimal("700"), total_cost=Decimal("4"), side=Side.SELL),
        )
        positions = (
            _position("A", entry_fill_id="eA", exit_fill_id="xA", status="CLOSED", age_sessions=4),
            _position("B", entry_fill_id="eB", exit_fill_id="xB", status="CLOSED", age_sessions=6),
            _position("C", entry_fill_id="eC", exit_fill_id="xC", status="CLOSED", age_sessions=8),
        )
        curve = (_point(date(2026, 1, 2), Decimal("100")),)
        m = build_backtest_metrics(
            _result(equity_curve=curve, fills=fills, positions=positions),
            initial_cash=Decimal("100"),
        )
        self.assertEqual(m.closed_round_trips, 3)
        self.assertEqual(m.win_rate, Decimal("2") / Decimal("3"))
        self.assertEqual(m.average_win, Decimal("150"))
        self.assertEqual(m.average_loss, Decimal("-100"))
        self.assertEqual(m.profit_factor, Decimal("3"))
        self.assertEqual(m.average_holding_sessions, Decimal("6"))
        self.assertEqual(m.total_fills, 6)

    def test_profit_factor_none_when_no_loss(self) -> None:
        fills = (
            _fill("eA", cash_delta=Decimal("-1000"), total_cost=Decimal("0"), side=Side.BUY),
            _fill("xA", cash_delta=Decimal("1200"), total_cost=Decimal("0"), side=Side.SELL),
            _fill("eB", cash_delta=Decimal("-500"), total_cost=Decimal("0"), side=Side.BUY),
            _fill("xB", cash_delta=Decimal("600"), total_cost=Decimal("0"), side=Side.SELL),
        )
        positions = (
            _position("A", entry_fill_id="eA", exit_fill_id="xA", status="CLOSED"),
            _position("B", entry_fill_id="eB", exit_fill_id="xB", status="CLOSED"),
        )
        curve = (_point(date(2026, 1, 2), Decimal("100")),)
        m = build_backtest_metrics(
            _result(equity_curve=curve, fills=fills, positions=positions),
            initial_cash=Decimal("100"),
        )
        self.assertIsNone(m.profit_factor)
        self.assertEqual(m.win_rate, Decimal("1"))

    def test_order_counts(self) -> None:
        orders = (
            _order("o1", "FILLED"), _order("o2", "FILLED"),
            _order("o3", "CANCELED_UNFILLED"), _order("o4", "CANCELED_CASH"),
        )
        curve = (_point(date(2026, 1, 2), Decimal("100")),)
        m = build_backtest_metrics(
            _result(equity_curve=curve, orders=orders), initial_cash=Decimal("100"),
        )
        self.assertEqual(m.filled_orders, 2)
        self.assertEqual(m.canceled_orders, 2)


class TestVolatilityAndExposure(unittest.TestCase):
    def test_sharpe_none_on_single_point(self) -> None:
        curve = (_point(date(2026, 1, 2), Decimal("100")),)
        m = build_backtest_metrics(_result(equity_curve=curve), initial_cash=Decimal("100"))
        self.assertIsNone(m.annualized_sharpe)
        self.assertEqual(m.daily_volatility, Decimal("0"))

    def test_position_counts_and_stale_days(self) -> None:
        curve = (
            _point(date(2026, 1, 2), Decimal("100"), position_count=1, stale_mark_count=0),
            _point(date(2026, 1, 3), Decimal("100"), position_count=3, stale_mark_count=2),
            _point(date(2026, 1, 4), Decimal("100"), position_count=2, stale_mark_count=0),
        )
        m = build_backtest_metrics(_result(equity_curve=curve), initial_cash=Decimal("100"))
        self.assertEqual(m.max_concurrent_positions, 3)
        self.assertEqual(m.average_position_count, Decimal("2"))
        self.assertEqual(m.stale_mark_days, 1)


class TestCostsAndValidation(unittest.TestCase):
    def test_total_costs_sum(self) -> None:
        fills = (
            _fill("f1", cash_delta=Decimal("-1000"), total_cost=Decimal("5"), side=Side.BUY),
            _fill("f2", cash_delta=Decimal("1200"), total_cost=Decimal("7"), side=Side.SELL),
            _fill("f3", cash_delta=Decimal("-500"), total_cost=Decimal("3"), side=Side.BUY),
        )
        curve = (_point(date(2026, 1, 2), Decimal("100")),)
        m = build_backtest_metrics(
            _result(equity_curve=curve, fills=fills), initial_cash=Decimal("100"),
        )
        self.assertEqual(m.total_costs, Decimal("15"))

    def test_empty_equity_curve_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_backtest_metrics(_result(equity_curve=()), initial_cash=Decimal("100"))

    def test_bad_initial_cash_raises(self) -> None:
        curve = (_point(date(2026, 1, 2), Decimal("100")),)
        with self.assertRaises(ValueError):
            build_backtest_metrics(_result(equity_curve=curve), initial_cash=Decimal("0"))


if __name__ == "__main__":
    unittest.main()
