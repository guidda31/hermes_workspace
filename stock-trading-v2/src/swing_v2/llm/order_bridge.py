"""INERT bridge: an admitted BUY decision -> a risk-validated live-order INTENT.

This is the furthest the LLM signal path may reach toward trading, and it stops well
short of it. It builds a typed ``LiveOrderIntent`` and runs it through the independent
local pretrade risk limits. That is ALL: building and risk-checking an intent is not
placing an order.

Deliberately absent (and asserted so by tests): any import of the order submitter, any
gate flip, any ``LiveExecutionConfig``, any network, any submit/amend/cancel. Turning a
validated intent into a real order is a separate, disabled-by-default, separately
approved path that requires the backtest + paper-trading validation the project does
not yet have. Do not add submission here.

Only BUY is bridged (entries). Exits (SELL/HOLD) are a different lifecycle and are not
handled by this module.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_FLOOR

from ..live.intent import LiveOrderIntent, OrderMode, Side
from ..live.risk import AccountRiskSnapshot, PretradeLimits, validate_pretrade
from .decision import DecisionAction, SymbolDecision

_DEFAULT_INITIAL_STOP_PCT = Decimal("0.05")


def _positive_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")
    return value


def build_inert_intent(
    decision: SymbolDecision,
    *,
    limit_price: Decimal,
    equity: Decimal,
    classification: str,
    strategy: str,
    strategy_version: str,
    signal_date: date,
    planned_or_open_positions: int,
    daily_loss: Decimal,
    initial_stop_pct: Decimal = _DEFAULT_INITIAL_STOP_PCT,
    limits: PretradeLimits | None = None,
) -> LiveOrderIntent:
    """Map an admitted BUY decision to a pretrade-risk-validated LimitOrder intent.

    Returns the validated intent. Raises ``ValueError`` if the decision is not a BUY,
    the sized quantity is below one share, or any independent pretrade risk limit is
    exceeded. Never submits, and never touches the gate or any network.
    """
    if type(decision) is not SymbolDecision:
        raise ValueError("decision must be a SymbolDecision")
    if decision.action is not DecisionAction.BUY:
        raise ValueError("only BUY decisions are bridged to an entry intent")
    _positive_decimal(limit_price, "limit_price")
    _positive_decimal(equity, "equity")
    if type(initial_stop_pct) is not Decimal or not (Decimal("0") < initial_stop_pct < Decimal("1")):
        raise ValueError("initial_stop_pct must be a Decimal in (0, 1)")
    if type(daily_loss) is not Decimal or not daily_loss.is_finite() or daily_loss < 0:
        raise ValueError("daily_loss must be a nonnegative finite Decimal")

    target_notional = decision.target_weight * equity
    quantity = int((target_notional / limit_price).to_integral_value(rounding=ROUND_FLOOR))
    if quantity < 1:
        raise ValueError("sized quantity is below one share")

    intent = LiveOrderIntent(
        strategy=strategy,
        strategy_version=strategy_version,
        signal_date=signal_date,
        symbol=decision.symbol,
        classification=classification,
        side=Side.BUY,
        quantity=quantity,
        limit_price=limit_price,
        order_mode=OrderMode.LIMIT,
    )

    # Proposed per-position risk = shares * stop distance, checked against the account.
    proposed_position_risk = Decimal(quantity) * limit_price * initial_stop_pct
    snapshot = AccountRiskSnapshot(
        planned_or_open_positions=planned_or_open_positions,
        equity=equity,
        daily_loss=daily_loss,
        proposed_position_risk=proposed_position_risk,
    )
    validate_pretrade(intent, snapshot, limits=limits)
    return intent
