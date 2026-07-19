"""Tests for the paper-trading CLI a Hermes routine drives each session.

Two-step, mirroring the forward CLI: `render_paper_prompt` builds the point-in-time
brief (showing held positions and whether the kill switch blocks entries) for the
agent to reason over; `run_paper_day` takes the agent's reply, guardrails it against
the recovered account, and runs one durable paper session at the execution date.
No real money, order, or network.
"""

import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from swing_v2.backtest_data import DailyBarSnapshot, SnapshotMetadata, save_snapshot
from swing_v2.contracts import DailyBar
from swing_v2.paper.cli import render_paper_prompt, run_paper_day
from swing_v2.paper.kill_switch import engage_kill_switch, is_kill_switch_engaged
from swing_v2.paper.ledger import load_latest_account
from swing_v2.paper.runner import paper_report
from datetime import datetime, timezone


D = Decimal
KST = timezone(timedelta(hours=9))
PILOT = ("005930", "000660")


def _bar(day, symbol, close):
    c = D(close)
    return DailyBar(day, symbol, "STOCK", c, c + D("1"), c - D("1"), c, 1_000_000, c * 1_000_000, True)


def _write_snapshot(tmp):
    days = []
    cursor = date(2026, 7, 20)
    while len(days) < 6:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()
    market = [_bar(d, "KS11", 2500 + i) for i, d in enumerate(days)]
    histories = {
        "005930": [_bar(d, "005930", 70000 + i * 100) for i, d in enumerate(days)],
        "000660": [_bar(d, "000660", 180000 + i * 100) for i, d in enumerate(days)],
    }
    metadata = SnapshotMetadata("TEST", "2026-07-21T00:00:00+09:00", days[-1].isoformat(), True)
    snapshot = DailyBarSnapshot(metadata, "KS11", {"005930": "STOCK", "000660": "STOCK"}, days, histories, market)
    path = Path(tmp) / "snap.json"
    save_snapshot(snapshot, path)
    return path, days[-2], days[-1]  # snapshot_path, signal_date, execution_date


def _reply(signal_date):
    return ('[{"symbol": "005930", "action": "BUY", "conviction": "0.8", '
            '"target_weight": "0.1", "rationale": "momentum", '
            f'"cited_evidence": ["px:005930:{signal_date.isoformat()}"]}}]')


class PaperCliTests(unittest.TestCase):
    def _paths(self, tmp):
        return Path(tmp) / "sessions", Path(tmp) / "kill.json"

    def test_render_prompt_contains_signal_date_and_symbols(self):
        with TemporaryDirectory() as tmp:
            snap, signal_date, _ = _write_snapshot(tmp)
            sessions, kill = self._paths(tmp)
            prompt = render_paper_prompt(
                snapshot_path=snap, signal_date=signal_date, symbols=PILOT,
                session_dir=sessions, kill_switch_path=kill,
            )
            self.assertIn(signal_date.isoformat(), prompt)
            self.assertIn("005930", prompt)

    def test_run_paper_day_executes_and_persists(self):
        with TemporaryDirectory() as tmp:
            snap, signal_date, execution_date = _write_snapshot(tmp)
            sessions, kill = self._paths(tmp)
            result = run_paper_day(
                snapshot_path=snap, signal_date=signal_date, execution_date=execution_date,
                symbols=PILOT, agent_reply=_reply(signal_date), eligible=frozenset(PILOT),
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("10000000"),
            )
            self.assertEqual([f.symbol for f in result.fills], ["005930"])
            self.assertIsNotNone(load_latest_account(sessions))
            self.assertEqual(paper_report(sessions).session_count, 1)

    def test_kill_switch_blocks_buy_in_run(self):
        with TemporaryDirectory() as tmp:
            snap, signal_date, execution_date = _write_snapshot(tmp)
            sessions, kill = self._paths(tmp)
            engage_kill_switch(kill, reason="halt", engaged_at=datetime(2026, 7, 19, 9, tzinfo=KST))
            self.assertTrue(is_kill_switch_engaged(kill))
            result = run_paper_day(
                snapshot_path=snap, signal_date=signal_date, execution_date=execution_date,
                symbols=PILOT, agent_reply=_reply(signal_date), eligible=frozenset(PILOT),
                session_dir=sessions, kill_switch_path=kill, initial_cash=D("10000000"),
            )
            self.assertEqual(result.fills, ())
            self.assertIn("KILL_SWITCH_ENGAGED", [u.reason for u in result.unfilled])

    def test_render_marks_entries_blocked_when_halted(self):
        with TemporaryDirectory() as tmp:
            snap, signal_date, _ = _write_snapshot(tmp)
            sessions, kill = self._paths(tmp)
            engage_kill_switch(kill, reason="halt", engaged_at=datetime(2026, 7, 19, 9, tzinfo=KST))
            prompt = render_paper_prompt(
                snapshot_path=snap, signal_date=signal_date, symbols=PILOT,
                session_dir=sessions, kill_switch_path=kill,
            )
            self.assertRegex(prompt.lower(), r"block|중단|차단")


if __name__ == "__main__":
    unittest.main()
