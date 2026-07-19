"""Pure, ranked entry plans with sequential cash reservation."""

from collections.abc import Sequence, Set
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .candidates import Candidate, select_entry_candidates
from .daily_loss_guard import (
    DailyLossGuardConfig,
    DailyLossGuardInput,
    evaluate_daily_loss_guard,
)
from .engine import BPS_DENOMINATOR, ExecutionCostConfig
from .position_sizing import (
    calculate_entry_quantity_for_fill_price,
    calculate_tick_rounded_buy_fill_price,
    validate_entry_sizing_inputs,
)


@dataclass(frozen=True)
class EntryCandidate:
    """A ranked candidate enriched with its expected next executable open."""

    candidate: Candidate
    expected_open_price: Decimal
    asset_type: str
    signal_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Candidate):
            raise ValueError("candidate must be a Candidate")
        if (
            not isinstance(self.expected_open_price, Decimal)
            or not self.expected_open_price.is_finite()
            or self.expected_open_price <= 0
        ):
            raise ValueError("expected_open_price must be a positive finite Decimal")
        if not isinstance(self.asset_type, str) or not self.asset_type:
            raise ValueError("asset_type must be a nonempty str")
        if type(self.signal_date) is not date:
            raise ValueError("signal_date must be a plain date")


@dataclass(frozen=True)
class EntryPlan:
    """An in-memory BUY intent and the inputs used to size it."""

    symbol: str
    asset_type: str
    signal_date: date
    expected_open_price: Decimal
    quantity: int
    expected_fill_price: Decimal
    expected_cash_cost: Decimal
    nav: Decimal
    risk_per_position: Decimal
    max_position_notional_pct: Decimal
    initial_stop_pct: Decimal
    costs: ExecutionCostConfig

    def __post_init__(self) -> None:
        if type(self.signal_date) is not date:
            raise ValueError("signal_date must be a plain date")


def create_entry_plans(
    *,
    candidates: Sequence[EntryCandidate],
    active_symbols: Set[str],
    pending_entry_symbols: Set[str],
    max_positions: int,
    nav: Decimal,
    available_cash: Decimal,
    costs: ExecutionCostConfig,
    risk_per_position: Decimal,
    max_position_notional_pct: Decimal,
    initial_stop_pct: Decimal,
    daily_loss_guard_input: DailyLossGuardInput | None = None,
    daily_loss_guard_config: DailyLossGuardConfig | None = None,
) -> tuple[EntryPlan, ...]:
    """Return executable BUY plans in rank order without over-reserving cash.

    Candidates that size to zero do not consume a portfolio slot, so lower-ranked
    candidates are considered until all available slots are filled or exhausted.
    """
    validate_entry_sizing_inputs(
        nav=nav,
        available_cash=available_cash,
        costs=costs,
        risk_per_position=risk_per_position,
        max_position_notional_pct=max_position_notional_pct,
        initial_stop_pct=initial_stop_pct,
    )
    entries = tuple(candidates)
    if not all(isinstance(entry, EntryCandidate) for entry in entries):
        raise ValueError("candidates must contain only EntryCandidate values")
    if (daily_loss_guard_input is None) != (daily_loss_guard_config is None):
        raise ValueError(
            "daily_loss_guard_input and daily_loss_guard_config must be provided together"
        )
    if daily_loss_guard_input is not None and daily_loss_guard_config is not None:
        guard_result = evaluate_daily_loss_guard(
            daily_loss_guard_input, daily_loss_guard_config
        )
        if not guard_result.entries_allowed:
            return ()

    selection = select_entry_candidates(
        candidates=tuple(entry.candidate for entry in entries),
        active_symbols=active_symbols,
        pending_entry_symbols=pending_entry_symbols,
        max_positions=max_positions,
    )
    by_symbol = {entry.candidate.symbol: entry for entry in entries}
    cash_remaining = available_cash
    plans: list[EntryPlan] = []

    for ranked_candidate in selection.ranked:
        if len(plans) == selection.available_slots:
            break
        entry = by_symbol[ranked_candidate.symbol]
        fill_price = calculate_tick_rounded_buy_fill_price(entry.expected_open_price, costs)
        quantity = calculate_entry_quantity_for_fill_price(
            fill_price=fill_price,
            nav=nav,
            available_cash=cash_remaining,
            costs=costs,
            asset_type=entry.asset_type,
            risk_per_position=risk_per_position,
            max_position_notional_pct=max_position_notional_pct,
            initial_stop_pct=initial_stop_pct,
        )
        if quantity == 0:
            continue

        cash_cost = _expected_buy_cash_cost(quantity, fill_price, costs)
        plans.append(
            EntryPlan(
                symbol=entry.candidate.symbol,
                asset_type=entry.asset_type,
                signal_date=entry.signal_date,
                expected_open_price=entry.expected_open_price,
                quantity=quantity,
                expected_fill_price=fill_price,
                expected_cash_cost=cash_cost,
                nav=nav,
                risk_per_position=risk_per_position,
                max_position_notional_pct=max_position_notional_pct,
                initial_stop_pct=initial_stop_pct,
                costs=costs,
            )
        )
        cash_remaining -= cash_cost

    return tuple(plans)


def _expected_buy_cash_cost(
    quantity: int, fill_price: Decimal, costs: ExecutionCostConfig
) -> Decimal:
    commission = fill_price * quantity * costs.buy_commission_bps / BPS_DENOMINATOR
    return fill_price * quantity + commission + costs.fixed_fee_per_order
