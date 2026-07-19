"""Process-local HMAC seals for Phase 1 mutable-object tamper detection.

The seal detects post-construction field mutation by ordinary callers. It is not a
security boundary against arbitrary code execution in this Python process: such code
can read private module state. Live submission must eventually live in a separately
privileged process/service with OS credential separation and broker-side limits.
"""

from __future__ import annotations

import hmac
import secrets

_PROCESS_SEAL_KEY = secrets.token_bytes(32)


def seal(kind: str, canonical: bytes) -> bytes:
    """Create an in-memory seal; callers must never serialize or log it."""
    return hmac.digest(_PROCESS_SEAL_KEY, kind.encode("ascii") + b"\0" + canonical, "sha256")


def verify(kind: str, canonical: bytes, value: object) -> bool:
    return type(value) is bytes and hmac.compare_digest(value, seal(kind, canonical))
