"""Tests for the decide-seam glue: render a Brief to a Hermes prompt, and parse the
agent's textual reply back into raw decision mappings.

The repo calls no LLM. These two pure functions are the concrete boundary of the
`decide` seam: the Hermes routine renders the prompt, reasons, and the reply is parsed
here (fail-closed) into dicts that feed the existing decision/guardrail path.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from swing_v2.backtest_data import DailyBarSnapshot, SnapshotBacktestData, SnapshotMetadata
from swing_v2.contracts import DailyBar
from swing_v2.llm.brief import build_brief
from swing_v2.llm.guardrail import PortfolioContext
from swing_v2.llm.prompt import parse_agent_response, render_brief_prompt


KST = timezone(timedelta(hours=9))
PILOT = ("005930", "000660")


def _bar(day, symbol, asset_type, close):
    close = Decimal(close)
    return DailyBar(day, symbol, asset_type, close, close, close, close, 1_000_000, close * 1_000_000, True)


def _make_data(last_day=date(2026, 7, 16), n=6):
    days = []
    cursor = last_day
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()
    market = [_bar(d, "KS11", "INDEX", 2500 + i) for i, d in enumerate(days)]
    histories = {
        "005930": [_bar(d, "005930", "STOCK", 70000 + i * 100) for i, d in enumerate(days)],
        "000660": [_bar(d, "000660", "STOCK", 180000 + i * 100) for i, d in enumerate(days)],
    }
    metadata = SnapshotMetadata("TEST", "2026-07-17T00:00:00+09:00", days[-1].isoformat(), True)
    snapshot = DailyBarSnapshot(metadata, "KS11", {"005930": "STOCK", "000660": "STOCK"}, days, histories, market)
    return SnapshotBacktestData(snapshot), days


def _brief():
    data, days = _make_data()
    return build_brief(data, signal_date=days[-1], symbols=PILOT, window=6)


class RenderBriefPromptTests(unittest.TestCase):
    def test_prompt_is_nonempty_text_with_signal_date_and_symbols(self):
        brief = _brief()
        prompt = render_brief_prompt(brief, portfolio=PortfolioContext(frozenset(), False))
        self.assertIsInstance(prompt, str)
        self.assertIn("2026-07-16", prompt)
        for symbol in PILOT:
            self.assertIn(symbol, prompt)

    def test_prompt_lists_evidence_ids_the_agent_may_cite(self):
        brief = _brief()
        prompt = render_brief_prompt(brief, portfolio=PortfolioContext(frozenset(), False))
        for evidence_id in brief.known_evidence_ids:
            self.assertIn(evidence_id, prompt)

    def test_prompt_states_output_schema_and_constraints(self):
        brief = _brief()
        prompt = render_brief_prompt(brief, portfolio=PortfolioContext(frozenset(), False))
        for token in ("BUY", "SELL", "HOLD", "conviction", "target_weight", "cited_evidence", "JSON"):
            self.assertIn(token, prompt)

    def test_prompt_shows_held_positions(self):
        brief = _brief()
        prompt = render_brief_prompt(brief, portfolio=PortfolioContext(frozenset({"005930"}), False))
        self.assertIn("005930", prompt)

    def test_prompt_marks_new_entries_blocked(self):
        brief = _brief()
        prompt = render_brief_prompt(brief, portfolio=PortfolioContext(frozenset(), True))
        self.assertRegex(prompt.lower(), r"block|중단|차단")


class ParseAgentResponseTests(unittest.TestCase):
    def test_parses_bare_json_array(self):
        text = '[{"symbol": "005930", "action": "HOLD", "conviction": "0.5", ' \
               '"target_weight": "0.1", "rationale": "ok", "cited_evidence": []}]'
        result = parse_agent_response(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "005930")

    def test_parses_json_inside_code_fence(self):
        text = "Here is my decision:\n```json\n[{\"symbol\": \"005930\", \"action\": \"HOLD\"}]\n```\nDone."
        result = parse_agent_response(text)
        self.assertEqual(result[0]["action"], "HOLD")

    def test_parses_object_with_decisions_key(self):
        text = '{"decisions": [{"symbol": "000660", "action": "SELL"}]}'
        result = parse_agent_response(text)
        self.assertEqual(result[0]["symbol"], "000660")

    def test_empty_array_is_allowed(self):
        self.assertEqual(parse_agent_response("[]"), [])

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            parse_agent_response("I decline to answer.")

    def test_non_array_json_raises(self):
        with self.assertRaises(ValueError):
            parse_agent_response('{"symbol": "005930"}')  # object without decisions key

    def test_array_of_non_objects_raises(self):
        with self.assertRaises(ValueError):
            parse_agent_response('["005930", "000660"]')

    def test_non_string_input_raises(self):
        with self.assertRaises(ValueError):
            parse_agent_response(12345)


if __name__ == "__main__":
    unittest.main()
