"""Local audit records for validated non-submitting order intents.

The SHA-256 digest detects accidental corruption and binds the serialized payload. It
is not a secret signature and cannot prevent an actor with the audit owner's delete
permission from deleting, recreating, and re-digesting a record. Trusted append-only
storage requires an external signing service or an immutable/append-only filesystem.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading

from .intent import LiveOrderIntent, OrderMode, Side, compute_canonical_intent_id


_RECORD_NAME = re.compile(r"intent-([0-9a-f]{64})\.json\Z")
_RECORD_KEYS = frozenset({"format_version", "intent", "intent_id", "digest"})
_INTENT_KEYS = frozenset({"strategy", "strategy_version", "signal_date", "symbol", "classification", "side", "quantity", "limit_price", "order_mode"})
_HALT_LOCKS_GUARD = threading.Lock()
_HALT_LOCKS: dict[tuple[int, int, str], threading.RLock] = {}
_HALT_LOCK_FDS_GUARD = threading.Lock()
_HALT_LOCK_FDS: set[int] = set()
_HALT_LOCK_OWNER_PID = os.getpid()
_HALT_LOCK_STATE = threading.local()


def _reset_account_halt_lock_process_state() -> None:
    """Discard lock state inherited across fork without unlocking the parent's flock."""
    global _HALT_LOCKS_GUARD, _HALT_LOCKS, _HALT_LOCK_FDS_GUARD, _HALT_LOCK_FDS
    global _HALT_LOCK_OWNER_PID, _HALT_LOCK_STATE
    # Closing an inherited FD drops only this process's reference.  Do not call
    # LOCK_UN: the inherited FD has the parent's open file description and could
    # release the parent's flock.
    for descriptor in _HALT_LOCK_FDS:
        try:
            os.close(descriptor)
        except OSError:
            pass
    _HALT_LOCKS_GUARD = threading.Lock()
    _HALT_LOCKS = {}
    _HALT_LOCK_FDS_GUARD = threading.Lock()
    _HALT_LOCK_FDS = set()
    _HALT_LOCK_OWNER_PID = os.getpid()
    _HALT_LOCK_STATE = threading.local()


def _ensure_account_halt_lock_process_state() -> None:
    """Make fork safety independent of whether an at-fork hook was installed."""
    if _HALT_LOCK_OWNER_PID != os.getpid():
        _reset_account_halt_lock_process_state()


def _lock_account_halt_fds_before_fork() -> None:
    """Keep the inherited-FD registry stable while Python forks this process."""
    _HALT_LOCK_FDS_GUARD.acquire()


def _unlock_account_halt_fds_after_parent_fork() -> None:
    _HALT_LOCK_FDS_GUARD.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_lock_account_halt_fds_before_fork,
        after_in_parent=_unlock_account_halt_fds_after_parent_fork,
        after_in_child=_reset_account_halt_lock_process_state,
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class _IntentSnapshot:
    """Canonical primitive values captured before the audit creation window."""

    strategy: str
    strategy_version: str
    signal_date: str
    symbol: str
    classification: str
    side: str
    quantity: int
    limit_price: str
    order_mode: str
    intent_id: str


def _capture_intent_snapshot(intent: LiveOrderIntent) -> _IntentSnapshot:
    """Validate and canonicalize an intent without retaining mutable object state."""
    strategy = intent.strategy
    strategy_version = intent.strategy_version
    signal_date = intent.signal_date
    symbol = intent.symbol
    classification = intent.classification
    side = intent.side
    quantity = intent.quantity
    limit_price = intent.limit_price
    order_mode = intent.order_mode
    canonical_intent_id = compute_canonical_intent_id(
        strategy, strategy_version, signal_date, symbol, classification, side,
        quantity, limit_price, order_mode,
    )
    if type(intent.intent_id) is not str or type(canonical_intent_id) is not str:
        raise ValueError("intent audit identity mismatch")
    if intent.intent_id != canonical_intent_id:
        raise ValueError("intent audit identity mismatch")
    return _IntentSnapshot(
        strategy=strategy,
        strategy_version=strategy_version,
        signal_date=signal_date.isoformat(),
        symbol=symbol,
        classification=classification,
        side=side.value,
        quantity=quantity,
        limit_price=format(limit_price.normalize(), "f"),
        order_mode=order_mode.value,
        intent_id=canonical_intent_id,
    )


def _intent_payload(snapshot: _IntentSnapshot) -> dict[str, object]:
    return {
        "strategy": snapshot.strategy, "strategy_version": snapshot.strategy_version,
        "signal_date": snapshot.signal_date, "symbol": snapshot.symbol,
        "classification": snapshot.classification, "side": snapshot.side,
        "quantity": snapshot.quantity, "limit_price": snapshot.limit_price,
        "order_mode": snapshot.order_mode,
    }


def _secure_root_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("audit root must be an existing absolute non-symlink directory")
    if metadata.st_uid != os.getuid():
        raise ValueError("audit root must be owned by the current user")
    if metadata.st_mode & 0o022:
        raise ValueError("audit root must not be group/world writable")
    if metadata.st_nlink < 2:
        raise ValueError("audit root has invalid link count")


def _safe_root(root: str | Path) -> Path:
    path = Path(root)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("audit root must be an existing absolute non-symlink directory")
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValueError("audit root must be an existing absolute non-symlink directory") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("audit root must be an existing absolute non-symlink directory")
    _secure_root_metadata(metadata)
    return path


def _open_secure_root(root: Path) -> int:
    _safe_root(root)
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise ValueError("audit root must be an existing absolute non-symlink directory") from exc
    try:
        _secure_root_metadata(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _secure_record_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise ValueError("audit record metadata is unsafe")
    if metadata.st_mode & 0o077:
        raise ValueError("audit record metadata is unsafe")


def _safe_record_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("audit record path is unsafe")
    if not _RECORD_NAME.fullmatch(candidate.name):
        raise ValueError("audit record filename is malformed")
    return candidate


class IntentAuditWriter:
    """Write records relative to a verified, opened audit-root descriptor.

    The descriptor prevents a root-path symlink swap from redirecting a write. The
    path's device/inode is also checked before and after each write, so a replaced
    root is rejected even though the anchored descriptor remains safe.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = _safe_root(root)
        self._root_fd = _open_secure_root(self._root)
        metadata = os.fstat(self._root_fd)
        self._root_identity = (metadata.st_dev, metadata.st_ino)
        # Test seam: production callers leave this unset. It makes the swap window deterministic.
        self._before_create: Callable[[], None] | None = None
        # Test seam: invoked while the account lock is held after the first halt check.
        self._after_first_halt_check: Callable[[], None] | None = None

    def close(self) -> None:
        descriptor = getattr(self, "_root_fd", -1)
        if descriptor >= 0:
            os.close(descriptor)
            self._root_fd = -1

    def __del__(self) -> None:
        self.close()

    def _assert_root_identity(self) -> None:
        current = _safe_root(self._root)
        metadata = os.lstat(current)
        if (metadata.st_dev, metadata.st_ino) != self._root_identity:
            raise ValueError("audit root identity changed after writer creation")
        _secure_root_metadata(os.fstat(self._root_fd))

    def write(self, intent: LiveOrderIntent) -> Path:
        if type(intent) is not LiveOrderIntent:
            raise ValueError("intent must be an exact LiveOrderIntent")
        snapshot = _capture_intent_snapshot(intent)
        self._assert_root_identity()
        if self._before_create is not None:
            self._before_create()
        if _capture_intent_snapshot(intent) != snapshot:
            raise ValueError("intent audit integrity changed before record creation")
        filename = f"intent-{snapshot.intent_id}.json"
        payload = {"format_version": 1, "intent": _intent_payload(snapshot), "intent_id": snapshot.intent_id}
        record = dict(payload, digest="sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest())
        encoded = _canonical_json(record) + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(filename, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError as exc:
            raise FileExistsError("intent audit record already exists") from exc
        try:
            os.fchmod(descriptor, 0o600)
            _secure_record_metadata(os.fstat(descriptor))
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(self._root_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._assert_root_identity()
        return self._root / filename

    def _ambiguous_halt_binding(self, account_number: object) -> str:
        """Return a non-secret account/root binding; never serialize the account itself."""
        if type(account_number) is not str or not account_number:
            raise ValueError("account number must be a nonempty plain str")
        self._assert_root_identity()
        return hashlib.sha256(_canonical_json({
            "account_number": account_number,
            "root_device": self._root_identity[0],
            "root_inode": self._root_identity[1],
        })).hexdigest()

    def _open_halt_lock(self, binding: str) -> int:
        """Open the hashed account lock through the trusted audit-root descriptor."""
        filename = f"account-lock-{binding}.lock"
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        created = False
        try:
            descriptor = os.open(filename, flags, dir_fd=self._root_fd)
        except FileNotFoundError:
            try:
                descriptor = os.open(filename, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._root_fd)
                created = True
            except FileExistsError:
                try:
                    descriptor = os.open(filename, flags, dir_fd=self._root_fd)
                except OSError as exc:
                    raise ValueError("ambiguous order halt lock is unsafe") from exc
            except OSError as exc:
                raise ValueError("ambiguous order halt lock is unsafe") from exc
        except OSError as exc:
            raise ValueError("ambiguous order halt lock is unsafe") from exc
        try:
            if created:
                os.fchmod(descriptor, 0o600)
            _secure_record_metadata(os.fstat(descriptor))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise ValueError("ambiguous order halt lock is unsafe")

    @contextmanager
    def _account_halt_lock(self, account_number: object):
        """Serialize one account's halt check, audit, POST, and halt persistence.

        The lock filename uses only a root-bound digest.  It is owner-only, opened
        relative to the verified root descriptor, and guarded by flock across
        processes.  Same-thread nesting is deliberately reentrant so marker writes
        inside a submit transaction use this exact lock rather than a second lock.
        """
        _ensure_account_halt_lock_process_state()
        binding = self._ambiguous_halt_binding(account_number)
        key = (*self._root_identity, binding)
        with _HALT_LOCKS_GUARD:
            process_lock = _HALT_LOCKS.setdefault(key, threading.RLock())
        process_lock.acquire()
        active = getattr(_HALT_LOCK_STATE, "active", set())
        nested = key in active
        descriptor = -1
        try:
            if not nested:
                active = set(active)
                active.add(key)
                _HALT_LOCK_STATE.active = active
                with _HALT_LOCK_FDS_GUARD:
                    descriptor = self._open_halt_lock(binding)
                    _HALT_LOCK_FDS.add(descriptor)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                except OSError as exc:
                    raise ValueError("ambiguous order halt lock acquisition failed") from exc
            yield
        finally:
            if not nested:
                active = set(getattr(_HALT_LOCK_STATE, "active", set()))
                active.discard(key)
                _HALT_LOCK_STATE.active = active
                if descriptor >= 0:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        with _HALT_LOCK_FDS_GUARD:
                            try:
                                os.close(descriptor)
                            finally:
                                _HALT_LOCK_FDS.discard(descriptor)
            process_lock.release()

    def _has_ambiguous_halt(self, account_number: object) -> bool:
        """Fail closed when this account/root has a durable ambiguous-order marker."""
        binding = self._ambiguous_halt_binding(account_number)
        filename = f"ambiguous-halt-{binding}.json"
        descriptor = -1
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(filename, flags, dir_fd=self._root_fd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ValueError("ambiguous order halt marker is unsafe") from exc
        try:
            _secure_record_metadata(os.fstat(descriptor))
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                marker = json.loads(handle.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("ambiguous order halt marker is malformed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if marker != {"format_version": 1, "binding": binding, "state": "AMBIGUOUS"}:
            raise ValueError("ambiguous order halt marker is malformed")
        return True

    def _record_ambiguous_halt(self, account_number: object) -> None:
        """Atomically persist a fail-closed reconciliation halt without secrets.

        There intentionally is no clear operation: later reconciliation must provide a
        separately privileged release mechanism rather than retrying this client.
        """
        with self._account_halt_lock(account_number):
            self._record_ambiguous_halt_locked(account_number)

    def _record_ambiguous_halt_locked(self, account_number: object) -> None:
        binding = self._ambiguous_halt_binding(account_number)
        filename = f"ambiguous-halt-{binding}.json"
        encoded = _canonical_json({"format_version": 1, "binding": binding, "state": "AMBIGUOUS"}) + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            try:
                descriptor = os.open(filename, flags, 0o600, dir_fd=self._root_fd)
            except FileExistsError:
                if not self._has_ambiguous_halt(account_number):
                    raise ValueError("ambiguous order halt marker collision")
                return
            os.fchmod(descriptor, 0o600)
            _secure_record_metadata(os.fstat(descriptor))
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(self._root_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._assert_root_identity()


def load_audit_record(path: str | Path) -> dict[str, object]:
    record_path = _safe_record_path(path)
    root_fd = _open_secure_root(record_path.parent)
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(record_path.name, flags, dir_fd=root_fd)
        _secure_record_metadata(os.fstat(descriptor))
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = json.loads(handle.read().decode("utf-8"))
    except ValueError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed intent audit record") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_fd)
    if not isinstance(raw, Mapping) or set(raw) != _RECORD_KEYS or raw.get("format_version") != 1:
        raise ValueError("malformed intent audit record")
    intent_raw = raw["intent"]
    if not isinstance(intent_raw, Mapping) or set(intent_raw) != _INTENT_KEYS or type(raw["intent_id"]) is not str or type(raw["digest"]) is not str:
        raise ValueError("malformed intent audit record")
    payload = {"format_version": 1, "intent": dict(intent_raw), "intent_id": raw["intent_id"]}
    expected_digest = "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()
    if raw["digest"] != expected_digest:
        raise ValueError("intent audit digest mismatch")
    try:
        intent = LiveOrderIntent(
            strategy=intent_raw["strategy"], strategy_version=intent_raw["strategy_version"],
            signal_date=date.fromisoformat(intent_raw["signal_date"]), symbol=intent_raw["symbol"],
            classification=intent_raw["classification"], side=Side(intent_raw["side"]),
            quantity=intent_raw["quantity"], limit_price=Decimal(intent_raw["limit_price"]),
            order_mode=OrderMode(intent_raw["order_mode"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed intent audit record") from exc
    name_match = _RECORD_NAME.fullmatch(record_path.name)
    if name_match is None or intent.intent_id != raw["intent_id"] or name_match.group(1) != intent.intent_id:
        raise ValueError("intent audit identity mismatch")
    return dict(raw)
