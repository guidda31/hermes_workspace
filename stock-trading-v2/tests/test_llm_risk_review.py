"""Tests for the LLM risk-refinement seam.

The keyword screener over-flags on purpose; this layer lets the LLM (Hermes/Claude,
at the runtime — the repo makes no API call) review each raw flag: dismiss noise (e.g.
a subsidiary's rights issue), confirm real material risk, refine severity, and add a
one-line reason + suggested action. render -> (LLM judges) -> parse, mirroring the
decision seam. Verdicts may only reference the evidence ids they were shown.
"""

import unittest

from swing_v2.llm.risk_review import (
    RefinedRiskFlag,
    apply_review,
    parse_risk_review,
    render_risk_review_prompt,
)
from swing_v2.llm.risk_screen import RiskFlag, Severity


def _flag(evidence_id, symbol, category, severity, title):
    return RiskFlag(symbol=symbol, category=category, severity=severity,
                    disclosure_title=title, evidence_id=evidence_id)


FLAGS = (
    _flag("dart:005380:1", "005380", "PRODUCTION_HALT", Severity.HIGH, "생산중단"),
    _flag("dart:105560:2", "105560", "DILUTION", Severity.MEDIUM, "유상증자결정(자회사의 주요경영사항)"),
)


class RenderTests(unittest.TestCase):
    def test_prompt_lists_flags_and_schema(self):
        prompt = render_risk_review_prompt(FLAGS)
        self.assertIn("dart:005380:1", prompt)
        self.assertIn("dart:105560:2", prompt)
        for token in ("material", "severity", "reason", "action", "JSON"):
            self.assertIn(token, prompt)


class ParseTests(unittest.TestCase):
    def test_parses_verdicts(self):
        text = ('[{"evidence_id":"dart:005380:1","material":true,"severity":"HIGH",'
                '"reason":"자사 생산 중단은 실적 직접 타격","action":"보유 시 비중 축소 검토"},'
                '{"evidence_id":"dart:105560:2","material":false,"severity":null,'
                '"reason":"자회사 소규모 유상증자, 모회사 영향 경미","action":"무시"}]')
        verdicts = parse_risk_review(text, known_evidence_ids=frozenset({"dart:005380:1", "dart:105560:2"}))
        self.assertEqual(len(verdicts), 2)
        self.assertTrue(verdicts[0].material)
        self.assertFalse(verdicts[1].material)

    def test_hallucinated_evidence_id_rejected(self):
        text = '[{"evidence_id":"dart:999:9","material":true,"severity":"HIGH","reason":"x","action":"y"}]'
        with self.assertRaises(ValueError):
            parse_risk_review(text, known_evidence_ids=frozenset({"dart:005380:1"}))

    def test_material_true_requires_severity(self):
        text = '[{"evidence_id":"dart:005380:1","material":true,"severity":null,"reason":"x","action":"y"}]'
        with self.assertRaises(ValueError):
            parse_risk_review(text, known_evidence_ids=frozenset({"dart:005380:1"}))

    def test_parses_code_fenced_json(self):
        text = "여기 결과:\n```json\n[{\"evidence_id\":\"dart:005380:1\",\"material\":true,\"severity\":\"HIGH\",\"reason\":\"r\",\"action\":\"a\"}]\n```"
        verdicts = parse_risk_review(text, known_evidence_ids=frozenset({"dart:005380:1"}))
        self.assertEqual(verdicts[0].evidence_id, "dart:005380:1")


class ApplyTests(unittest.TestCase):
    def test_dismisses_non_material_keeps_material_with_llm_fields(self):
        verdicts = parse_risk_review(
            '[{"evidence_id":"dart:005380:1","material":true,"severity":"HIGH","reason":"실적 직접 타격","action":"비중 축소"},'
            '{"evidence_id":"dart:105560:2","material":false,"severity":null,"reason":"자회사 경미","action":"무시"}]',
            known_evidence_ids=frozenset({"dart:005380:1", "dart:105560:2"}),
        )
        refined = apply_review(FLAGS, verdicts)
        self.assertEqual(len(refined), 1)  # subsidiary dilution dismissed
        self.assertIsInstance(refined[0], RefinedRiskFlag)
        self.assertEqual(refined[0].symbol, "005380")
        self.assertEqual(refined[0].reason, "실적 직접 타격")
        self.assertEqual(refined[0].action, "비중 축소")

    def test_unreviewed_flag_is_kept_conservatively(self):
        # only one verdict provided; the other flag has no verdict -> kept as-is.
        verdicts = parse_risk_review(
            '[{"evidence_id":"dart:005380:1","material":true,"severity":"HIGH","reason":"r","action":"a"}]',
            known_evidence_ids=frozenset({"dart:005380:1"}),
        )
        refined = apply_review(FLAGS, verdicts)
        symbols = {r.symbol for r in refined}
        self.assertIn("005380", symbols)
        self.assertIn("105560", symbols)  # unreviewed kept (fail-safe: don't silently drop)


if __name__ == "__main__":
    unittest.main()
