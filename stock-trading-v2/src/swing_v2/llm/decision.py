"""Structured trading decision produced by Hermes, validated at the tool boundary.

The repository never calls an LLM API. Hermes (the agent, authenticated to GPT via
the OpenAI OAuth session) reads a point-in-time brief and returns a decision as a
plain mapping/list. These parsers are the enforcement point between the agent and
the deterministic strategy code: they reject anything that is not exactly the v0
decision schema, and — critically — reject any evidence citation or symbol that was
not part of the brief the agent was given (a hallucination guard). No sizing, order,
fill, cash, or network concept appears here; that is the guardrail/execution layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


class DecisionAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


_UNIT_LOW = Decimal("0")
_UNIT_HIGH = Decimal("1")


def _require_plain_nonempty_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty plain str")
    return value


def _coerce_unit_decimal(value: object, field_name: str) -> Decimal:
    """Parse a wire scalar into a finite Decimal within the closed unit interval."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{field_name} must be a numeric scalar")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal value") from exc
    if not parsed.is_finite() or parsed < _UNIT_LOW or parsed > _UNIT_HIGH:
        raise ValueError(f"{field_name} must be a finite value within [0, 1]")
    return parsed


@dataclass(frozen=True)
class SymbolDecision:
    symbol: str
    action: DecisionAction
    conviction: Decimal
    target_weight: Decimal
    rationale: str
    cited_evidence: tuple[str, ...]


def parse_symbol_decision(
    raw: object,
    *,
    known_symbols: frozenset[str],
    known_evidence_ids: frozenset[str],
) -> SymbolDecision:
    """Validate one agent-produced decision mapping into a typed SymbolDecision.

    Rejects unknown symbols, unknown actions, out-of-range convictions/weights,
    empty rationales, and any cited evidence id not present in the brief.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("decision must be a mapping")

    required = {"symbol", "action", "conviction", "target_weight", "rationale", "cited_evidence"}
    missing = required - set(raw.keys())
    if missing:
        raise ValueError(f"decision is missing required fields: {sorted(missing)}")

    symbol = _require_plain_nonempty_string(raw["symbol"], "symbol")
    if symbol not in known_symbols:
        raise ValueError(f"decision symbol {symbol!r} is not in the brief universe")

    action_raw = raw["action"]
    if type(action_raw) is not str or action_raw not in DecisionAction.__members__:
        raise ValueError(f"action must be one of {sorted(DecisionAction.__members__)}")
    action = DecisionAction[action_raw]

    conviction = _coerce_unit_decimal(raw["conviction"], "conviction")
    target_weight = _coerce_unit_decimal(raw["target_weight"], "target_weight")
    rationale = _require_plain_nonempty_string(raw["rationale"], "rationale")

    cited_evidence = _validate_cited_evidence(raw["cited_evidence"], known_evidence_ids)

    if action is DecisionAction.BUY and (conviction <= _UNIT_LOW or target_weight <= _UNIT_LOW):
        raise ValueError("BUY requires positive conviction and target_weight")
    if action is DecisionAction.SELL and target_weight != _UNIT_LOW:
        raise ValueError("SELL must target a full exit (target_weight == 0) in v0")

    return SymbolDecision(
        symbol=symbol,
        action=action,
        conviction=conviction,
        target_weight=target_weight,
        rationale=rationale,
        cited_evidence=cited_evidence,
    )


def _validate_cited_evidence(value: object, known_evidence_ids: frozenset[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("cited_evidence must be a list of evidence ids")
    evidence: list[str] = []
    for item in value:
        evidence_id = _require_plain_nonempty_string(item, "cited_evidence item")
        if evidence_id not in known_evidence_ids:
            raise ValueError(f"cited evidence {evidence_id!r} was not in the brief")
        evidence.append(evidence_id)
    return tuple(evidence)


def parse_decision_set(
    raw: object,
    *,
    known_symbols: frozenset[str],
    known_evidence_ids: frozenset[str],
) -> tuple[SymbolDecision, ...]:
    """Validate the agent's full decision list; reject duplicate symbols."""
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        raise ValueError("decision set must be a list of decisions")
    decisions: list[SymbolDecision] = []
    seen: set[str] = set()
    for entry in raw:
        decision = parse_symbol_decision(
            entry, known_symbols=known_symbols, known_evidence_ids=known_evidence_ids
        )
        if decision.symbol in seen:
            raise ValueError(f"duplicate decision for symbol {decision.symbol!r}")
        seen.add(decision.symbol)
        decisions.append(decision)
    return tuple(decisions)
