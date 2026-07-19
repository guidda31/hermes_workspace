"""Immutable, tamper-evident signal-only audit record for one trading day.

An agent-runtime LLM cannot be pinned or replayed like a temperature-0 API call, so
auditability replaces reproducibility: we durably record exactly what Hermes saw (the
brief), what it decided, which decisions were admitted or rejected, the model id, and
the KST decision time. The record is write-once (O_EXCL) and carries a SHA-256 integrity
digest over its own canonical bytes.

By construction it holds NO order, fill, position, quantity, cash, or price field — a
signal observation is not a trade. Nothing here submits an order or opens a network
connection.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from .brief import Brief, SymbolBrief
from .decision import SymbolDecision
from .guardrail import AdmittedPlan

SIGNAL_AUDIT_SCHEMA_VERSION = 1


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _decimal_or_none(value: Decimal | None) -> str | None:
    return None if value is None else _canonical_decimal(value)


def _serialize_decision(decision: SymbolDecision) -> dict[str, object]:
    return {
        "symbol": decision.symbol,
        "action": decision.action.value,
        "conviction": _canonical_decimal(decision.conviction),
        "target_weight": _canonical_decimal(decision.target_weight),
        "rationale": decision.rationale,
        "cited_evidence": list(decision.cited_evidence),
    }


def _serialize_symbol_brief(symbol_brief: SymbolBrief) -> dict[str, object]:
    return {
        "symbol": symbol_brief.symbol,
        "asset_type": symbol_brief.asset_type,
        "latest_trade_date": symbol_brief.latest_trade_date.isoformat(),
        "latest_close": _canonical_decimal(symbol_brief.latest_close),
        "latest_trading_value": _canonical_decimal(symbol_brief.latest_trading_value),
        "moving_average_20": _decimal_or_none(symbol_brief.moving_average_20),
        "moving_average_60": _decimal_or_none(symbol_brief.moving_average_60),
        "return_20": _decimal_or_none(symbol_brief.return_20),
        "return_60": _decimal_or_none(symbol_brief.return_60),
        "liquidity_pass": symbol_brief.liquidity_pass,
        "price_evidence_id": symbol_brief.price_evidence_id,
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "symbol": item.symbol,
                "published_at": item.published_at.isoformat(),
                "summary": item.summary,
            }
            for item in symbol_brief.evidence
        ],
    }


def _serialize_brief(brief: Brief) -> dict[str, object]:
    return {
        "signal_date": brief.signal_date.isoformat(),
        "market": {
            "symbol": brief.market.symbol,
            "latest_trade_date": brief.market.latest_trade_date.isoformat(),
            "latest_close": _canonical_decimal(brief.market.latest_close),
            "is_risk_on": brief.market.is_risk_on,
            "price_evidence_id": brief.market.price_evidence_id,
        },
        "symbols": [_serialize_symbol_brief(s) for s in brief.symbols],
    }


def _canonical_bytes(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(obj: object) -> str:
    return hashlib.sha256(_canonical_bytes(obj)).hexdigest()


def build_signal_audit(
    *,
    brief: Brief,
    decisions: tuple[SymbolDecision, ...],
    plan: AdmittedPlan,
    model_id: str,
    decided_at: datetime,
) -> dict[str, object]:
    """Assemble the immutable record. ``integrity.digest`` covers all other members."""
    if not isinstance(brief, Brief):
        raise ValueError("brief must be a Brief")
    if not isinstance(plan, AdmittedPlan):
        raise ValueError("plan must be an AdmittedPlan")
    if type(model_id) is not str or not model_id.strip():
        raise ValueError("model_id must be a nonempty plain str")
    if type(decided_at) is not datetime or decided_at.tzinfo is None:
        raise ValueError("decided_at must be a timezone-aware datetime")

    serialized_brief = _serialize_brief(brief)
    record: dict[str, object] = {
        "schema_version": SIGNAL_AUDIT_SCHEMA_VERSION,
        "signal_date": brief.signal_date.isoformat(),
        "model_id": model_id,
        "decided_at": decided_at.isoformat(),
        "brief_digest": _digest(serialized_brief),
        "brief": serialized_brief,
        "decisions": [_serialize_decision(d) for d in decisions],
        "admitted_symbols": [d.symbol for d in plan.admitted],
        "rejected": [
            {"symbol": r.symbol, "action": r.action.value, "reason": r.reason}
            for r in plan.rejected
        ],
    }
    record["integrity"] = {"algorithm": "sha256", "digest": _digest(record)}
    return record


def write_signal_audit(record: Mapping[str, object], path: str | Path) -> None:
    """Write the record write-once as canonical JSON; refuse to overwrite."""
    if not isinstance(record, Mapping) or "integrity" not in record:
        raise ValueError("record must be a mapping with an integrity member")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"signal audit already exists and is immutable: {destination}") from exc
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def load_signal_audit(path: str | Path) -> dict[str, object]:
    """Read a record and re-verify its integrity digest before returning it."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("signal audit must be readable JSON") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("integrity"), dict):
        raise ValueError("signal audit is missing its integrity member")
    stored = raw["integrity"].get("digest")
    body = {k: v for k, v in raw.items() if k != "integrity"}
    if not isinstance(stored, str) or stored != _digest(body):
        raise ValueError("signal audit integrity digest mismatch")
    return raw
