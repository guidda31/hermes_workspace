"""Deterministic first-pass disclosure risk screener (defensive landmine detection).

Not alpha — loss avoidance. Classifies DART disclosure titles into material-risk
categories by severity, so a daily flood of filings narrows to the few worth a human
(or LLM) second look. Buybacks/cancellations and completed rights-issue *results* are
NOT risks; negations like "관리종목 미지정" / "불성실공시 지정유예" are excluded. This is a
coarse keyword pass by design — it over-flags rather than misses, and the LLM refines it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .brief import EvidenceItem


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


@dataclass(frozen=True)
class RiskFlag:
    symbol: str
    category: str
    severity: Severity
    disclosure_title: str
    evidence_id: str


# (severity, category, include-any, exclude-any). First matching rule wins per title;
# HIGH rules are listed before MEDIUM so the highest-severity match is chosen.
_RULES: tuple[tuple[Severity, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (Severity.HIGH, "CAPITAL_REDUCTION", ("감자",), ()),
    (Severity.HIGH, "TRADING_HALT", ("매매거래정지", "거래정지"), ()),
    (Severity.HIGH, "MANAGEMENT_ISSUE", ("관리종목",), ("미지정", "해제")),
    (Severity.HIGH, "DELISTING_RISK", ("상장폐지", "상장적격성", "실질심사"), ()),
    (Severity.HIGH, "FRAUD", ("횡령", "배임"), ()),
    (Severity.HIGH, "INSOLVENCY", ("부도", "회생절차", "파산", "감사의견거절", "감사의견한정", "의견거절"), ()),
    (Severity.HIGH, "DISCLOSURE_VIOLATION", ("불성실공시",), ("미지정", "지정유예", "해제")),
    (Severity.HIGH, "PRODUCTION_HALT", ("생산중단", "영업정지"), ()),
    (Severity.HIGH, "CLINICAL_SETBACK", ("조기종료", "자진취하"), ()),
    (Severity.MEDIUM, "DILUTION", ("유상증자", "전환사채", "신주인수권부사채", "교환사채"),
     ("발행결과", "발행실적", "실적보고서", "증권발행실적")),
    (Severity.MEDIUM, "LITIGATION", ("소송",), ()),
    (Severity.MEDIUM, "CONTROL_CHANGE", ("최대주주변경", "경영권"), ()),
)

_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1}


def _classify(title: str) -> tuple[Severity, str] | None:
    for severity, category, include, exclude in _RULES:
        if any(k in title for k in include) and not any(k in title for k in exclude):
            return severity, category
    return None


def screen_disclosures(symbol: str, disclosures: Sequence[EvidenceItem]) -> tuple[RiskFlag, ...]:
    """Return material-risk flags for one symbol's disclosures, most-severe first."""
    if type(symbol) is not str or not symbol:
        raise ValueError("symbol must be a nonempty plain str")
    flags: list[RiskFlag] = []
    for item in disclosures:
        if not isinstance(item, EvidenceItem):
            raise ValueError("disclosures must be EvidenceItem values")
        classified = _classify(item.summary)
        if classified is not None:
            severity, category = classified
            flags.append(RiskFlag(
                symbol=symbol, category=category, severity=severity,
                disclosure_title=item.summary.strip(), evidence_id=item.evidence_id,
            ))
    flags.sort(key=lambda f: _SEVERITY_ORDER[f.severity])
    return tuple(flags)
