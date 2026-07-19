"""Tests for the LLM (Hermes) decision schema and its tool-boundary validation.

The repo does NOT call an LLM API. Hermes (the agent) produces a decision object
which the strategy passes, as a plain mapping, through these deterministic parsers.
The parser is the enforcement point: it rejects schema violations and any evidence
citation that was not present in the brief the agent was given (hallucination guard).
"""

import unittest
from decimal import Decimal

from swing_v2.llm.decision import (
    DecisionAction,
    SymbolDecision,
    parse_decision_set,
    parse_symbol_decision,
)


KNOWN_SYMBOLS = frozenset({"005930", "000660", "035420"})
KNOWN_EVIDENCE = frozenset({"px:005930:2026-07-16", "dart:005930:0001", "news:005930:a1"})


def _valid_buy_raw():
    return {
        "symbol": "005930",
        "action": "BUY",
        "conviction": "0.8",
        "target_weight": "0.1",
        "rationale": "strong momentum with supportive disclosure",
        "cited_evidence": ["px:005930:2026-07-16", "dart:005930:0001"],
    }


class ParseSymbolDecisionTests(unittest.TestCase):
    def test_valid_buy_parses_into_typed_decision(self):
        decision = parse_symbol_decision(
            _valid_buy_raw(), known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE
        )
        self.assertIsInstance(decision, SymbolDecision)
        self.assertEqual(decision.symbol, "005930")
        self.assertIs(decision.action, DecisionAction.BUY)
        self.assertEqual(decision.conviction, Decimal("0.8"))
        self.assertEqual(decision.target_weight, Decimal("0.1"))
        self.assertEqual(decision.cited_evidence, ("px:005930:2026-07-16", "dart:005930:0001"))

    def test_symbol_outside_brief_universe_is_rejected(self):
        raw = _valid_buy_raw()
        raw["symbol"] = "999999"
        with self.assertRaises(ValueError):
            parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_cited_evidence_not_in_brief_is_rejected_as_hallucination(self):
        raw = _valid_buy_raw()
        raw["cited_evidence"] = ["dart:005930:0001", "news:made-up-9999"]
        with self.assertRaises(ValueError):
            parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_unknown_action_is_rejected(self):
        raw = _valid_buy_raw()
        raw["action"] = "SHORT"
        with self.assertRaises(ValueError):
            parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_conviction_out_of_unit_range_is_rejected(self):
        for bad in ("-0.1", "1.5"):
            raw = _valid_buy_raw()
            raw["conviction"] = bad
            with self.assertRaises(ValueError):
                parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_target_weight_out_of_unit_range_is_rejected(self):
        for bad in ("-0.01", "1.01"):
            raw = _valid_buy_raw()
            raw["target_weight"] = bad
            with self.assertRaises(ValueError):
                parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_empty_rationale_is_rejected(self):
        raw = _valid_buy_raw()
        raw["rationale"] = "   "
        with self.assertRaises(ValueError):
            parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_buy_requires_positive_conviction_and_weight(self):
        for field, value in (("conviction", "0"), ("target_weight", "0")):
            raw = _valid_buy_raw()
            raw[field] = value
            with self.assertRaises(ValueError):
                parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_sell_must_target_full_exit_weight_zero_in_v0(self):
        raw = {
            "symbol": "000660",
            "action": "SELL",
            "conviction": "0.9",
            "target_weight": "0.05",
            "rationale": "trend broke below MA20",
            "cited_evidence": [],
        }
        with self.assertRaises(ValueError):
            parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_hold_and_sell_with_empty_evidence_is_allowed(self):
        raw = {
            "symbol": "000660",
            "action": "HOLD",
            "conviction": "0.4",
            "target_weight": "0.08",
            "rationale": "still within trend, no new catalyst",
            "cited_evidence": [],
        }
        decision = parse_symbol_decision(
            raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE
        )
        self.assertIs(decision.action, DecisionAction.HOLD)
        self.assertEqual(decision.cited_evidence, ())

    def test_missing_field_is_rejected(self):
        for missing in ("symbol", "action", "conviction", "target_weight", "rationale", "cited_evidence"):
            raw = _valid_buy_raw()
            del raw[missing]
            with self.assertRaises(ValueError):
                parse_symbol_decision(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_non_mapping_input_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_symbol_decision(["not", "a", "mapping"], known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_float_conviction_is_rejected_only_if_nonfinite(self):
        # Strings are the canonical wire form; a finite numeric is coerced via str().
        raw = _valid_buy_raw()
        raw["conviction"] = 0.8
        decision = parse_symbol_decision(
            raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE
        )
        self.assertEqual(decision.conviction, Decimal("0.8"))


class ParseDecisionSetTests(unittest.TestCase):
    def test_parses_list_of_decisions(self):
        raw = [
            _valid_buy_raw(),
            {
                "symbol": "000660",
                "action": "HOLD",
                "conviction": "0.5",
                "target_weight": "0.07",
                "rationale": "holding trend",
                "cited_evidence": [],
            },
        ]
        decisions = parse_decision_set(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)
        self.assertEqual(len(decisions), 2)
        self.assertEqual({d.symbol for d in decisions}, {"005930", "000660"})

    def test_duplicate_symbol_is_rejected(self):
        raw = [_valid_buy_raw(), _valid_buy_raw()]
        with self.assertRaises(ValueError):
            parse_decision_set(raw, known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_non_list_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_decision_set(_valid_buy_raw(), known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE)

    def test_no_order_or_fill_fields_leak_into_decisions(self):
        decisions = parse_decision_set(
            [_valid_buy_raw()], known_symbols=KNOWN_SYMBOLS, known_evidence_ids=KNOWN_EVIDENCE
        )
        forbidden = {"order_id", "fill_id", "quantity", "price", "position_id", "cash", "filled"}
        for decision in decisions:
            self.assertEqual(forbidden & set(vars(decision).keys()), set())


if __name__ == "__main__":
    unittest.main()
