"""Durable manual kill switch for paper trading (Phase-5 manual-halt gate).

A file-backed halt marker. When engaged, the caller/runner must drop new BUYs
(entries); SELL/exit orders stay allowed. This module only records and reports the
flag — it submits no order and opens no network connection.

The marker is engaged idempotently: re-engaging keeps the FIRST reason and time
(O_EXCL create, tolerate an existing marker). A present-but-malformed marker fails
CLOSED — ``is_kill_switch_engaged`` returns True for any existing marker file, because
halting on a corrupt flag is safer than trading through it.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path

KILL_SWITCH_SCHEMA_VERSION = 1


def engage_kill_switch(path: str | Path, *, reason: str, engaged_at: datetime) -> None:
    """Durably record the halt marker; idempotent, preserving the first engagement."""
    if type(reason) is not str or not reason.strip():
        raise ValueError("reason must be a nonempty plain str")
    if type(engaged_at) is not datetime or engaged_at.tzinfo is None:
        raise ValueError("engaged_at must be a timezone-aware datetime")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "schema_version": KILL_SWITCH_SCHEMA_VERSION,
        "reason": reason,
        "engaged_at": engaged_at.isoformat(),
    }
    payload = json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return  # already engaged; keep the original reason/time
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def is_kill_switch_engaged(path: str | Path) -> bool:
    """True iff a marker file exists. A malformed marker fails CLOSED (engaged=True)."""
    return Path(path).is_file()


def read_kill_switch(path: str | Path) -> dict | None:
    """Return the marker contents (reason, engaged_at) or None if not engaged.

    Fails closed: a present-but-corrupt marker is still engaged, so its reason/time are
    reported as unreadable rather than as absence. Use ``is_kill_switch_engaged`` for the
    authoritative gate decision.
    """
    destination = Path(path)
    if not destination.is_file():
        return None
    try:
        raw = json.loads(destination.read_text(encoding="utf-8"))
        reason = raw["reason"]
        engaged_at = datetime.fromisoformat(raw["engaged_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"reason": "<unreadable kill-switch marker>", "engaged_at": None}
    if type(reason) is not str or engaged_at.tzinfo is None:
        return {"reason": "<unreadable kill-switch marker>", "engaged_at": None}
    return {"reason": reason, "engaged_at": engaged_at}


def release_kill_switch(path: str | Path) -> None:
    """Deliberately remove the marker (manual re-enable). Absent marker is a no-op."""
    try:
        os.unlink(Path(path))
    except FileNotFoundError:
        return
