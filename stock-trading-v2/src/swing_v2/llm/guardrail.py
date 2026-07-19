"""Hard guardrails over Hermes' proposed decisions — the code enforces, the agent proposes.

This is the signal-only admission stage: it decides which proposed decisions are
allowed to become *intents*, and why the rest are rejected. It never sizes to shares,
reserves cash, or submits an order — that is the (separately gated) execution layer.

Enforced in v0:
- deny-by-default eligible universe (a BUY for an unlisted/ineligible symbol is rejected);
- the upstream new-entry block (daily-loss guard / risk-off already decided) blocks all BUYs;
- one position per symbol, so a BUY of an already-held name is rejected;
- a single-position target-weight cap;
- the max concurrent position count, dropping the lowest-conviction BUYs beyond capacity.

Deliberately NOT yet enforced (documented, not silently dropped): sector concentration
(max 2 / 25%), ETF aggregate cap (40%), and aggregate-weight normalization. These are a
later slice; until then this guardrail must not be described as full doc-02 risk control.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .decision import DecisionAction, SymbolDecision


_MAX_POSITIONS = 5
_MAX_SINGLE_WEIGHT = Decimal("0.20")  # doc-04 max_position_notional_pct default


@dataclass(frozen=True)
class PortfolioContext:
    held_symbols: frozenset[str]
    new_entries_blocked: bool

    def __post_init__(self) -> None:
        if not isinstance(self.held_symbols, frozenset):
            raise ValueError("held_symbols must be a frozenset")
        if type(self.new_entries_blocked) is not bool:
            raise ValueError("new_entries_blocked must be a bool")


@dataclass(frozen=True)
class GuardrailConfig:
    eligible_symbols: frozenset[str]
    max_positions: int = _MAX_POSITIONS
    max_single_weight: Decimal = _MAX_SINGLE_WEIGHT

    def __post_init__(self) -> None:
        if not isinstance(self.eligible_symbols, frozenset):
            raise ValueError("eligible_symbols must be a frozenset")
        if type(self.max_positions) is not int or self.max_positions <= 0:
            raise ValueError("max_positions must be a positive int")
        if type(self.max_single_weight) is not Decimal or not self.max_single_weight.is_finite():
            raise ValueError("max_single_weight must be a finite Decimal")
        if self.max_single_weight <= 0 or self.max_single_weight > 1:
            raise ValueError("max_single_weight must be within (0, 1]")


@dataclass(frozen=True)
class RejectedDecision:
    symbol: str
    action: DecisionAction
    reason: str


@dataclass(frozen=True)
class AdmittedPlan:
    admitted: tuple[SymbolDecision, ...]
    rejected: tuple[RejectedDecision, ...]


def apply_guardrails(
    decisions: Sequence[SymbolDecision],
    *,
    portfolio: PortfolioContext,
    config: GuardrailConfig,
) -> AdmittedPlan:
    """Admit only decisions within the hard limits; reject the rest with a reason."""
    if not isinstance(portfolio, PortfolioContext):
        raise ValueError("portfolio must be a PortfolioContext")
    if not isinstance(config, GuardrailConfig):
        raise ValueError("config must be a GuardrailConfig")

    admitted_sells_holds: list[SymbolDecision] = []
    candidate_buys: list[SymbolDecision] = []
    rejected: list[RejectedDecision] = []

    for decision in decisions:
        if type(decision) is not SymbolDecision:
            raise ValueError("each decision must be a SymbolDecision")
        reason = _per_decision_reason(decision, portfolio, config)
        if reason is not None:
            rejected.append(RejectedDecision(decision.symbol, decision.action, reason))
        elif decision.action is DecisionAction.BUY:
            candidate_buys.append(decision)
        else:
            admitted_sells_holds.append(decision)

    admitted_buys, capacity_rejects = _enforce_max_positions(
        candidate_buys, admitted_sells_holds, portfolio, config
    )
    rejected.extend(capacity_rejects)

    return AdmittedPlan(
        admitted=tuple(admitted_sells_holds + admitted_buys),
        rejected=tuple(rejected),
    )


def _per_decision_reason(
    decision: SymbolDecision, portfolio: PortfolioContext, config: GuardrailConfig
) -> str | None:
    """Return a rejection reason for a single decision, or None if locally admissible."""
    held = decision.symbol in portfolio.held_symbols
    if decision.action in (DecisionAction.SELL, DecisionAction.HOLD):
        if not held:
            return "symbol is not currently held"
        return None
    # BUY
    if held:
        return "symbol is already held (one position per symbol in v0)"
    if decision.symbol not in config.eligible_symbols:
        return "symbol is not in the deny-by-default eligible universe"
    if portfolio.new_entries_blocked:
        return "new entries are blocked (daily-loss guard / risk-off)"
    if decision.target_weight > config.max_single_weight:
        return "target_weight exceeds the single-position cap"
    return None


def _enforce_max_positions(
    candidate_buys: list[SymbolDecision],
    admitted_sells_holds: list[SymbolDecision],
    portfolio: PortfolioContext,
    config: GuardrailConfig,
) -> tuple[list[SymbolDecision], list[RejectedDecision]]:
    """Fill remaining slots with the highest-conviction buys; reject the overflow."""
    sold = {d.symbol for d in admitted_sells_holds if d.action is DecisionAction.SELL}
    remaining_held = len(portfolio.held_symbols - sold)
    free_slots = max(config.max_positions - remaining_held, 0)

    # Highest conviction first; ties broken by symbol for determinism.
    ordered = sorted(candidate_buys, key=lambda d: (-d.conviction, d.symbol))
    admitted = ordered[:free_slots]
    overflow = ordered[free_slots:]
    rejects = [
        RejectedDecision(d.symbol, d.action, "maximum concurrent positions reached")
        for d in overflow
    ]
    return admitted, rejects
