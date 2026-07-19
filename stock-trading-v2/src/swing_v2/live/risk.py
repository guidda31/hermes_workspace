"""Fail-closed, injected-account pretrade risk limits; no broker access."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json

from .integrity import seal, verify
from .intent import LiveOrderIntent


_MAX_POSITIONS = 5
_MAX_POSITION_RISK_FRACTION = Decimal("0.01")
_MAX_DAILY_LOSS_FRACTION = Decimal("0.03")
_MAX_ORDER_NOTIONAL = Decimal("1000000")


def _positive_finite_decimal(value: object, field_name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a positive finite Decimal")
    return value


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _canonical_limits_bytes(limits: "PretradeLimits") -> bytes:
    if type(limits.max_positions) is not int or limits.max_positions <= 0:
        raise ValueError("max_positions must be a positive plain int")
    for value, name in ((limits.max_position_risk_fraction, "max_position_risk_fraction"),
                        (limits.max_daily_loss_fraction, "max_daily_loss_fraction")):
        _positive_finite_decimal(value, name)
        if value > Decimal("1"):
            raise ValueError(f"{name} must not exceed one")
    _positive_finite_decimal(limits.max_order_notional, "max_order_notional")
    return json.dumps({
        "max_positions": limits.max_positions,
        "max_position_risk_fraction": _canonical_decimal(limits.max_position_risk_fraction),
        "max_daily_loss_fraction": _canonical_decimal(limits.max_daily_loss_fraction),
        "max_order_notional": _canonical_decimal(limits.max_order_notional),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _canonical_snapshot_bytes(snapshot: "AccountRiskSnapshot") -> bytes:
    if type(snapshot.planned_or_open_positions) is not int or snapshot.planned_or_open_positions < 0:
        raise ValueError("planned_or_open_positions must be a nonnegative plain int")
    _positive_finite_decimal(snapshot.equity, "equity")
    for value, name in ((snapshot.daily_loss, "daily_loss"), (snapshot.proposed_position_risk, "proposed_position_risk")):
        if type(value) is not Decimal or not value.is_finite() or value < 0:
            raise ValueError(f"{name} must be a nonnegative finite Decimal")
    return json.dumps({
        "planned_or_open_positions": snapshot.planned_or_open_positions,
        "equity": _canonical_decimal(snapshot.equity),
        "daily_loss": _canonical_decimal(snapshot.daily_loss),
        "proposed_position_risk": _canonical_decimal(snapshot.proposed_position_risk),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


@dataclass(frozen=True)
class PretradeLimits:
    max_positions: int = _MAX_POSITIONS
    max_position_risk_fraction: Decimal = _MAX_POSITION_RISK_FRACTION
    max_daily_loss_fraction: Decimal = _MAX_DAILY_LOSS_FRACTION
    max_order_notional: Decimal = _MAX_ORDER_NOTIONAL
    _integrity_seal: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_integrity_seal", seal("PretradeLimits", _canonical_limits_bytes(self)))


@dataclass(frozen=True)
class AccountRiskSnapshot:
    planned_or_open_positions: int
    equity: Decimal
    daily_loss: Decimal
    proposed_position_risk: Decimal
    _integrity_seal: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_integrity_seal", seal("AccountRiskSnapshot", _canonical_snapshot_bytes(self)))


def _require_intact_snapshot(snapshot: AccountRiskSnapshot) -> None:
    if not verify("AccountRiskSnapshot", _canonical_snapshot_bytes(snapshot), snapshot._integrity_seal):
        raise ValueError("snapshot integrity seal mismatch")


def _require_intact_limits(limits: PretradeLimits) -> None:
    if not verify("PretradeLimits", _canonical_limits_bytes(limits), limits._integrity_seal):
        raise ValueError("limits integrity seal mismatch")


def _trusted_intent_notional(intent: LiveOrderIntent) -> Decimal:
    """Recompute notional from exact-type canonical fields, never an overrideable proxy."""
    if type(intent) is not LiveOrderIntent:
        raise ValueError("intent must be an exact LiveOrderIntent")
    if type(intent.quantity) is not int or intent.quantity <= 0:
        raise ValueError("intent quantity is malformed")
    if type(intent.limit_price) is not Decimal or not intent.limit_price.is_finite() or intent.limit_price <= 0:
        raise ValueError("intent limit_price is malformed")
    return intent.limit_price * Decimal(intent.quantity)


def validate_pretrade(intent: LiveOrderIntent, snapshot: AccountRiskSnapshot, *, limits: PretradeLimits | None = None) -> None:
    """Admit only intents within independent local risk limits."""
    notional = _trusted_intent_notional(intent)
    if type(snapshot) is not AccountRiskSnapshot:
        raise ValueError("snapshot must be an exact AccountRiskSnapshot")
    active_limits = PretradeLimits() if limits is None else limits
    if type(active_limits) is not PretradeLimits:
        raise ValueError("limits must be an exact PretradeLimits")
    _require_intact_snapshot(snapshot)
    _require_intact_limits(active_limits)
    if snapshot.planned_or_open_positions >= active_limits.max_positions:
        raise ValueError("maximum planned/open positions reached")
    if snapshot.proposed_position_risk > snapshot.equity * active_limits.max_position_risk_fraction:
        raise ValueError("per-position risk limit exceeded")
    if snapshot.daily_loss > snapshot.equity * active_limits.max_daily_loss_fraction:
        raise ValueError("daily loss limit exceeded")
    if notional <= 0 or notional > active_limits.max_order_notional:
        raise ValueError("order notional cap exceeded")
