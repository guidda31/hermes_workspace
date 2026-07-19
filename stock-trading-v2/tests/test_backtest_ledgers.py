"""Tests for the doc-04 §7.1/§7.2 ledger serializer."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from swing_v2.backtest.backtest_engine import (
    BacktestResult,
    EquityCurvePoint,
    SignalRecord,
    UniverseExclusionRecord,
)
from swing_v2.backtest.engine import Fill, Order, Position, Side
from swing_v2.backtest.ledgers import write_backtest_ledgers, write_run_summary


def _order(order_id: str, status: str, side: Side = Side.BUY) -> Order:
    return Order(
        order_id=order_id, signal_id=f"signal-{order_id}", position_id=None,
        symbol="005930", asset_type="STOCK", side=side,
        signal_date=date(2026, 1, 5), scheduled_trade_date=date(2026, 1, 6),
        status=status, intent_reason="ENTRY_SIGNAL", requested_quantity=10,
        filled_quantity=10 if status == "FILLED" else 0,
        unfilled_quantity=0 if status == "FILLED" else 10,
        unfilled_reason=None if status == "FILLED" else "MISSING_BAR",
    )


def _fill(fill_id: str, order_id: str) -> Fill:
    return Fill(
        fill_id=fill_id, order_id=order_id, position_id="position-1",
        trade_date=date(2026, 1, 6), symbol="005930", asset_type="STOCK",
        side=Side.BUY, quantity=10, reference_open=Decimal("70000"),
        raw_slippage_price=Decimal("70070"), fill_price=Decimal("70100"),
        notional=Decimal("701000"), commission=Decimal("105.15"),
        sell_tax=Decimal("0"), fixed_fee=Decimal("0"),
        total_cost=Decimal("105.15"), cash_delta=Decimal("-701105.15"),
    )


def _position(position_id: str, status: str) -> Position:
    return Position(
        position_id=position_id, symbol="005930", asset_type="STOCK",
        entry_order_id="order-1", entry_fill_id="fill-1",
        entry_price=Decimal("70100"), initial_stop_price=Decimal("66595"),
        quantity=10,
        exit_order_id="order-2" if status == "CLOSED" else None,
        exit_fill_id="fill-2" if status == "CLOSED" else None,
        exit_price=Decimal("72000") if status == "CLOSED" else None,
        exit_reason="STOP_CLOSE" if status == "CLOSED" else None,
        status=status, age_sessions=3,
    )


def _signal(symbol: str, eligible: bool) -> SignalRecord:
    return SignalRecord(
        signal_id=f"signal-2026-01-05-{symbol}", signal_date=date(2026, 1, 5),
        symbol=symbol, eligible=eligible,
        rejection_reason=None if eligible else "LIQUIDITY",
        risk_on=True, liquidity_pass=eligible, momentum_pass=eligible,
        candidate_rank=1 if eligible else None,
        breakout_strength=Decimal("1.25") if eligible else None,
        momentum_60=Decimal("0.30") if eligible else None,
        scheduled_trade_date=date(2026, 1, 6),
    )


def _equity_point() -> EquityCurvePoint:
    return EquityCurvePoint(
        trade_date=date(2026, 1, 6), cash=Decimal("298894.85"),
        market_value=Decimal("721000"), nav_close=Decimal("1019894.85"),
        daily_return=Decimal("0.0198"), cumulative_return=Decimal("0.0198"),
        peak_nav=Decimal("1019894.85"), drawdown=Decimal("0"),
        gross_exposure=Decimal("0.707"), position_count=1, stale_mark_count=0,
        new_entry_blocked=False, new_entry_block_reason=None,
    )


def _result(*, empty: bool = False) -> BacktestResult:
    if empty:
        return BacktestResult((), (), (), (), (), (), ())
    return BacktestResult(
        all_day_results=(object(), object()),
        equity_curve=(_equity_point(),),
        orders=(_order("order-1", "FILLED"), _order("order-2", "CANCELED_UNFILLED", Side.SELL)),
        fills=(_fill("fill-1", "order-1"),),
        positions=(_position("position-1", "CLOSED"), _position("position-2", "OPEN")),
        signals=(_signal("005930", True), _signal("000660", False)),
        universe_exclusions=(UniverseExclusionRecord(date(2026, 1, 5), "035720", "SUSPENDED"),),
    )


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


class WriteBacktestLedgersTest(unittest.TestCase):
    def test_all_files_written_with_headers_and_row_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_backtest_ledgers(_result(), tmp)
            self.assertEqual(
                set(paths), {"equity_curve", "orders", "fills", "positions", "signals"}
            )
            for path in paths.values():
                self.assertTrue(path.exists())

            header, rows = _read_csv(paths["equity_curve"])
            self.assertEqual(
                header,
                ["trade_date", "cash", "market_value", "nav_close", "daily_return",
                 "cumulative_return", "peak_nav", "drawdown", "gross_exposure",
                 "position_count", "stale_mark_count", "new_entry_blocked",
                 "new_entry_block_reason"],
            )
            self.assertEqual(len(rows), 1)

            _, order_rows = _read_csv(paths["orders"])
            self.assertEqual(len(order_rows), 2)
            _, fill_rows = _read_csv(paths["fills"])
            self.assertEqual(len(fill_rows), 1)
            _, position_rows = _read_csv(paths["positions"])
            self.assertEqual(len(position_rows), 2)
            sig_header, sig_rows = _read_csv(paths["signals"])
            self.assertEqual(len(sig_rows), 2)
            self.assertEqual(
                sig_header,
                ["signal_id", "signal_date", "symbol", "eligible", "rejection_reason",
                 "risk_on", "liquidity_pass", "momentum_pass", "candidate_rank",
                 "breakout_strength", "momentum_60", "scheduled_trade_date"],
            )

    def test_decimals_are_canonical_strings_not_floats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_backtest_ledgers(_result(), tmp)
            header, rows = _read_csv(paths["fills"])
            row = dict(zip(header, rows[0]))
            self.assertEqual(row["fill_price"], "70100")
            self.assertEqual(row["commission"], "105.15")
            self.assertEqual(row["cash_delta"], "-701105.15")
            # No float artifacts such as "70100.0".
            self.assertNotIn(".0", row["fill_price"])

            eq_header, eq_rows = _read_csv(paths["equity_curve"])
            eq = dict(zip(eq_header, eq_rows[0]))
            self.assertEqual(eq["daily_return"], "0.0198")
            self.assertEqual(eq["new_entry_blocked"], "false")
            self.assertEqual(eq["new_entry_block_reason"], "")

    def test_optional_fields_render_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_backtest_ledgers(_result(), tmp)
            header, rows = _read_csv(paths["positions"])
            open_row = dict(zip(header, [r for r in rows if r[0] == "position-2"][0]))
            self.assertEqual(open_row["exit_price"], "")
            self.assertEqual(open_row["exit_reason"], "")
            self.assertEqual(open_row["status"], "OPEN")

            sig_header, sig_rows = _read_csv(paths["signals"])
            rejected = dict(zip(sig_header, [r for r in sig_rows if r[2] == "000660"][0]))
            self.assertEqual(rejected["candidate_rank"], "")
            self.assertEqual(rejected["eligible"], "false")
            self.assertEqual(rejected["rejection_reason"], "LIQUIDITY")

    def test_empty_ledger_writes_header_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_backtest_ledgers(_result(empty=True), tmp)
            for path in paths.values():
                header, rows = _read_csv(path)
                self.assertTrue(header)
                self.assertEqual(rows, [])

    def test_creates_missing_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "runs" / "run-001"
            paths = write_backtest_ledgers(_result(), nested)
            self.assertTrue(nested.is_dir())
            self.assertTrue(paths["orders"].exists())

    def test_rejects_non_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                write_backtest_ledgers({"not": "a result"}, tmp)


class WriteRunSummaryTest(unittest.TestCase):
    def test_summary_counts_and_config_round_trip(self) -> None:
        config_summary = {"initial_cash": "1000000", "start_date": "2026-01-01"}
        with tempfile.TemporaryDirectory() as tmp:
            path = write_run_summary(_result(), tmp, config_summary=config_summary)
            self.assertTrue(path.exists())
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            counts = loaded["counts"]
            self.assertEqual(counts["sessions"], 2)
            self.assertEqual(counts["orders"], 2)
            self.assertEqual(counts["fills"], 1)
            self.assertEqual(counts["positions"], 2)
            self.assertEqual(counts["signals"], 2)
            self.assertEqual(counts["orders_by_status"]["FILLED"], 1)
            self.assertEqual(counts["orders_by_status"]["CANCELED_UNFILLED"], 1)
            self.assertEqual(loaded["config_summary"], config_summary)

    def test_empty_result_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_run_summary(_result(empty=True), tmp, config_summary={})
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["counts"]["orders"], 0)
            self.assertEqual(loaded["counts"]["orders_by_status"], {})
            self.assertEqual(loaded["config_summary"], {})

    def test_rejects_non_dict_config_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                write_run_summary(_result(), tmp, config_summary=["not", "a", "dict"])


if __name__ == "__main__":
    unittest.main()
