"""LLM refinement seam for the keyword risk screener.

The screener over-flags by design; here the LLM (Hermes/Claude, at the runtime — the
repo makes no API call) reviews each raw flag and returns a structured verdict:
dismiss noise, confirm real material risk, refine severity, and add a one-line reason
and suggested action. render_risk_review_prompt -> (LLM judges) -> parse_risk_review,
mirroring the decision seam. A verdict may only reference an evidence id it was shown,
and an unreviewed flag is kept conservatively rather than silently dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json

from .risk_screen import RiskFlag, Severity


@dataclass(frozen=True)
class RiskReviewVerdict:
    evidence_id: str
    material: bool
    severity: Severity | None
    reason: str
    action: str


@dataclass(frozen=True)
class RefinedRiskFlag:
    symbol: str
    category: str
    severity: Severity
    disclosure_title: str
    evidence_id: str
    reason: str
    action: str


_SCHEMA = """\
Respond with ONLY a JSON array — one verdict per flag above:
  {"evidence_id": <one id above>, "material": true|false,
   "severity": "HIGH"|"MEDIUM"|null, "reason": <short string>, "action": <short string>}
Rules: reference only the evidence_ids shown; material=false to dismiss noise (e.g. a
subsidiary's small rights issue, routine filings); if material=true, severity must be
HIGH or MEDIUM. Keep reason/action to one short line each (Korean is fine)."""


def render_risk_review_prompt(flags: Sequence[RiskFlag]) -> str:
    """Render the raw flags for LLM review."""
    lines = [
        "You are a KRX risk reviewer. For each auto-flagged disclosure below, judge "
        "whether it is a MATERIAL risk to a holder of that stock, refine its severity, "
        "and give a one-line reason and suggested action. This is loss-avoidance, not a "
        "trade recommendation.\n",
        "Auto-flagged disclosures:",
    ]
    for flag in flags:
        lines.append(
            f"- {flag.symbol} [{flag.category}/{flag.severity.value}] {flag.disclosure_title}"
            f"  (evidence_id={flag.evidence_id})"
        )
    lines.append("\n" + _SCHEMA)
    return "\n".join(lines) + "\n"


def _extract_json(text: str) -> object:
    fence = "```"
    if fence in text:
        start = text.find(fence)
        newline = text.find("\n", start)
        end = text.find(fence, newline + 1)
        if newline != -1 and end != -1:
            body = text[newline + 1:end].strip()
            if body.lower().startswith("json"):
                body = body[4:].strip()
            return json.loads(body)
    stripped = text.strip()
    open_idx, close_idx = stripped.find("["), stripped.rfind("]")
    if open_idx != -1 and close_idx > open_idx:
        return json.loads(stripped[open_idx:close_idx + 1])
    return json.loads(stripped)


def parse_risk_review(text: object, *, known_evidence_ids: frozenset[str]) -> tuple[RiskReviewVerdict, ...]:
    """Parse the LLM's review reply into typed verdicts, fail-closed."""
    if type(text) is not str or not text.strip():
        raise ValueError("review reply must be a nonempty str")
    try:
        parsed = _extract_json(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("review reply did not contain parseable JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError("review reply must be a JSON array of verdicts")

    verdicts: list[RiskReviewVerdict] = []
    for entry in parsed:
        if not isinstance(entry, Mapping):
            raise ValueError("each verdict must be a JSON object")
        evidence_id = entry.get("evidence_id")
        if type(evidence_id) is not str or evidence_id not in known_evidence_ids:
            raise ValueError(f"verdict references an unknown evidence_id: {evidence_id!r}")
        if type(entry.get("material")) is not bool:
            raise ValueError("verdict material must be a bool")
        material = entry["material"]
        severity_raw = entry.get("severity")
        if material:
            if type(severity_raw) is not str or severity_raw not in Severity.__members__:
                raise ValueError("a material verdict must have severity HIGH or MEDIUM")
            severity = Severity[severity_raw]
        else:
            severity = None
        reason = entry.get("reason")
        action = entry.get("action")
        if type(reason) is not str or not reason.strip() or type(action) is not str or not action.strip():
            raise ValueError("verdict reason and action must be nonempty strings")
        verdicts.append(RiskReviewVerdict(evidence_id, material, severity, reason.strip(), action.strip()))
    return verdicts


_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1}


def apply_review(flags: Sequence[RiskFlag], verdicts: Sequence[RiskReviewVerdict]) -> tuple[RefinedRiskFlag, ...]:
    """Apply verdicts to flags: drop dismissed, refine material, keep unreviewed as-is."""
    by_id = {v.evidence_id: v for v in verdicts}
    refined: list[RefinedRiskFlag] = []
    for flag in flags:
        verdict = by_id.get(flag.evidence_id)
        if verdict is None:
            refined.append(RefinedRiskFlag(
                flag.symbol, flag.category, flag.severity, flag.disclosure_title,
                flag.evidence_id, "unreviewed (kept fail-safe)", "review manually",
            ))
        elif verdict.material:
            refined.append(RefinedRiskFlag(
                flag.symbol, flag.category, verdict.severity or flag.severity,
                flag.disclosure_title, flag.evidence_id, verdict.reason, verdict.action,
            ))
        # dismissed (material=false) -> dropped
    refined.sort(key=lambda r: _ORDER[r.severity])
    return tuple(refined)
