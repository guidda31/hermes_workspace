"""Disabled-by-default Phase 1 safety boundary for future live trading.

The process-local HMAC seal detects ordinary callers mutating a constructed config.
It is not authorization against arbitrary code execution in this Python process,
which can read private module memory. A future order submitter must be isolated in a
separately privileged process/service with OS credential separation and broker-side
limits; Phase 1 does not provide full live authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from .integrity import seal, verify

LIVE_OPERATOR_CONFIRMATION = "KIS_LIVE_TRADING_OPERATOR_CONFIRMED"


def _canonical_config_bytes(config: "LiveExecutionConfig") -> bytes:
    if type(config.live_trading_enabled) is not bool:
        raise ValueError("live_trading_enabled must be a plain bool")
    if config.operator_confirmation is not None and type(config.operator_confirmation) is not str:
        raise ValueError("operator_confirmation must be a plain str or None")
    return json.dumps({
        "live_trading_enabled": config.live_trading_enabled,
        "operator_confirmation": config.operator_confirmation,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


@dataclass(frozen=True)
class LiveExecutionConfig:
    live_trading_enabled: bool = False
    operator_confirmation: str | None = None
    _integrity_seal: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_integrity_seal", seal("LiveExecutionConfig", _canonical_config_bytes(self)))


def require_live_execution_enabled(config: LiveExecutionConfig) -> None:
    if type(config) is not LiveExecutionConfig:
        raise ValueError("config must be an exact LiveExecutionConfig")
    if not verify("LiveExecutionConfig", _canonical_config_bytes(config), config._integrity_seal):
        raise ValueError("config integrity seal mismatch")
    if config.live_trading_enabled is not True:
        raise ValueError("live execution is disabled")
    if config.operator_confirmation != LIVE_OPERATOR_CONFIRMATION:
        raise ValueError("live execution requires explicit operator confirmation")
