"""Adapts PIT KRX universe metadata into the guardrail's eligible_symbols set.

Deny-by-default: only symbols the existing ``select_eligible_universe`` classifier
deems eligible *and* whose provenance is available at ``signal_date`` survive. Per
symbol denial (missing/expired metadata, future-dated provenance, or an ineligibility
flag) is delegated to that classifier, which never crashes on those cases. Structural
input errors (wrong types, blank symbols) fail closed with ``ValueError`` at the
boundary — matching how ``universe_metadata`` validates its own inputs — so a
malformed call cannot silently seed a permissive guardrail.
"""

from __future__ import annotations

from datetime import date

from ..universe_metadata import UniverseMetadataSnapshot, select_eligible_universe


def eligible_symbols_as_of(
    metadata: UniverseMetadataSnapshot,
    signal_date: date,
    candidate_symbols: frozenset[str],
) -> frozenset[str]:
    """Return the candidates tradable at ``signal_date`` per universe_metadata rules."""
    if not isinstance(metadata, UniverseMetadataSnapshot):
        raise ValueError("metadata must be a UniverseMetadataSnapshot")
    if type(signal_date) is not date:
        raise ValueError("signal_date must be a plain date")
    if type(candidate_symbols) is not frozenset:
        raise ValueError("candidate_symbols must be a frozenset")
    if any(type(symbol) is not str or not symbol.strip() for symbol in candidate_symbols):
        raise ValueError("candidate_symbols must contain nonempty plain str values")

    selection = select_eligible_universe(
        metadata, signal_date, requested_symbols=tuple(sorted(candidate_symbols))
    )
    return frozenset(selection.symbols)
