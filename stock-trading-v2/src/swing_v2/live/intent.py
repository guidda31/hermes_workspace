"""Typed, non-submitting live-order intent contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
import hashlib
import json


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderMode(str, Enum):
    LIMIT = "LIMIT"


_ALLOWED_CLASSIFICATIONS = frozenset({"STOCK", "DOMESTIC_INDEX_OR_SECTOR"})


def _require_plain_nonempty_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty plain str")
    return value


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def compute_canonical_intent_id(
    strategy: object,
    strategy_version: object,
    signal_date: object,
    symbol: object,
    classification: object,
    side: object,
    quantity: object,
    limit_price: object,
    order_mode: object,
) -> str:
    """Return the trusted ID from exact, independently validated primitives."""
    _require_plain_nonempty_string(strategy, "strategy")
    _require_plain_nonempty_string(strategy_version, "strategy_version")
    _require_plain_nonempty_string(symbol, "symbol")
    if type(signal_date) is not date:
        raise ValueError("signal_date must be a plain date")
    if type(classification) is not str or classification not in _ALLOWED_CLASSIFICATIONS:
        raise ValueError("classification is not explicitly allowed for live trading")
    if type(side) is not Side:
        raise ValueError("side must be Side")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive plain int")
    if type(limit_price) is not Decimal or not limit_price.is_finite() or limit_price <= 0:
        raise ValueError("limit_price must be a positive finite Decimal")
    if type(order_mode) is not OrderMode:
        raise ValueError("order_mode must be OrderMode")
    canonical = json.dumps({
        "strategy": strategy,
        "strategy_version": strategy_version,
        "signal_date": signal_date.isoformat(),
        "symbol": symbol,
        "classification": classification,
        "side": side.value,
        "quantity": quantity,
        "limit_price": _canonical_decimal(limit_price),
        "order_mode": order_mode.value,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LiveOrderIntent:
    strategy: str
    strategy_version: str
    signal_date: date
    symbol: str
    classification: str
    side: Side
    quantity: int
    limit_price: Decimal
    order_mode: OrderMode
    intent_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", compute_canonical_intent_id(
            self.strategy, self.strategy_version, self.signal_date, self.symbol,
            self.classification, self.side, self.quantity, self.limit_price, self.order_mode,
        ))

    @property
    def notional(self) -> Decimal:
        return self.limit_price * Decimal(self.quantity)

    def _compute_intent_id(self) -> str:
        return compute_canonical_intent_id(
            self.strategy, self.strategy_version, self.signal_date, self.symbol,
            self.classification, self.side, self.quantity, self.limit_price, self.order_mode,
        )
