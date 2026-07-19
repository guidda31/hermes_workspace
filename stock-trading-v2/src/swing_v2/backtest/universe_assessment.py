"""Explicit seam from point-in-time metadata selection to candidate assessment."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from swing_v2.universe_metadata import UniverseMetadataSnapshot, UniverseSelection, select_eligible_universe

from .close_time_candidates import CandidateAssessment, assess_close_time_candidates


@dataclass(frozen=True)
class UniverseCandidateAssessment:
    """Auditable metadata selection plus assessments of its allowed symbols only."""

    selection: UniverseSelection
    assessments: tuple[CandidateAssessment, ...]


def assess_eligible_close_time_candidates(
    *,
    signal_date: date,
    market_closes: Sequence[Decimal],
    asset_types: object,
    asset_histories: object,
    candidate_symbols: Sequence[str],
    universe_metadata: UniverseMetadataSnapshot,
) -> UniverseCandidateAssessment:
    """Filter raw candidates at the signal date before any signal calculation."""
    selection = select_eligible_universe(
        universe_metadata, signal_date, requested_symbols=candidate_symbols,
    )
    assessments = assess_close_time_candidates(
        signal_date=signal_date,
        market_closes=market_closes,
        asset_types=asset_types,
        asset_histories=asset_histories,
        universe_symbols=set(selection.symbols),
    )
    return UniverseCandidateAssessment(selection=selection, assessments=assessments)
