"""Durable, immutable persistence for paper-trading session results.

Each simulated session is written write-once (O_EXCL) to ``paper-<trade_date>.json`` as
canonical JSON carrying a SHA-256 integrity digest over its own bytes. Write-once per
trade_date is the duplicate-session / double-apply guard: re-running a day cannot silently
overwrite its ledger entry. Loads re-verify the digest and refuse tampered records.

Restart recovery reads the newest session's account back into real ``PaperAccount`` /
``PaperPosition`` objects, so a restarted paper run resumes from durable state rather than
in-memory state. Nothing here submits an order or opens a network connection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..backtest.engine import Fill
from .session import PaperAccount, PaperPosition, PaperSessionResult, UnfilledDecision

PAPER_LEDGER_SCHEMA_VERSION = 1

_FILENAME_RE = re.compile(r"^paper-(\d{4}-\d{2}-\d{2})\.json$")


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _canonical_bytes(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(obj: object) -> str:
    return hashlib.sha256(_canonical_bytes(obj)).hexdigest()


def _serialize_position(position: PaperPosition) -> dict[str, object]:
    return {
        "symbol": position.symbol,
        "asset_type": position.asset_type,
        "entry_price": _canonical_decimal(position.entry_price),
        "quantity": position.quantity,
        "entry_date": position.entry_date.isoformat(),
    }


def _serialize_account(account: PaperAccount) -> dict[str, object]:
    return {
        "cash": _canonical_decimal(account.cash),
        "positions": [_serialize_position(p) for p in account.positions],
    }


def _serialize_fill(fill: Fill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": fill.quantity,
        "fill_price": _canonical_decimal(fill.fill_price),
        "cash_delta": _canonical_decimal(fill.cash_delta),
        "commission": _canonical_decimal(fill.commission),
        "sell_tax": _canonical_decimal(fill.sell_tax),
        "total_cost": _canonical_decimal(fill.total_cost),
        "reference_open": _canonical_decimal(fill.reference_open),
    }


def _serialize_unfilled(unfilled: UnfilledDecision) -> dict[str, object]:
    return {"symbol": unfilled.symbol, "side": unfilled.side, "reason": unfilled.reason}


def _build_record(result: PaperSessionResult) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": PAPER_LEDGER_SCHEMA_VERSION,
        "trade_date": result.trade_date.isoformat(),
        "account": _serialize_account(result.account),
        "fills": [_serialize_fill(f) for f in result.fills],
        "unfilled": [_serialize_unfilled(u) for u in result.unfilled],
        "realized_pnl": _canonical_decimal(result.realized_pnl),
        "nav": _canonical_decimal(result.nav),
    }
    record["integrity"] = {"algorithm": "sha256", "digest": _digest(record)}
    return record


def save_paper_session(session_dir: str | Path, result: PaperSessionResult) -> Path:
    """Write ``result`` write-once as canonical JSON; refuse to overwrite the trade_date."""
    if not isinstance(result, PaperSessionResult):
        raise ValueError("result must be a PaperSessionResult")
    directory = Path(session_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"paper-{result.trade_date.isoformat()}.json"
    payload = json.dumps(_build_record(result), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"paper session already exists and is immutable: {destination}") from exc
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return destination


def load_paper_session(path: str | Path) -> dict[str, object]:
    """Read a session record and re-verify its integrity digest before returning it."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("paper session must be readable JSON") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("integrity"), dict):
        raise ValueError("paper session is missing its integrity member")
    stored = raw["integrity"].get("digest")
    body = {k: v for k, v in raw.items() if k != "integrity"}
    if not isinstance(stored, str) or stored != _digest(body):
        raise ValueError("paper session integrity digest mismatch")
    return raw


def _account_from_record(record: dict[str, object]) -> PaperAccount:
    account = record["account"]
    positions = tuple(
        PaperPosition(
            symbol=p["symbol"],
            asset_type=p["asset_type"],
            entry_price=Decimal(p["entry_price"]),
            quantity=int(p["quantity"]),
            entry_date=date.fromisoformat(p["entry_date"]),
        )
        for p in account["positions"]
    )
    return PaperAccount(cash=Decimal(account["cash"]), positions=positions)


def _session_paths(session_dir: str | Path) -> list[Path]:
    directory = Path(session_dir)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if _FILENAME_RE.match(p.name))


def load_latest_account(session_dir: str | Path) -> PaperAccount | None:
    """Restart recovery: reconstruct the newest session's account, or None if there is none."""
    paths = _session_paths(session_dir)
    if not paths:
        return None
    newest = max(paths, key=lambda p: _FILENAME_RE.match(p.name).group(1))
    return _account_from_record(load_paper_session(newest))


def list_session_records(session_dir: str | Path) -> tuple[dict[str, object], ...]:
    """All session records, integrity-verified, sorted ascending by trade_date."""
    records = [load_paper_session(p) for p in _session_paths(session_dir)]
    return tuple(sorted(records, key=lambda r: r["trade_date"]))
