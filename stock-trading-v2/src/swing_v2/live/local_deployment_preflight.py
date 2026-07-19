"""Read-only local diagnosis adapter for the Phase 1 deployment preflight CLI.

It deliberately cannot collect systemd-resolved effective configuration evidence, so
its inspection is never eligible for operator review.
"""
from __future__ import annotations

import base64
import binascii
import grp
import hashlib
import os
from pathlib import Path
import pwd
import stat
from typing import Final

from swing_v2.live.deployment_preflight import DeploymentInspection, FileMetadata, KeyDeclaration, Principal, ServiceUnit

_AUTHORITY: Final = "kis-nonce-authority"
_EXECUTOR: Final = "kis-executor"
_SOCKET_GROUP: Final = "kis-authority-clients"
_PUBLIC_KEY_NAME: Final = "authority-ed25519.pub"
_MAX_PUBLIC_KEY_BYTES: Final = 4096


def _principal(name: str) -> Principal | None:
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    supplementary = frozenset(item.gr_gid for item in grp.getgrall() if name in item.gr_mem)
    return Principal(entry.pw_name, entry.pw_uid, entry.pw_gid, supplementary)


def _metadata(path: Path) -> FileMetadata:
    try:
        value = path.lstat()
    except OSError:
        return FileMetadata("missing", 0, 0, 0, False, 0, False)
    mode = stat.S_IMODE(value.st_mode)
    kind = "directory" if stat.S_ISDIR(value.st_mode) else "regular" if stat.S_ISREG(value.st_mode) else "socket" if stat.S_ISSOCK(value.st_mode) else "other"
    return FileMetadata(kind, value.st_uid, value.st_gid, mode, stat.S_ISLNK(value.st_mode), value.st_nlink, True)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode, left.st_uid, left.st_gid, left.st_nlink, left.st_size) == (right.st_dev, right.st_ino, right.st_mode, right.st_uid, right.st_gid, right.st_nlink, right.st_size)


def _safe_parent_stat(value: os.stat_result) -> bool:
    return stat.S_ISDIR(value.st_mode) and not stat.S_ISLNK(value.st_mode) and stat.S_IMODE(value.st_mode) & 0o022 == 0


def _read_anchored(parent: Path, filename: str, max_bytes: int) -> bytes | None:
    """Read a bounded regular file by anchored descriptors, or return no evidence."""
    if not parent.is_absolute() or type(filename) is not str or filename in {"", ".", ".."} or "/" in filename or "\x00" in filename or type(max_bytes) is not int or not 0 <= max_bytes <= _MAX_PUBLIC_KEY_BYTES:
        return None
    try:
        pre = os.lstat(parent)
        if not _safe_parent_stat(pre):
            return None
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(parent, directory_flags)
    except OSError:
        return None
    try:
        opened_parent = os.fstat(directory_fd)
        if not _safe_parent_stat(opened_parent) or not _same_identity(pre, opened_parent):
            return None
        try:
            child_fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        except OSError:
            return None
        try:
            before = os.fstat(child_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o022 or before.st_size > max_bytes:
                return None
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(child_fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(child_fd)
            post_parent = os.fstat(directory_fd)
            post_path = os.lstat(parent)
            if len(data) > max_bytes or not _same_identity(before, after) or not _same_identity(opened_parent, post_parent) or not _same_identity(pre, post_path):
                return None
            return data
        finally:
            os.close(child_fd)
    except OSError:
        return None
    finally:
        os.close(directory_fd)


def _is_ed25519_public_key(data: bytes) -> bool:
    if type(data) is not bytes or not 1 <= len(data) <= _MAX_PUBLIC_KEY_BYTES:
        return False
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return False
    parts = text.rstrip("\n").split(" ")
    if len(parts) not in {2, 3} or parts[0] != "ssh-ed25519" or not parts[1] or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for character in parts[1]):
        return False
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error):
        return False
    return len(blob) == 51 and blob[:15] == b"\x00\x00\x00\x0bssh-ed25519" and blob[15:19] == b"\x00\x00\x00\x20"


def _safe_public_key(parent: Path, filename: str) -> KeyDeclaration | None:
    if filename != _PUBLIC_KEY_NAME:
        return None
    data = _read_anchored(parent, filename, _MAX_PUBLIC_KEY_BYTES)
    if data is None or not _is_ed25519_public_key(data):
        return None
    return KeyDeclaration(True, hashlib.sha256(data).hexdigest())


def _unit(name: str, user: str, path: Path) -> ServiceUnit | None:
    """Only diagnostic presence: never parse a base unit as effective systemd config."""
    if path.name != name or not path.is_absolute():
        return None
    # A safe bounded read establishes only that a local template is inspectable; no content
    # is parsed or reported, because drop-ins, resets, continuations and prefixes need a
    # separately approved root-owned effective-config collector.
    if _read_anchored(path.parent, path.name, _MAX_PUBLIC_KEY_BYTES) is None:
        return None
    return ServiceUnit(name, str(path), user, user, "", False, frozenset(), False, None)


def local_inspection() -> DeploymentInspection:
    """Inspect conventional metadata only; no systemctl, shell, secrets, or network."""
    operator_entry = pwd.getpwuid(os.getuid())
    operator = _principal(operator_entry.pw_name)
    authority, executor, socket_group = _principal(_AUTHORITY), _principal(_EXECUTOR), _principal(_SOCKET_GROUP)
    socket_members = frozenset() if socket_group is None else frozenset(next((item.gr_mem for item in grp.getgrall() if item.gr_gid == socket_group.gid), ()))
    authority_unit = _unit("kis-nonce-authority.service", _AUTHORITY, Path("/etc/systemd/system/kis-nonce-authority.service"))
    executor_unit = _unit("kis-executor.service", _EXECUTOR, Path("/etc/systemd/system/kis-executor.service"))
    authority_key = _safe_public_key(Path("/var/lib/kis-nonce-authority/public"), _PUBLIC_KEY_NAME)
    executor_key = _safe_public_key(Path("/etc/kis-executor/public"), _PUBLIC_KEY_NAME)
    return DeploymentInspection(operator, authority, executor, authority_unit, executor_unit, _metadata(Path("/var/lib/kis-nonce-authority/private")), _metadata(Path("/var/lib/kis-nonce-authority/private/authority-ed25519.key")), _metadata(Path("/var/lib/kis-nonce-authority/consumed.sqlite3")), _metadata(Path("/etc/kis-executor/public")), _metadata(Path("/etc/kis-executor/public/authority-ed25519.pub")), authority_key or KeyDeclaration(False, None), executor_key or KeyDeclaration(False, None), _metadata(Path("/run/kis-nonce-authority")), _metadata(Path("/run/kis-nonce-authority/authority.sock")), socket_group, socket_members, authority_unit is not None and executor_unit is not None, frozenset(), False, False)
