"""Tests for the paper-trading session simulator.

Paper trading exercises the full intent -> fill -> position -> cash -> reconciliation
plumbing with SIMULATED next-open fills and NO real money or broker. It reuses the
backtest cost model (single source of truth) and the same gap-up / IOC discipline.
An AST test asserts the module never reaches a real submitter, gate, or network.
"""

import ast
import pathlib
import unittest
from datetime import date
from decimal import Decimal

from swing_v2.backtest.engine import ExecutionCostConfig, Side
from swing_v2.contracts import DailyBar
from swing_v2.llm.decision import DecisionAction, SymbolDecision
from swing_v2.paper.session import PaperAccount, PaperPosition, simulate_paper_session


D = Decimal
TRADE_DATE = date(2026, 7, 17)


def _no_costs():
    return ExecutionCostConfig(
        buy_slippage_bps=D("0"), sell_slippage_bps=D("0"),
        buy_commission_bps=D("0"), sell_commission_bps=D("0"),
        sell_tax_bps_by_asset_type={"STOCK": D("0")}, fixed_fee_per_order=D("0"),
        tick_rounder=lambda price, _side: price,
    )


def _bar(symbol, open_price, *, is_tradable=True, volume=1_000_000):
    p = D(open_price)
    return DailyBar(TRADE_DATE, symbol, "STOCK", p, p + D("1"), p - D("1"), p, volume, p * volume, is_tradable)


def _buy(symbol, weight="0.1", conviction="0.8"):
    return SymbolDecision(symbol, DecisionAction.BUY, D(conviction), D(weight), "why", ())


def _sell(symbol):
    return SymbolDecision(symbol, DecisionAction.SELL, D("0.9"), D("0"), "exit", ())


def _account(cash="1000000", positions=()):
    return PaperAccount(cash=D(cash), positions=tuple(positions))


class PaperBuyTests(unittest.TestCase):
    def test_buy_into_empty_account_opens_position_and_debits_cash(self):
        result = simulate_paper_session(
            _account("1000000"),
            decisions=(_buy("005930", weight="0.1"),),
            session_bars={"005930": _bar("005930", "100")},
            reference_close_by_symbol={"005930": D("100")},
            costs=_no_costs(),
            trade_date=TRADE_DATE,
        )
        self.assertEqual(len(result.fills), 1)
        # 10% of 1,000,000 / 100 = 1000 shares; zero-cost debit 100,000
        self.assertEqual(result.account.cash, D("900000"))
        self.assertEqual(len(result.account.positions), 1)
        self.assertEqual(result.account.positions[0].symbol, "005930")
        self.assertEqual(result.account.positions[0].quantity, 1000)

    def test_gap_up_beyond_threshold_blocks_the_buy(self):
        result = simulate_paper_session(
            _account("1000000"),
            decisions=(_buy("005930"),),
            session_bars={"005930": _bar("005930", "106")},  # +6% vs ref 100
            reference_close_by_symbol={"005930": D("100")},
            costs=_no_costs(),
            trade_date=TRADE_DATE,
            max_gap_up_pct=D("0.05"),
        )
        self.assertEqual(result.fills, ())
        self.assertEqual(result.account.positions, ())
        self.assertEqual(result.unfilled[0].reason, "GAP_UP_BLOCKED")

    def test_missing_bar_leaves_buy_unfilled(self):
        result = simulate_paper_session(
            _account("1000000"),
            decisions=(_buy("005930"),),
            session_bars={"005930": None},
            reference_close_by_symbol={"005930": D("100")},
            costs=_no_costs(), trade_date=TRADE_DATE,
        )
        self.assertEqual(result.fills, ())
        self.assertEqual(result.unfilled[0].reason, "MISSING_BAR")

    def test_insufficient_cash_leaves_buy_unfilled(self):
        # equity marks the held position at ref close, so sizing wants a big buy the
        # small cash balance cannot fund.
        account = _account("50000", (PaperPosition("000660", "STOCK", D("100"), 9500, date(2026, 7, 1)),))
        result = simulate_paper_session(
            account,
            decisions=(_buy("005930", weight="0.1"),),
            session_bars={"005930": _bar("005930", "100")},
            reference_close_by_symbol={"005930": D("100"), "000660": D("100")},
            costs=_no_costs(), trade_date=TRADE_DATE,
        )
        self.assertEqual(result.fills, ())
        self.assertEqual(result.unfilled[0].reason, "CASH_UNAVAILABLE")

    def test_buy_of_already_held_symbol_is_skipped(self):
        account = _account("1000000", (PaperPosition("005930", "STOCK", D("90"), 100, date(2026, 7, 1)),))
        result = simulate_paper_session(
            account, decisions=(_buy("005930"),),
            session_bars={"005930": _bar("005930", "100")},
            reference_close_by_symbol={"005930": D("100")},
            costs=_no_costs(), trade_date=TRADE_DATE,
        )
        self.assertEqual(result.fills, ())
        self.assertEqual(result.unfilled[0].reason, "ALREADY_HELD")


class PaperSellTests(unittest.TestCase):
    def test_sell_closes_position_credits_cash_and_realizes_pnl(self):
        account = _account("500000", (PaperPosition("005930", "STOCK", D("100"), 1000, date(2026, 7, 1)),))
        result = simulate_paper_session(
            account, decisions=(_sell("005930"),),
            session_bars={"005930": _bar("005930", "110")},
            reference_close_by_symbol={"005930": D("110")},
            costs=_no_costs(), trade_date=TRADE_DATE,
        )
        self.assertEqual(result.account.positions, ())
        self.assertEqual(result.account.cash, D("610000"))  # 500,000 + 110,000 proceeds
        self.assertEqual(result.realized_pnl, D("10000"))   # (110-100)*1000

    def test_sell_of_unheld_symbol_is_unfilled(self):
        result = simulate_paper_session(
            _account("500000"), decisions=(_sell("005930"),),
            session_bars={"005930": _bar("005930", "110")},
            reference_close_by_symbol={"005930": D("110")},
            costs=_no_costs(), trade_date=TRADE_DATE,
        )
        self.assertEqual(result.unfilled[0].reason, "NOT_HELD")


class PaperReconciliationTests(unittest.TestCase):
    def test_cash_after_session_equals_starting_cash_plus_fill_cash_deltas(self):
        account = _account("500000", (PaperPosition("000660", "STOCK", D("100"), 500, date(2026, 7, 1)),))
        result = simulate_paper_session(
            account,
            decisions=(_sell("000660"), _buy("005930", weight="0.1")),
            session_bars={"000660": _bar("000660", "120"), "005930": _bar("005930", "100")},
            reference_close_by_symbol={"000660": D("120"), "005930": D("100")},
            costs=_no_costs(), trade_date=TRADE_DATE,
        )
        expected_cash = account.cash + sum((f.cash_delta for f in result.fills), D("0"))
        self.assertEqual(result.account.cash, expected_cash)

    def test_hold_is_a_noop(self):
        account = _account("500000", (PaperPosition("005930", "STOCK", D("100"), 100, date(2026, 7, 1)),))
        hold = SymbolDecision("005930", DecisionAction.HOLD, D("0.5"), D("0.1"), "keep", ())
        result = simulate_paper_session(
            account, decisions=(hold,),
            session_bars={"005930": _bar("005930", "105")},
            reference_close_by_symbol={"005930": D("105")},
            costs=_no_costs(), trade_date=TRADE_DATE,
        )
        self.assertEqual(result.fills, ())
        self.assertEqual(result.account, account)

    def test_costs_reduce_proceeds_and_pnl(self):
        costs = ExecutionCostConfig(
            buy_slippage_bps=D("10"), sell_slippage_bps=D("10"),
            buy_commission_bps=D("15"), sell_commission_bps=D("15"),
            sell_tax_bps_by_asset_type={"STOCK": D("20")}, fixed_fee_per_order=D("0"),
            tick_rounder=lambda price, _side: price,
        )
        account = _account("0", (PaperPosition("005930", "STOCK", D("100"), 1000, date(2026, 7, 1)),))
        result = simulate_paper_session(
            account, decisions=(_sell("005930"),),
            session_bars={"005930": _bar("005930", "110")},
            reference_close_by_symbol={"005930": D("110")},
            costs=costs, trade_date=TRADE_DATE,
        )
        # sell fill price = 110*(1-0.001)=109.89; proceeds net of commission+tax < gross
        self.assertLess(result.account.cash, D("110000"))
        self.assertGreater(result.account.cash, D("0"))


class PaperSafetyTests(unittest.TestCase):
    def test_module_has_no_real_submitter_gate_or_network(self):
        source = pathlib.Path("src/swing_v2/paper/session.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        joined = " ".join(imported)
        for forbidden in ("production_execution", "gate", "requests", "urllib", "socket", "kis"):
            self.assertNotIn(forbidden, joined)
        for call in (".post(", ".submit(", "urlopen("):
            self.assertNotIn(call, source)


if __name__ == "__main__":
    unittest.main()
