"""Forward-observation SIGNAL runner: one signal-only cycle, never an order.

This orchestrates the full signal cycle by composing the existing llm tools:
``build_brief`` -> the injected ``decide`` seam -> ``parse_decision_set`` ->
``apply_guardrails`` -> ``build_signal_audit``. It is the seam where Hermes (the LLM
brain) plugs in: ``decide`` receives the built ``Brief`` and returns the agent's raw
decision mappings. This module calls no LLM API and opens no network connection —
``decide`` is injected, and in tests it is a fake.

By construction it produces an immutable signal audit and NEVER an order: no order,
fill, position, quantity, or cash concept appears here or in the record it returns.
A malformed agent response (``decide`` not returning a list of mappings) is surfaced
fail-closed as a ValueError.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from ..backtest_data import SnapshotBacktestData
from .brief import Brief, EvidenceProvider, build_brief
from .decision import parse_decision_set
from .guardrail import GuardrailConfig, PortfolioContext, apply_guardrails
from .signal_audit import build_signal_audit, write_signal_audit

# The injected Hermes seam: it receives the built Brief and returns raw decisions.
DecideFn = Callable[[Brief], Sequence[Mapping]]

# Keep in step with brief._DEFAULT_WINDOW so 60-day indicators are computable.
_DEFAULT_WINDOW = 120


def _require_raw_decisions(raw: object) -> Sequence[Mapping]:
    """Fail-closed check that the agent returned a list of decision mappings."""
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        raise ValueError("decide must return a list of decision mappings")
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValueError("each agent decision must be a mapping")
    return raw


def run_forward_signal(
    data: SnapshotBacktestData,
    *,
    signal_date: date,
    symbols: Sequence[str],
    guardrail_config: GuardrailConfig,
    portfolio: PortfolioContext,
    model_id: str,
    decided_at: datetime,
    decide: DecideFn,
    disclosure_provider: Optional[EvidenceProvider] = None,
    news_provider: Optional[EvidenceProvider] = None,
    window: int = _DEFAULT_WINDOW,
    output_path: Optional[str | Path] = None,
) -> dict:
    """Run one signal-only cycle and return the immutable signal audit record.

    Builds the PIT brief, hands it to the injected ``decide`` seam, validates and
    guardrails the returned decisions, then assembles (and optionally writes) the
    audit. Never sizes, orders, or fills. Fail-closed on bad injected args.
    """
    if not isinstance(data, SnapshotBacktestData):
        raise ValueError("data must be a SnapshotBacktestData")
    if not callable(decide):
        raise ValueError("decide must be a callable Brief -> list[Mapping] seam")
    if not isinstance(guardrail_config, GuardrailConfig):
        raise ValueError("guardrail_config must be a GuardrailConfig")
    if not isinstance(portfolio, PortfolioContext):
        raise ValueError("portfolio must be a PortfolioContext")
    if disclosure_provider is not None and not callable(disclosure_provider):
        raise ValueError("disclosure_provider must be callable or None")
    if news_provider is not None and not callable(news_provider):
        raise ValueError("news_provider must be callable or None")

    brief = build_brief(
        data,
        signal_date=signal_date,
        symbols=symbols,
        disclosure_provider=disclosure_provider,
        news_provider=news_provider,
        window=window,
    )

    raw = _require_raw_decisions(decide(brief))
    decisions = parse_decision_set(
        raw,
        known_symbols=brief.known_symbols,
        known_evidence_ids=brief.known_evidence_ids,
    )
    plan = apply_guardrails(decisions, portfolio=portfolio, config=guardrail_config)
    record = build_signal_audit(
        brief=brief,
        decisions=decisions,
        plan=plan,
        model_id=model_id,
        decided_at=decided_at,
    )
    if output_path is not None:
        write_signal_audit(record, output_path)
    return record
