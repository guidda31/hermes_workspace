"""Tests for durable, immutable paper-trading session persistence.

The ledger gives paper trading restart-recovery (reload the newest account) and
duplicate-session prevention (write-once per trade_date). Records are canonical JSON
carrying a SHA-256 integrity digest; loads re-verify the digest. No network.
"""

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from swing_v2.backtest.engine import ExecutionCostConfig
from swing_v2.contracts import DailyBar
from swing_v2.llm.decision import DecisionAction, SymbolDecision
from swing_v2.paper.ledger import (
    list_session_records,
    load_latest_account,
    load_paper_session,
    save_paper_session,
)
from swing_v2.paper.session import PaperAccount, PaperPosition, simulate_paper_session


D = Decimal


def _no_costs():
    return ExecutionCostConfig(
        buy_slippage_bps=D("0"), sell_slippage_bps=D("0"),
        buy_commission_bps=D("0"), sell_commission_bps=D("0"),
        sell_tax_bps_by_asset_type={"STOCK": D("0")}, fixed_fee_per_order=D("0"),
        tick_rounder=lambda price, _side: price,
    )


def _bar(trade_date, symbol, open_price):
    p = D(open_price)
    return DailyBar(trade_date, symbol, "STOCK", p, p + D("1"), p - D("1"), p, 1_000_000, p * 1_000_000, True)


def _buy(symbol, weight="0.1"):
    return SymbolDecision(symbol, DecisionAction.BUY, D("0.8"), D(weight), "why", ())


def _sell(symbol):
    return SymbolDecision(symbol, DecisionAction.SELL, D("0.9"), D("0"), "exit", ())


def _result(trade_date, symbol):
    """A session that buys ``symbol`` (opens a position + a fill) and fails one SELL."""
    return simulate_paper_session(
        PaperAccount(cash=D("1000000")),
        decisions=(_buy(symbol, weight="0.1"), _sell("999999")),
        session_bars={symbol: _bar(trade_date, symbol, "100")},
        reference_close_by_symbol={symbol: D("100")},
        costs=_no_costs(),
        trade_date=trade_date,
    )


class SaveLoadRoundTripTests(unittest.TestCase):
    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _result(date(2026, 7, 17), "005930")
            path = save_paper_session(tmp, result)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "paper-2026-07-17.json")

            record = load_paper_session(path)
            self.assertEqual(record["trade_date"], "2026-07-17")
            self.assertEqual(record["account"]["cash"], "900000")
            self.assertEqual(record["account"]["positions"][0]["symbol"], "005930")
            self.assertEqual(record["account"]["positions"][0]["quantity"], 1000)
            self.assertEqual(record["fills"][0]["symbol"], "005930")
            self.assertEqual(record["fills"][0]["side"], "BUY")
            self.assertEqual(record["unfilled"][0], {"symbol": "999999", "side": "SELL", "reason": "NOT_HELD"})
            self.assertIn("integrity", record)

    def test_returned_path_lives_in_session_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_paper_session(tmp, _result(date(2026, 7, 17), "005930"))
            self.assertEqual(path.parent, Path(tmp))


class DuplicateGuardTests(unittest.TestCase):
    def test_save_twice_for_same_date_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _result(date(2026, 7, 17), "005930")
            save_paper_session(tmp, result)
            with self.assertRaises(ValueError):
                save_paper_session(tmp, _result(date(2026, 7, 17), "000660"))


class RestartRecoveryTests(unittest.TestCase):
    def test_load_latest_account_returns_newest_date_as_paper_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_paper_session(tmp, _result(date(2026, 7, 15), "005930"))
            save_paper_session(tmp, _result(date(2026, 7, 17), "000660"))
            save_paper_session(tmp, _result(date(2026, 7, 16), "035720"))

            account = load_latest_account(tmp)
            self.assertIsInstance(account, PaperAccount)
            self.assertEqual(len(account.positions), 1)
            position = account.positions[0]
            self.assertIsInstance(position, PaperPosition)
            self.assertEqual(position.symbol, "000660")
            self.assertEqual(position.entry_date, date(2026, 7, 17))
            self.assertEqual(position.entry_price, D("100"))
            self.assertEqual(position.quantity, 1000)
            self.assertEqual(account.cash, D("900000"))

    def test_empty_directory_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_latest_account(tmp))

    def test_non_matching_filenames_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "notes.txt").write_text("ignore me", encoding="utf-8")
            (Path(tmp) / "paper-latest.json").write_text("{}", encoding="utf-8")
            self.assertIsNone(load_latest_account(tmp))
            save_paper_session(tmp, _result(date(2026, 7, 17), "005930"))
            account = load_latest_account(tmp)
            self.assertIsInstance(account, PaperAccount)
            self.assertEqual(account.positions[0].symbol, "005930")


class IntegrityTests(unittest.TestCase):
    def test_tampered_file_fails_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_paper_session(tmp, _result(date(2026, 7, 17), "005930"))
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["account"]["cash"] = "1"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_paper_session(path)


class ListRecordsTests(unittest.TestCase):
    def test_list_session_records_sorted_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_paper_session(tmp, _result(date(2026, 7, 17), "005930"))
            save_paper_session(tmp, _result(date(2026, 7, 15), "000660"))
            save_paper_session(tmp, _result(date(2026, 7, 16), "035720"))

            records = list_session_records(tmp)
            self.assertEqual(
                [r["trade_date"] for r in records],
                ["2026-07-15", "2026-07-16", "2026-07-17"],
            )
            for record in records:
                self.assertIn("integrity", record)

    def test_list_session_records_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(list_session_records(tmp), ())


if __name__ == "__main__":
    unittest.main()
