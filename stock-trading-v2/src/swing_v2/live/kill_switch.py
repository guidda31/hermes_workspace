"""Live-trading manual kill switch — a fail-closed halt checked before any real order.

Reuses the durable, idempotent file marker from ``paper.kill_switch`` (a malformed or
present marker fails CLOSED = halted). Adds ``require_not_halted``, which the pilot calls
before submitting: if the switch is engaged it raises ``LiveTradingHalted`` and no order
is placed. Engage/release are manual operator actions. This module opens no network and
places no order — it only records and checks the flag.
"""

from __future__ import annotations

from pathlib import Path

from ..paper.kill_switch import (
    engage_kill_switch,
    is_kill_switch_engaged,
    read_kill_switch,
    release_kill_switch,
)

__all__ = [
    "DEFAULT_LIVE_KILL_SWITCH",
    "LiveTradingHalted",
    "engage_kill_switch",
    "is_kill_switch_engaged",
    "read_kill_switch",
    "release_kill_switch",
    "require_not_halted",
]

DEFAULT_LIVE_KILL_SWITCH = "data/live-kill-switch.json"


class LiveTradingHalted(RuntimeError):
    """The manual kill switch is engaged; no live order may be placed."""


def require_not_halted(path: str | Path) -> None:
    """Raise ``LiveTradingHalted`` if the kill switch at ``path`` is engaged (fail-closed)."""
    if is_kill_switch_engaged(path):
        marker = read_kill_switch(path) or {}
        reason = marker.get("reason", "<engaged>")
        raise LiveTradingHalted(f"live trading is HALTED by the kill switch: {reason}")
