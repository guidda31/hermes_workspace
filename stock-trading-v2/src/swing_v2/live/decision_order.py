"""Bridge the AI's forward-observation decision into a live pilot order.

The stock is chosen by the AI (the LLM decision layer records BUY/SELL/HOLD with a
target_weight in data/forward-records/); this module turns an admitted BUY decision into
a sized, pretrade-validated PilotOrderPlan so the operator never hand-picks a symbol.
Sizing: quantity = target_weight * equity / limit_price, then CLAMPED so the notional
never exceeds the tiny pilot cap. Reading is pure JSON; no order is placed here.
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path


def latest_record_path(records_dir) -> Path | None:
    """Newest data/forward-records/signal-*.json by filename, or None if none exist."""
    paths = sorted(Path(records_dir).glob("signal-*.json"))
    return paths[-1] if paths else None


def load_record(path) -> dict:
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(record, dict) or "decisions" not in record or "admitted_symbols" not in record:
        raise ValueError("not a forward-observation record (missing decisions/admitted_symbols)")
    return record


def admitted_buy_decisions(record: dict) -> tuple[dict, ...]:
    """The AI's admitted BUY picks (action BUY and symbol in admitted_symbols)."""
    admitted = set(record.get("admitted_symbols", ()))
    return tuple(
        d for d in record.get("decisions", ())
        if d.get("action") == "BUY" and d.get("symbol") in admitted
    )


def snapshot_close(snapshot_path, symbol: str) -> Decimal:
    """Latest close for a symbol from a forward snapshot (a limit-price reference)."""
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    history = (snapshot.get("histories") or {}).get(symbol)
    if not isinstance(history, list) or not history:
        raise ValueError(f"snapshot has no price history for {symbol}")
    close = history[-1].get("close")
    try:
        price = Decimal(str(close))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"snapshot close for {symbol} is not a number") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError(f"snapshot close for {symbol} must be positive")
    return price


def sized_quantity(
    *, target_weight: Decimal, equity: Decimal, limit_price: Decimal, max_order_notional: Decimal
) -> tuple[int, bool]:
    """Shares from the AI's target weight, clamped to the pilot notional cap.

    Returns (quantity, clamped) where ``clamped`` is True when the target weight implied
    more shares than the pilot cap allows (so the pilot executes a smaller slice).
    """
    for value, name in ((target_weight, "target_weight"), (equity, "equity"),
                        (limit_price, "limit_price"), (max_order_notional, "max_order_notional")):
        if type(value) is not Decimal or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a positive finite Decimal")
    from_weight = int((target_weight * equity / limit_price).to_integral_value(rounding=ROUND_FLOOR))
    from_cap = int((max_order_notional / limit_price).to_integral_value(rounding=ROUND_FLOOR))
    quantity = min(from_weight, from_cap)
    return quantity, from_weight > from_cap
