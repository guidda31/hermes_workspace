import unittest
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from swing_v2.backtest import (
    EntryPlan,
    ExecutionCostConfig,
    ExitIntent,
    PortfolioState,
    Position,
    Side,
    create_portfolio_state,
    run_portfolio_day,
)
from swing_v2.contracts import DailyBar


D = Decimal
DAY_ONE = date(2026, 2, 3)
DAY_TWO = date(2026, 2, 4)


def round_to_won(price: Decimal, side: Side) -> Decimal:
    return price.quantize(D("1"), rounding=ROUND_CEILING if side is Side.BUY else ROUND_FLOOR)


COSTS = ExecutionCostConfig(
    buy_slippage_bps=D("10"), sell_slippage_bps=D("10"),
    buy_commission_bps=D("15"), sell_commission_bps=D("15"),
    sell_tax_bps_by_asset_type={"STOCK": D("20")}, fixed_fee_per_order=D("7"),
    tick_rounder=round_to_won,
)


def bar(symbol: str, trade_date: date, *, opened: str, closed: str | None = None, tradable: bool = True) -> DailyBar:
    opening = D(opened)
    close = D(closed) if closed is not None else opening
    return DailyBar(trade_date, symbol, "STOCK", opening, max(opening, close) + 1,
                    min(opening, close) - 1, close, 100, D("10000"), tradable)


def entry_plan(symbol: str = "NEW") -> EntryPlan:
    return EntryPlan(
        symbol=symbol, asset_type="STOCK", signal_date=date(2026, 2, 2),
        expected_open_price=D("100"), quantity=5, expected_fill_price=D("101"),
        expected_cash_cost=D("512.7575"), nav=D("1000"), risk_per_position=D("0.01"),
        max_position_notional_pct=D("0.2"), initial_stop_pct=D("0.05"), costs=COSTS,
    )


def open_position(symbol: str) -> Position:
    return Position(
        position_id=f"position-{symbol}", symbol=symbol, asset_type="STOCK",
        entry_order_id=f"entry-order-{symbol}", entry_fill_id=f"entry-fill-{symbol}",
        entry_price=D("100"), initial_stop_price=D("90"), quantity=5,
        exit_order_id=None, exit_fill_id=None, exit_price=None, exit_reason=None,
        status="OPEN", age_sessions=1,
    )


class PortfolioDayTests(unittest.TestCase):
    def test_two_day_entry_close_signal_then_exit_close_appends_ledger_and_marks_nav(self) -> None:
        opened = create_portfolio_state(D("1000"))

        first = run_portfolio_day(
            opening_state=opened, trade_date=DAY_ONE,
            pending_entry_plans=(entry_plan(),), pending_exit_intents=(),
            entry_scheduled_trade_dates_by_symbol={"NEW": DAY_ONE},
            exit_scheduled_trade_dates_by_symbol={},
            entry_open_bars_by_symbol={"NEW": bar("NEW", DAY_ONE, opened="100", closed="95")},
            exit_open_bars_by_symbol={}, entry_execution_id="day-one-entry",
            exit_execution_id="day-one-exit", planned_entry_available_cash=D("1000"), costs=COSTS,
            closing_bars_by_symbol={"NEW": bar("NEW", DAY_ONE, opened="100", closed="95")},
            historical_closes_by_symbol={"NEW": (D("100"),)},
        )

        self.assertEqual(first.opening_state, opened)
        self.assertEqual(first.entry_run_result.orders[0].status, "FILLED")
        self.assertEqual(first.closing_state.positions[0].age_sessions, 1)
        self.assertEqual(first.next_pending_exit_intents, (ExitIntent("NEW", 5, "STOP_CLOSE", DAY_ONE),))
        self.assertEqual(first.closing_state.cash, D("487.2425"))
        self.assertEqual(first.valuation.nav, D("962.2425"))
        with self.assertRaises(FrozenInstanceError):
            first.trade_date = DAY_TWO  # type: ignore[misc]

        second = run_portfolio_day(
            opening_state=first.closing_state, trade_date=DAY_TWO,
            pending_entry_plans=(), pending_exit_intents=first.next_pending_exit_intents,
            entry_scheduled_trade_dates_by_symbol={},
            exit_scheduled_trade_dates_by_symbol={"NEW": DAY_TWO},
            entry_open_bars_by_symbol={},
            exit_open_bars_by_symbol={"NEW": bar("NEW", DAY_TWO, opened="90")},
            entry_execution_id="day-two-entry", exit_execution_id="day-two-exit",
            planned_entry_available_cash=D("0"), costs=COSTS,
            closing_bars_by_symbol={}, historical_closes_by_symbol={},
        )

        self.assertEqual(second.exit_run_result.orders[0].status, "FILLED")
        self.assertEqual(second.closing_state.cash, D("923.6850"))
        self.assertEqual(second.valuation.nav, D("923.6850"))
        self.assertEqual(len(second.closing_state.orders), 2)
        self.assertEqual(len(second.closing_state.fills), 2)
        self.assertEqual(second.next_pending_exit_intents, ())

    def test_exit_ioc_cancellation_does_not_block_precreated_entry_fill(self) -> None:
        opening = PortfolioState(D("1000"), (open_position("OLD"),), (), ())

        result = run_portfolio_day(
            opening_state=opening, trade_date=DAY_ONE,
            pending_entry_plans=(entry_plan("NEW"),),
            pending_exit_intents=(ExitIntent("OLD", 5, "STOP_CLOSE", date(2026, 2, 2)),),
            entry_scheduled_trade_dates_by_symbol={"NEW": DAY_ONE},
            exit_scheduled_trade_dates_by_symbol={"OLD": DAY_ONE},
            entry_open_bars_by_symbol={"NEW": bar("NEW", DAY_ONE, opened="100")},
            exit_open_bars_by_symbol={"OLD": None}, entry_execution_id="entry",
            exit_execution_id="exit", planned_entry_available_cash=D("1000"), costs=COSTS,
            closing_bars_by_symbol={"OLD": bar("OLD", DAY_ONE, opened="100"), "NEW": bar("NEW", DAY_ONE, opened="100")},
            historical_closes_by_symbol={"OLD": (D("100"),), "NEW": (D("100"),)},
        )

        self.assertEqual(result.exit_run_result.orders[0].unfilled_reason, "MISSING_BAR")
        self.assertEqual(result.entry_run_result.orders[0].status, "FILLED")
        self.assertEqual(tuple(order.side for order in result.closing_state.orders), (Side.SELL, Side.BUY))

    def test_rejects_scheduled_date_mismatch_before_returning_any_partial_state(self) -> None:
        opening = create_portfolio_state(D("1000"))
        with self.assertRaisesRegex(ValueError, "trade_date"):
            run_portfolio_day(
                opening_state=opening, trade_date=DAY_ONE,
                pending_entry_plans=(entry_plan(),), pending_exit_intents=(),
                entry_scheduled_trade_dates_by_symbol={"NEW": DAY_TWO},
                exit_scheduled_trade_dates_by_symbol={},
                entry_open_bars_by_symbol={"NEW": bar("NEW", DAY_ONE, opened="100")},
                exit_open_bars_by_symbol={}, entry_execution_id="entry", exit_execution_id="exit",
                planned_entry_available_cash=D("1000"), costs=COSTS,
                closing_bars_by_symbol={"NEW": bar("NEW", DAY_ONE, opened="100")},
                historical_closes_by_symbol={"NEW": (D("100"),)},
            )
        self.assertEqual(opening, create_portfolio_state(D("1000")))


if __name__ == "__main__":
    unittest.main()
