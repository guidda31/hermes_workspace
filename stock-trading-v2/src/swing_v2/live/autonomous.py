"""Bounded authorization + budgets for UNATTENDED (auto-armed) live trading.

Autonomous mode removes the human --arm/operator-confirm step, so the human circuit
breaker is replaced by an explicit, EXPIRING, BUDGETED authorization the operator writes
ONCE — plus every existing gate still applies (kill switch always overrides, pretrade
risk, tiny caps). Every check FAILS CLOSED: absent / disabled / expired / corrupt
authorization, an exhausted daily order-count or notional budget, or a request outside
KRX regular hours all block the order.

An `AutonomousAuthorization` is a JSON file the operator creates deliberately (it must
carry the exact confirmation phrase). A per-day order-budget ledger (one JSON line per
placed order) enforces "at most N orders and M notional per day" across cron firings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

_KST = timezone(timedelta(hours=9))
AUTONOMOUS_CONFIRMATION = "KIS_AUTONOMOUS_TRADING_OPERATOR_CONFIRMED"
DEFAULT_AUTH_FILE = "data/live-autonomous-auth.json"
DEFAULT_ORDER_BUDGET_DIR = "data/live-order-budget"
_KRX_OPEN = time(9, 0)
_KRX_CLOSE = time(15, 30)


class AutonomousBlocked(RuntimeError):
    """An unattended order is not authorized right now; fail-closed (no order placed)."""


@dataclass(frozen=True)
class AutonomousAuthorization:
    enabled: bool
    operator_confirmation: str
    expires_at: datetime
    max_orders_per_day: int
    max_notional_per_day: Decimal


def write_authorization(
    path, *, operator_confirmation: str, expires_at: datetime,
    max_orders_per_day: int, max_notional_per_day: Decimal,
) -> None:
    """Persist the operator's autonomous opt-in. Requires the exact confirmation phrase."""
    if operator_confirmation != AUTONOMOUS_CONFIRMATION:
        raise ValueError(f'operator_confirmation must equal "{AUTONOMOUS_CONFIRMATION}"')
    if type(expires_at) is not datetime or expires_at.tzinfo is None:
        raise ValueError("expires_at must be a timezone-aware datetime")
    if type(max_orders_per_day) is not int or max_orders_per_day <= 0:
        raise ValueError("max_orders_per_day must be a positive int")
    if type(max_notional_per_day) is not Decimal or not max_notional_per_day.is_finite() or max_notional_per_day <= 0:
        raise ValueError("max_notional_per_day must be a positive finite Decimal")
    payload = {
        "enabled": True,
        "operator_confirmation": operator_confirmation,
        "expires_at": expires_at.isoformat(),
        "max_orders_per_day": max_orders_per_day,
        "max_notional_per_day": format(max_notional_per_day.normalize(), "f"),
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def load_authorization(path) -> AutonomousAuthorization | None:
    """Return the authorization, or None if the file is absent. Corrupt -> raise (fail-closed)."""
    destination = Path(path)
    if not destination.exists():
        return None
    try:
        raw = json.loads(destination.read_text(encoding="utf-8"))
        auth = AutonomousAuthorization(
            enabled=raw["enabled"],
            operator_confirmation=raw["operator_confirmation"],
            expires_at=datetime.fromisoformat(raw["expires_at"]),
            max_orders_per_day=raw["max_orders_per_day"],
            max_notional_per_day=Decimal(str(raw["max_notional_per_day"])),
        )
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise AutonomousBlocked("autonomous authorization file is corrupt; halt and inspect it") from exc
    if type(auth.enabled) is not bool or type(auth.max_orders_per_day) is not int or auth.expires_at.tzinfo is None:
        raise AutonomousBlocked("autonomous authorization file is malformed")
    return auth


def is_krx_regular_session(now: datetime) -> bool:
    """True during KRX regular hours (Mon-Fri 09:00-15:30 KST). Holidays not modeled."""
    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    local = now.astimezone(_KST)
    if local.weekday() >= 5:
        return False
    return _KRX_OPEN <= local.timetz().replace(tzinfo=None) <= _KRX_CLOSE


def _budget_path(order_budget_dir, day: str) -> Path:
    datetime.strptime(day, "%Y-%m-%d")  # validate format (raises ValueError)
    return Path(order_budget_dir) / f"{day}.jsonl"


def record_order(order_budget_dir, *, day: str, symbol: str, notional: Decimal, at: datetime) -> None:
    """Append one placed-order entry to the day's budget ledger (called after a fill accept)."""
    if type(symbol) is not str or not symbol.strip():
        raise ValueError("symbol must be a nonempty plain str")
    if type(notional) is not Decimal or not notional.is_finite() or notional <= 0:
        raise ValueError("notional must be a positive finite Decimal")
    if type(at) is not datetime or at.tzinfo is None:
        raise ValueError("at must be a timezone-aware datetime")
    path = _budget_path(order_budget_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"day": day, "symbol": symbol,
                       "notional": format(notional.normalize(), "f"), "at": at.isoformat()},
                      ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def day_order_usage(order_budget_dir, day: str) -> tuple[int, Decimal]:
    """(orders_placed, total_notional) for the day. Corrupt ledger -> raise (fail-closed)."""
    path = _budget_path(order_budget_dir, day)
    if not path.exists():
        return 0, Decimal("0")
    count = 0
    total = Decimal("0")
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            total += Decimal(str(json.loads(raw)["notional"]))
            count += 1
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise AutonomousBlocked(f"order-budget ledger for {day} is corrupt; halt and inspect it") from exc
    return count, total


def require_autonomous_authorized(
    auth_path, *, now: datetime, orders_today: int, notional_today: Decimal, next_notional: Decimal,
) -> str:
    """Fail-closed gate for an unattended order. Returns the gate confirmation phrase or raises.

    Checks: authorization present + enabled + carries the exact phrase + not expired; the
    day's order-count and notional budgets accommodate this order. The kill switch and
    market-hours checks are enforced by the caller before this. The returned phrase is the
    LIVE gate's confirmation, so autonomy still passes through the same submit gate.
    """
    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    auth = load_authorization(auth_path)
    if auth is None:
        raise AutonomousBlocked("no autonomous authorization on file (operator opt-in required)")
    if auth.enabled is not True:
        raise AutonomousBlocked("autonomous authorization is disabled")
    if auth.operator_confirmation != AUTONOMOUS_CONFIRMATION:
        raise AutonomousBlocked("autonomous authorization lacks the exact operator confirmation")
    if now >= auth.expires_at:
        raise AutonomousBlocked(f"autonomous authorization expired at {auth.expires_at.isoformat()}")
    if orders_today >= auth.max_orders_per_day:
        raise AutonomousBlocked(f"daily order-count budget exhausted ({orders_today}/{auth.max_orders_per_day})")
    if notional_today + next_notional > auth.max_notional_per_day:
        raise AutonomousBlocked(
            f"daily notional budget exceeded ({notional_today}+{next_notional} > {auth.max_notional_per_day})")
    from .gate import LIVE_OPERATOR_CONFIRMATION
    return LIVE_OPERATOR_CONFIRMATION
