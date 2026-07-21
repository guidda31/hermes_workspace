"""Orchestration glue for a small, human-armed live KRX pilot order.

Everything dangerous already exists and is guarded elsewhere: the order transport
(``production_execution``), the disabled-by-default gate (``gate``), the fail-closed
pretrade risk limits (``risk``), and the write-once audit (``audit``). This module adds
NO new transport, gate, or risk rule. It only:

  1. builds a validated ``LiveOrderIntent`` + ``AccountRiskSnapshot`` under TIGHTENED
     pilot caps (one position, tiny notional) — ``build_pilot_order``;
  2. renders the EXACT wire body + TR id that would be sent, submitting nothing —
     ``describe_pilot_plan`` (the default/preflight path);
  3. hands a built plan to the existing submitter only when a caller passes the exact
     operator-confirmation phrase — ``submit_pilot_order``.

Preflight is safe and offline. Submission still requires the existing gate's explicit
confirmation string, so this module cannot place an order by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .gate import LiveExecutionConfig
from .intent import LiveOrderIntent, OrderMode, Side
from .kill_switch import require_not_halted
from .risk import AccountRiskSnapshot, PretradeLimits, validate_pretrade

# A pilot is deliberately tiny — these caps sit far below the standing PretradeLimits.
PILOT_MAX_ORDER_NOTIONAL = Decimal("100000")  # 10만원 default hard cap
PILOT_MAX_POSITIONS = 1
_DEFAULT_INITIAL_STOP_PCT = Decimal("0.05")


@dataclass(frozen=True)
class PilotOrderPlan:
    """A validated, not-yet-submitted pilot order and the exact limits it passed."""

    intent: LiveOrderIntent
    snapshot: AccountRiskSnapshot
    limits: PretradeLimits

    @property
    def notional(self) -> Decimal:
        return self.intent.notional


def _positive_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")
    return value


def build_pilot_order(
    *,
    symbol: str,
    side: Side,
    quantity: int,
    limit_price: Decimal,
    signal_date: date,
    equity: Decimal,
    open_positions: int,
    daily_loss: Decimal = Decimal("0"),
    initial_stop_pct: Decimal = _DEFAULT_INITIAL_STOP_PCT,
    classification: str = "STOCK",
    strategy: str = "krx-swing-pilot",
    strategy_version: str = "pilot-1",
    max_order_notional: Decimal = PILOT_MAX_ORDER_NOTIONAL,
    max_positions: int = PILOT_MAX_POSITIONS,
) -> PilotOrderPlan:
    """Build a pretrade-validated pilot order under tightened caps; never submits.

    Raises ``ValueError`` if any independent pretrade limit is breached (notional cap,
    already-open position, per-position risk, daily loss). Returns the validated plan.
    """
    if type(side) is not Side:
        raise ValueError("side must be an exact Side")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive plain int")
    _positive_decimal(limit_price, "limit_price")
    _positive_decimal(equity, "equity")
    if type(open_positions) is not int or open_positions < 0:
        raise ValueError("open_positions must be a nonnegative plain int")
    if type(initial_stop_pct) is not Decimal or not (Decimal("0") < initial_stop_pct < Decimal("1")):
        raise ValueError("initial_stop_pct must be a Decimal in (0, 1)")
    if type(daily_loss) is not Decimal or not daily_loss.is_finite() or daily_loss < 0:
        raise ValueError("daily_loss must be a nonnegative finite Decimal")

    intent = LiveOrderIntent(
        strategy=strategy, strategy_version=strategy_version, signal_date=signal_date,
        symbol=symbol, classification=classification, side=side, quantity=quantity,
        limit_price=limit_price, order_mode=OrderMode.LIMIT,
    )
    proposed_position_risk = intent.notional * initial_stop_pct
    snapshot = AccountRiskSnapshot(
        planned_or_open_positions=open_positions, equity=equity,
        daily_loss=daily_loss, proposed_position_risk=proposed_position_risk,
    )
    limits = PretradeLimits(max_positions=max_positions, max_order_notional=max_order_notional)
    validate_pretrade(intent, snapshot, limits=limits)  # raises on any breach
    return PilotOrderPlan(intent=intent, snapshot=snapshot, limits=limits)


def build_pilot_exit(
    *,
    symbol: str,
    quantity: int,
    limit_price: Decimal,
    signal_date: date,
    equity: Decimal,
    open_positions: int,
    classification: str = "STOCK",
    strategy: str = "krx-swing-pilot",
    strategy_version: str = "pilot-1",
) -> PilotOrderPlan:
    """Build a validated SELL exit. Unlike an entry, an exit REDUCES risk, so it must not
    be blocked by the entry caps (positions / notional / daily-loss). The snapshot/limits
    are therefore set so a legitimate full exit always validates: no new position risk,
    the position count taken as post-exit, and the notional cap set to this order's own
    notional. Safety for exits comes from kill-switch, market hours, audit, and holding
    the shares — enforced by the caller and the submitter."""
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive plain int")
    _positive_decimal(limit_price, "limit_price")
    _positive_decimal(equity, "equity")
    if type(open_positions) is not int or open_positions <= 0:
        raise ValueError("open_positions must be a positive plain int for an exit")
    intent = LiveOrderIntent(
        strategy=strategy, strategy_version=strategy_version, signal_date=signal_date,
        symbol=symbol, classification=classification, side=Side.SELL, quantity=quantity,
        limit_price=limit_price, order_mode=OrderMode.LIMIT,
    )
    snapshot = AccountRiskSnapshot(
        planned_or_open_positions=max(0, open_positions - 1), equity=equity,
        daily_loss=Decimal("0"), proposed_position_risk=Decimal("0"),
    )
    limits = PretradeLimits(max_positions=max(1, open_positions), max_order_notional=intent.notional)
    validate_pretrade(intent, snapshot, limits=limits)
    return PilotOrderPlan(intent=intent, snapshot=snapshot, limits=limits)


def describe_pilot_plan(plan: PilotOrderPlan, *, account_number: str) -> str:
    """Render the exact wire body + TR id the submitter would send. Submits nothing."""
    if type(plan) is not PilotOrderPlan:
        raise ValueError("plan must be a PilotOrderPlan")
    # Reuse the submitter's own capture so the preview is byte-identical to a real send.
    from .production_execution import _capture_cash_limit_action

    action = _capture_cash_limit_action(plan.intent, account_number)
    lines = [
        "=== LIVE PILOT PREFLIGHT (DRY-RUN — nothing is sent) ===",
        f"  account   : {account_number}",
        f"  side/TR   : {plan.intent.side.value} / {action.tr_id}",
        f"  symbol    : {plan.intent.symbol}  qty={plan.intent.quantity}  limit={plan.intent.limit_price}",
        f"  notional  : {plan.notional}  (pilot cap {plan.limits.max_order_notional})",
        f"  limits    : max_positions={plan.limits.max_positions}",
        f"  intent_id : {plan.intent.intent_id}",
        "  wire body :",
    ]
    for key, value in action.wire_body().items():
        lines.append(f"      {key} = {value}")
    lines.append("  To actually place this order, run `submit --arm` with the operator confirmation phrase.")
    return "\n".join(lines) + "\n"


def submit_pilot_order(plan: PilotOrderPlan, *, client, operator_confirmation: str, kill_switch_path):
    """Submit a built plan through the existing gated production client.

    Fail-closed order of checks before any network call: (1) the manual kill switch at
    ``kill_switch_path`` must be disengaged (else raise ``LiveTradingHalted``); (2)
    ``operator_confirmation`` must equal the gate's required phrase (else the gate
    raises). Returns the broker acknowledgement on success.
    """
    if type(plan) is not PilotOrderPlan:
        raise ValueError("plan must be a PilotOrderPlan")
    require_not_halted(kill_switch_path)  # fail-closed: an engaged/corrupt switch blocks the order
    config = LiveExecutionConfig(live_trading_enabled=True, operator_confirmation=operator_confirmation)
    return client.submit_cash_limit_order(
        config=config, intent=plan.intent, snapshot=plan.snapshot, limits=plan.limits,
    )
