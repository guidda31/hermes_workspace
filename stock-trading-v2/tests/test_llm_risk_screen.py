"""Tests for the disclosure risk screener (defensive landmine detection).

Not alpha — loss avoidance. A deterministic first-pass keyword classifier over DART
disclosure titles, flagging material negatives (dilution, production halt, clinical
setback, delisting/management/halt, fraud, capital reduction, disclosure violation,
litigation) by severity, with negation handling so "관리종목 미지정" is NOT a flag.
The LLM refines the flagged set; this narrows hundreds of filings to the few worth reading.
"""

import unittest
from datetime import datetime, timedelta, timezone

from swing_v2.llm.brief import EvidenceItem
from swing_v2.llm.risk_screen import RiskFlag, Severity, screen_disclosures


KST = timezone(timedelta(hours=9))


def _item(evidence_id, summary, symbol="005930"):
    return EvidenceItem(evidence_id=evidence_id, kind="disclosure", symbol=symbol,
                        published_at=datetime(2026, 7, 20, 9, tzinfo=KST), summary=summary)


class RiskScreenTests(unittest.TestCase):
    def _cats(self, flags):
        return {(f.category, f.severity) for f in flags}

    def test_flags_dilution_rights_issue(self):
        flags = screen_disclosures("000660", (_item("d1", "[기재정정]주요사항보고서(유상증자결정)", "000660"),))
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].category, "DILUTION")
        self.assertIs(flags[0].severity, Severity.MEDIUM)

    def test_flags_production_halt_high(self):
        flags = screen_disclosures("005380", (_item("d1", "생산중단", "005380"),))
        self.assertEqual(self._cats(flags), {("PRODUCTION_HALT", Severity.HIGH)})

    def test_flags_clinical_setback(self):
        flags = screen_disclosures("068270", (_item("d1", "투자판단관련주요경영사항(CTP51 유럽 임상 3상 조기종료 및 시험계획 자진취하)", "068270"),))
        self.assertEqual(flags[0].category, "CLINICAL_SETBACK")
        self.assertIs(flags[0].severity, Severity.HIGH)

    def test_negation_management_issue_not_designated_is_not_flagged(self):
        # "불성실공시법인미지정(지정유예)" is NOT a violation — must not flag.
        flags = screen_disclosures("005490", (_item("d1", "불성실공시법인미지정              (지정유예)", "005490"),))
        self.assertEqual(flags, ())

    def test_buyback_and_cancellation_are_not_risk(self):
        items = (
            _item("d1", "[기재정정]주식소각결정"),
            _item("d2", "자기주식취득결과보고서"),
            _item("d3", "기업설명회(IR)개최(안내공시)"),
        )
        self.assertEqual(screen_disclosures("105560", items), ())

    def test_rights_issue_result_is_not_flagged_only_the_decision(self):
        # The completed result is old news; only the *decision* is the risk signal.
        flags = screen_disclosures("000660", (_item("d1", "유상증자또는주식관련사채등의발행결과(자율공시)", "000660"),))
        self.assertEqual(flags, ())

    def test_capital_reduction_and_delisting_are_high(self):
        items = (_item("d1", "감자결정"), _item("d2", "상장적격성 실질심사 대상 결정"))
        cats = self._cats(screen_disclosures("XXXXXX", items))
        self.assertIn(("CAPITAL_REDUCTION", Severity.HIGH), cats)
        self.assertIn(("DELISTING_RISK", Severity.HIGH), cats)

    def test_returns_sorted_high_first(self):
        items = (_item("d1", "소송등의판결"), _item("d2", "생산중단"))
        flags = screen_disclosures("005380", items)
        self.assertIs(flags[0].severity, Severity.HIGH)   # production halt first
        self.assertEqual(flags[0].category, "PRODUCTION_HALT")

    def test_flag_carries_symbol_title_and_evidence_id(self):
        flags = screen_disclosures("005380", (_item("dX", "생산중단", "005380"),))
        self.assertIsInstance(flags[0], RiskFlag)
        self.assertEqual(flags[0].symbol, "005380")
        self.assertEqual(flags[0].evidence_id, "dX")
        self.assertIn("생산중단", flags[0].disclosure_title)


if __name__ == "__main__":
    unittest.main()
