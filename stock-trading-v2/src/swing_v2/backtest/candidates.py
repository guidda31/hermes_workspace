"""Pure ranking and capacity selection for entry candidates."""

from collections.abc import Sequence, Set
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Candidate:
    """A fully evaluated symbol available for entry ranking.

    Position and pending-order state deliberately does not belong on a candidate.
    ``select_entry_candidates`` receives that state in its portfolio snapshot.
    """

    symbol: str
    eligible: bool
    breakout_strength: Decimal
    momentum_60: Decimal

    def __post_init__(self) -> None:
        _validate_symbol(self.symbol, "symbol")
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a bool")
        for name, value in (
            ("breakout_strength", self.breakout_strength),
            ("momentum_60", self.momentum_60),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")


@dataclass(frozen=True)
class CandidateSelection:
    """All eligible ranked candidates plus the capacity-limited selection."""

    selected: tuple[Candidate, ...]
    available_slots: int
    ranked: tuple[Candidate, ...] = ()


def select_entry_candidates(
    *,
    candidates: Sequence[Candidate],
    active_symbols: Set[str],
    pending_entry_symbols: Set[str],
    max_positions: int,
) -> CandidateSelection:
    """Select eligible candidates using an authoritative portfolio snapshot.

    ``active_symbols`` and ``pending_entry_symbols`` are the only sources of
    occupancy state.  They must be disjoint: a symbol cannot simultaneously
    occupy an active position and reserve an entry slot.
    """
    _validate_nonnegative_integer(max_positions, "max_positions")
    active_symbols = _validate_symbol_set(active_symbols, "active_symbols")
    pending_entry_symbols = _validate_symbol_set(
        pending_entry_symbols, "pending_entry_symbols"
    )

    if active_symbols & pending_entry_symbols:
        raise ValueError("active_symbols and pending_entry_symbols must be disjoint")

    available_slots = max_positions - len(active_symbols) - len(pending_entry_symbols)
    if available_slots < 0:
        raise ValueError("portfolio snapshot exceeds max_positions")

    _validate_unique_candidate_symbols(candidates)
    occupied_symbols = active_symbols | pending_entry_symbols
    eligible_candidates = [
        candidate
        for candidate in candidates
        if candidate.eligible and candidate.symbol not in occupied_symbols
    ]
    ranked = sorted(
        eligible_candidates,
        key=lambda candidate: (
            -candidate.breakout_strength,
            -candidate.momentum_60,
            candidate.symbol,
        ),
    )
    return CandidateSelection(
        selected=tuple(ranked[:available_slots]),
        available_slots=available_slots,
        ranked=tuple(ranked),
    )


def _validate_symbol(symbol: object, name: str) -> None:
    if not isinstance(symbol, str) or not symbol:
        raise ValueError(f"{name} must be a nonempty str")


def _validate_nonnegative_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _validate_symbol_set(symbols: object, name: str) -> frozenset[str]:
    if not isinstance(symbols, Set):
        raise ValueError(f"{name} must be a set of symbols")
    for symbol in symbols:
        _validate_symbol(symbol, name)
    return frozenset(symbols)


def _validate_unique_candidate_symbols(candidates: Sequence[Candidate]) -> None:
    symbols = [candidate.symbol for candidate in candidates]
    if len(symbols) != len(set(symbols)):
        raise ValueError("candidate symbols must be unique")
