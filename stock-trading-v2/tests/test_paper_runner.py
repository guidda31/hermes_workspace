"""Tests for the durable multi-session paper runner.

The runner composes the leaf modules into a restart-safe daily loop: recover the
latest account from the ledger, honor the kill switch (block new BUYs), simulate the
session, and persist it write-once (so a date cannot be double-applied). It also
rebuilds session results from disk for cross-restart reporting.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest.engine import ExecutionCostConfig, Side
from swing_v2.contracts import DailyBar
from swing_v2.llm.decision import DecisionAction, SymbolDecision
from swing_v2.paper.kill_switch import engage_kill_switch
from swing_v2.paper.ledger import load_latest_account
from swing_v2.paper.report import PaperReport
from swing_v2.paper.runner import load_session_results, paper_report, run_paper_session


D = Decimal
KST = timezone(timedelta(hours=9))
D1, D2 = date(2026, 7, 17), date(2026, 7, 20)


def _no_costs():
    return ExecutionCostConfig(D("0"), D("0"), D("0"), D("0"), {"STOCK": D("0")}, D("0"), lambda p, _s: p)


def _bar(symbol, open_price, close=None):
    o = D(open_price)
    c = D(close) if close is not None else o
    return DailyBar(D1, symbol, "STOCK", o, o + D("1"), o - D("1"), c, 1_000_000, o * 1_000_000, True)


def _bar_on(day, symbol, open_price, close=None):
    o = D(open_price)
    c = D(close) if close is not None else o
    return DailyBar(day, symbol, "STOCK", o, o + D("1"), o - D("1"), c, 1_000_000, o * 1_000_000, True)


def _buy(symbol, weight="0.1"):
    return SymbolDecision(symbol, DecisionAction.BUY, D("0.8"), D(weight), "why", ())


def _sell(symbol):
    return SymbolDecision(symbol, DecisionAction.SELL, D("0.9"), D("0"), "exit", ())


class PaperRunnerTests(unittest.TestCase):
    def _dirs(self, tmp):
        return Path(tmp) / "sessions", Path(tmp) / "kill.json"

    def test_first_session_uses_initial_cash_and_persists(self):
        with TemporaryDirectory() as tmp:
            sessions, kill = self._dirs(tmp)
            result = run_paper_session(
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("1000000"),
                decisions=(_buy("005930", "0.1"),),
                session_bars={"005930": _bar("005930", "100")},
                reference_close_by_symbol={"005930": D("100")},
                costs=_no_costs(), trade_date=D1,
            )
            self.assertEqual(len(result.account.positions), 1)
            self.assertEqual(result.account.cash, D("900000"))
            # persisted -> recoverable
            recovered = load_latest_account(sessions)
            self.assertEqual(recovered.cash, D("900000"))

    def test_second_session_recovers_account_across_restart(self):
        with TemporaryDirectory() as tmp:
            sessions, kill = self._dirs(tmp)
            run_paper_session(
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("1000000"),
                decisions=(_buy("005930", "0.1"),),
                session_bars={"005930": _bar("005930", "100")},
                reference_close_by_symbol={"005930": D("100")}, costs=_no_costs(), trade_date=D1,
            )
            # Fresh call (simulating a restart): initial_cash is ignored because a
            # prior account exists; the held position is recovered and sold.
            result2 = run_paper_session(
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("999"),
                decisions=(_sell("005930"),),
                session_bars={"005930": _bar_on(D2, "005930", "110")},
                reference_close_by_symbol={"005930": D("110")}, costs=_no_costs(), trade_date=D2,
            )
            self.assertEqual(result2.account.positions, ())
            self.assertEqual(result2.account.cash, D("1010000"))  # 900,000 + 110,000 proceeds
            self.assertEqual(result2.realized_pnl, D("10000"))

    def test_duplicate_session_date_is_refused(self):
        with TemporaryDirectory() as tmp:
            sessions, kill = self._dirs(tmp)
            kwargs = dict(
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("1000000"),
                decisions=(_buy("005930"),), session_bars={"005930": _bar("005930", "100")},
                reference_close_by_symbol={"005930": D("100")}, costs=_no_costs(), trade_date=D1,
            )
            run_paper_session(**kwargs)
            with self.assertRaises(ValueError):
                run_paper_session(**kwargs)  # same date -> write-once guard

    def test_kill_switch_blocks_new_buys_but_allows_sells(self):
        with TemporaryDirectory() as tmp:
            sessions, kill = self._dirs(tmp)
            # Seed a held position on D1.
            run_paper_session(
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("1000000"),
                decisions=(_buy("005930", "0.1"),),
                session_bars={"005930": _bar("005930", "100")},
                reference_close_by_symbol={"005930": D("100")}, costs=_no_costs(), trade_date=D1,
            )
            engage_kill_switch(kill, reason="manual halt", engaged_at=datetime(2026, 7, 19, 9, tzinfo=KST))
            result = run_paper_session(
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("1000000"),
                decisions=(_sell("005930"), _buy("000660", "0.1")),
                session_bars={"005930": _bar_on(D2, "005930", "110"), "000660": _bar_on(D2, "000660", "200")},
                reference_close_by_symbol={"005930": D("110"), "000660": D("200")},
                costs=_no_costs(), trade_date=D2,
            )
            # SELL executed; BUY blocked and recorded.
            self.assertEqual([f.symbol for f in result.fills], ["005930"])
            self.assertIn(("000660", "BUY", "KILL_SWITCH_ENGAGED"),
                          [(u.symbol, u.side, u.reason) for u in result.unfilled])
            self.assertEqual(result.account.positions, ())

    def test_report_over_persisted_sessions(self):
        with TemporaryDirectory() as tmp:
            sessions, kill = self._dirs(tmp)
            run_paper_session(
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("1000000"),
                decisions=(_buy("005930", "0.1"),),
                session_bars={"005930": _bar("005930", "100")},
                reference_close_by_symbol={"005930": D("100")}, costs=_no_costs(), trade_date=D1,
            )
            run_paper_session(
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("1000000"),
                decisions=(_sell("005930"),),
                session_bars={"005930": _bar_on(D2, "005930", "110")},
                reference_close_by_symbol={"005930": D("110")}, costs=_no_costs(), trade_date=D2,
            )
            results = load_session_results(sessions)
            self.assertEqual(len(results), 2)
            report = paper_report(sessions)
            self.assertIsInstance(report, PaperReport)
            self.assertEqual(report.session_count, 2)
            self.assertEqual(report.total_realized_pnl, D("10000"))
            self.assertEqual([d for d, _ in report.equity_curve], [D1, D2])


if __name__ == "__main__":
    unittest.main()
