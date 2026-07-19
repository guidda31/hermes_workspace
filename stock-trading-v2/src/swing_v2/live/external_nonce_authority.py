"""Inactive Phase-1 durable external nonce-consumption authority template.

This module deliberately starts no listener and contains no broker, order, or HTTP code.
Deployment must provide a distinct authority OS principal, an injected 32-byte Ed25519 private seed,
and an AF_UNIX peer-authenticated caller.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import struct
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from swing_v2.live.privileged_protocol import (
    ExternalNonceConsumptionReceipt,
    ExternalNonceConsumptionRequest,
)

_MAX_REQUEST_BYTES = 16 * 1024
_HEX64 = frozenset("0123456789abcdef")


def _plain_string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} is malformed")
    return value


def _digest(value: object, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in _HEX64 for character in value):
        raise ValueError(f"{name} is malformed")
    return value


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise ValueError(f"{name} is malformed")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass(frozen=True, slots=True)
class AuthorityIdentity:
    """Immutable authority identity; socket_path is deployment metadata, not a listener."""
    service_id: str
    service_version: int
    executor_uids: frozenset[int]
    socket_path: str

    def __post_init__(self) -> None:
        _plain_string(self.service_id, "service_id")
        if type(self.service_version) is not int or self.service_version <= 0:
            raise ValueError("service_version is malformed")
        if type(self.executor_uids) is not frozenset or not self.executor_uids or any(type(uid) is not int or uid < 0 for uid in self.executor_uids):
            raise ValueError("executor_uids must be a nonempty frozenset of numeric UIDs")
        if type(self.socket_path) is not str or not self.socket_path.startswith("/") or "\x00" in self.socket_path:
            raise ValueError("socket_path must be absolute deployment metadata")


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    uid: int
    gid: int
    pid: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in (self.uid, self.gid, self.pid)):
            raise ValueError("peer credentials are malformed")


def _receipt_payload(identity: AuthorityIdentity, consumption: ExternalNonceConsumptionRequest) -> bytes:
    """The signed exact request binding; never serialized into a receipt."""
    return b"kis-nonce-authority-ed25519-receipt-v1\0" + _canonical_json({
        "authority_id": identity.service_id,
        "authority_version": identity.service_version,
        "consumption": consumption.canonical_bytes().decode("ascii"),
    })


def _receipt_values(identity: AuthorityIdentity, consumption: ExternalNonceConsumptionRequest, signer: Ed25519PrivateKey) -> tuple[str, str, str, str]:
    payload = _receipt_payload(identity, consumption)
    return (
        hashlib.sha256(consumption.canonical_bytes()).hexdigest(),
        hashlib.sha256(consumption.invocation_challenge).hexdigest(),
        hashlib.sha256(b"kis-nonce-authority-receipt-digest-v2\0" + payload).hexdigest(),
        base64.urlsafe_b64encode(signer.sign(payload)).rstrip(b"=").decode("ascii"),
    )


def _validate_consumption(consumption: object, clock: Callable[[], datetime]) -> ExternalNonceConsumptionRequest:
    if type(consumption) is not ExternalNonceConsumptionRequest:
        raise ValueError("consumption must be an exact ExternalNonceConsumptionRequest")
    consumption.__post_init__()
    now = _utc(clock(), "clock result")
    if consumption.expires_at <= now:
        raise ValueError("consumption is expired")
    return consumption


class DurableNonceAuthorityCore:
    """SQLite-backed core. It is not a daemon and has no implicit installation path."""
    __slots__ = ("authority_id", "authority_version", "_identity", "_database_path", "_signer", "_authority_uid", "_clock")

    def __init__(self, *, identity: AuthorityIdentity, database_path: Path, authority_private_key: bytes, authority_uid: int, clock: Callable[[], datetime]) -> None:
        if type(identity) is not AuthorityIdentity or not isinstance(database_path, Path) or type(authority_private_key) is not bytes or len(authority_private_key) != 32 or type(authority_uid) is not int or authority_uid < 0 or not callable(clock):
            raise ValueError("authority configuration is malformed")
        if authority_uid in identity.executor_uids:
            raise ValueError("authority UID must differ from every executor UID")
        self.authority_id, self.authority_version = identity.service_id, identity.service_version
        try:
            signer = Ed25519PrivateKey.from_private_bytes(authority_private_key)
        except ValueError as exc:
            raise ValueError("authority configuration is malformed") from exc
        self._identity, self._database_path, self._signer = identity, database_path, signer
        self._authority_uid, self._clock = authority_uid, clock
        self._admit_database_path()
        self._initialize_database()

    def __repr__(self) -> str:
        return "DurableNonceAuthorityCore(<redacted>)"

    def _admit_database_path(self) -> None:
        parent = self._database_path.parent
        try:
            parent_status = parent.lstat()
        except OSError as exc:
            raise ValueError("unsafe database parent") from exc
        if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode) or parent_status.st_uid != os.getuid() or stat.S_IMODE(parent_status.st_mode) != 0o700:
            raise ValueError("unsafe database parent")
        try:
            status = self._database_path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValueError("unsafe database path") from exc
        if stat.S_ISLNK(status.st_mode):
            raise ValueError("database path is a symlink")
        if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) != 0o600:
            raise ValueError("unsafe database path")

    def _initialize_database(self) -> None:
        try:
            fd = os.open(self._database_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            self._admit_database_path()
        except OSError as exc:
            raise ValueError("unable to create secure database") from exc
        else:
            os.close(fd)
            os.chmod(self._database_path, 0o600)
        connection = self._connection()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS consumed_requests (request_fingerprint TEXT PRIMARY KEY, nonce_fingerprint TEXT NOT NULL UNIQUE, invocation_challenge_digest TEXT NOT NULL, receipt_digest TEXT NOT NULL)")
        finally:
            connection.close()
        for suffix in ("-wal", "-shm"):
            candidate = Path(f"{self._database_path}{suffix}")
            if candidate.exists() and not candidate.is_symlink():
                os.chmod(candidate, 0o600)

    def _connection(self) -> sqlite3.Connection:
        self._admit_database_path()
        return sqlite3.connect(f"file:{self._database_path}?mode=rw", uri=True, isolation_level=None, timeout=10.0)

    def consume_once(self, consumption: ExternalNonceConsumptionRequest) -> ExternalNonceConsumptionReceipt:
        item = _validate_consumption(consumption, self._clock)
        canonical = item.canonical_bytes()
        request_fingerprint = hashlib.sha256(canonical).hexdigest()
        nonce_fingerprint = hashlib.sha256(item.nonce).hexdigest()
        request_fingerprint, invocation_challenge_digest, receipt_digest, signature_b64url = _receipt_values(self._identity, item, self._signer)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("INSERT INTO consumed_requests(request_fingerprint, nonce_fingerprint, invocation_challenge_digest, receipt_digest) VALUES (?, ?, ?, ?)", (request_fingerprint, nonce_fingerprint, invocation_challenge_digest, receipt_digest))
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise ValueError("nonce consumption already consumed") from exc
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ValueError("durable nonce authority persistence failed") from exc
        finally:
            connection.close()
        return ExternalNonceConsumptionReceipt(self.authority_id, self.authority_version, request_fingerprint, invocation_challenge_digest, receipt_digest, signature_b64url)


class UnixAuthorityWireHandler:
    """AF_UNIX request handler only; deployment owns binding/listening and lifecycle."""
    __slots__ = ("_core", "_authority_uid", "_max_request_bytes")

    def __init__(self, core: DurableNonceAuthorityCore, *, authority_uid: int, max_request_bytes: int = _MAX_REQUEST_BYTES) -> None:
        if type(core) is not DurableNonceAuthorityCore or type(authority_uid) is not int or authority_uid < 0 or authority_uid in core._identity.executor_uids or type(max_request_bytes) is not int or not 1 <= max_request_bytes <= _MAX_REQUEST_BYTES:
            raise ValueError("wire handler configuration is malformed")
        self._core, self._authority_uid, self._max_request_bytes = core, authority_uid, max_request_bytes

    def _allow_peer(self, peer: object) -> None:
        if type(peer) is not PeerCredentials:
            raise ValueError("peer credentials are required")
        peer.__post_init__()
        if peer.uid == self._authority_uid or peer.uid not in self._core._identity.executor_uids:
            raise ValueError("peer UID is not an allowed distinct executor")

    def handle_payload(self, payload: bytes, peer: PeerCredentials) -> bytes:
        self._allow_peer(peer)
        if type(payload) is not bytes or len(payload) > self._max_request_bytes:
            raise ValueError("wire payload is too large or malformed")
        consumption = _decode_request(payload)
        receipt = self._core.consume_once(consumption)
        return _canonical_json({"authority_id": receipt.authority_id, "authority_version": receipt.authority_version, "request_fingerprint": receipt.request_fingerprint, "invocation_challenge_digest": receipt.invocation_challenge_digest, "receipt_digest": receipt.receipt_digest, "signature_b64url": receipt.signature_b64url})

    def handle_socket(self, connection: socket.socket) -> None:
        if type(connection) is not socket.socket or connection.family != socket.AF_UNIX or not hasattr(socket, "SO_PEERCRED"):
            raise ValueError("AF_UNIX SO_PEERCRED connection is required")
        raw_credential = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw_credential)
        size = struct.unpack("!I", _receive_exact(connection, 4))[0]
        if size > self._max_request_bytes:
            raise ValueError("wire payload is too large")
        response = self.handle_payload(_receive_exact(connection, size), PeerCredentials(uid, gid, pid))
        connection.sendall(struct.pack("!I", len(response)) + response)


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        part = connection.recv(remaining)
        if not part:
            raise ValueError("truncated wire payload")
        chunks.append(part)
        remaining -= len(part)
    return b"".join(chunks)


def _reject_duplicate_keys(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate JSON key or malformed schema")
        result[key] = value
    return result


def _decode_request(payload: bytes) -> ExternalNonceConsumptionRequest:
    try:
        decoded = payload.decode("ascii")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON constant")))
    except ValueError as exc:
        if "duplicate JSON" in str(exc):
            raise
        raise ValueError("malformed canonical JSON") from exc
    if type(value) is not dict or set(value) != {"nonce_b64url", "action_fingerprint", "account_binding_digest", "expires_at", "invocation_challenge_b64url"}:
        raise ValueError("malformed exact wire schema")
    if payload != _canonical_json(value):
        raise ValueError("wire JSON is not canonical")
    try:
        nonce_text = _plain_string(value["nonce_b64url"], "nonce_b64url")
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=" for character in nonce_text):
            raise ValueError("bad nonce encoding")
        nonce = base64.urlsafe_b64decode(nonce_text.encode("ascii"))
        if base64.urlsafe_b64encode(nonce).decode("ascii") != nonce_text:
            raise ValueError("nonce encoding is not canonical")
        challenge_text = _plain_string(value["invocation_challenge_b64url"], "invocation_challenge_b64url")
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=" for character in challenge_text):
            raise ValueError("bad invocation challenge encoding")
        invocation_challenge = base64.urlsafe_b64decode(challenge_text.encode("ascii"))
        if base64.urlsafe_b64encode(invocation_challenge).decode("ascii") != challenge_text:
            raise ValueError("invocation challenge encoding is not canonical")
        expires_at = datetime.fromisoformat(_plain_string(value["expires_at"], "expires_at"))
        if expires_at.tzinfo is not timezone.utc or expires_at.isoformat() != value["expires_at"]:
            raise ValueError("timestamp is not canonical UTC")
        return ExternalNonceConsumptionRequest(nonce, _digest(value["action_fingerprint"], "action_fingerprint"), _digest(value["account_binding_digest"], "account_binding_digest"), expires_at, invocation_challenge)
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed exact wire request") from exc


class WireAuthorityClient:
    """Injected wire exchange for tests/deployment adapters; it opens no transport itself."""
    __slots__ = ("authority_id", "authority_version", "_identity", "_exchange")

    def __init__(self, *, identity: AuthorityIdentity, exchange: Callable[[bytes], bytes]) -> None:
        if type(identity) is not AuthorityIdentity or not callable(exchange):
            raise ValueError("wire client configuration is malformed")
        self.authority_id, self.authority_version, self._identity, self._exchange = identity.service_id, identity.service_version, identity, exchange

    def consume(self, consumption: ExternalNonceConsumptionRequest) -> ExternalNonceConsumptionReceipt:
        if type(consumption) is not ExternalNonceConsumptionRequest:
            raise ValueError("consumption must be an exact ExternalNonceConsumptionRequest")
        consumption.__post_init__()
        item = consumption
        payload = _canonical_json({"nonce_b64url": base64.urlsafe_b64encode(item.nonce).decode("ascii"), "action_fingerprint": item.action_fingerprint, "account_binding_digest": item.account_binding_digest, "expires_at": item.expires_at.isoformat(), "invocation_challenge_b64url": base64.urlsafe_b64encode(item.invocation_challenge).decode("ascii")})
        try:
            response = self._exchange(payload)
            value = json.loads(response.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
            if type(value) is not dict or set(value) != {"authority_id", "authority_version", "request_fingerprint", "invocation_challenge_digest", "receipt_digest", "signature_b64url"}:
                raise ValueError("bad receipt schema")
            if response != _canonical_json(value):
                raise ValueError("receipt JSON is not canonical")
            return ExternalNonceConsumptionReceipt(value["authority_id"], value["authority_version"], value["request_fingerprint"], value["invocation_challenge_digest"], value["receipt_digest"], value["signature_b64url"])
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("authority wire receipt is malformed") from exc


class ExternalNonceAuthorityAdapter:
    """Protocol-compatible adapter that verifies an authority's public-key receipt fail closed."""
    __slots__ = ("authority_id", "authority_version", "_client", "_public_verifier")

    def __init__(self, *, client: WireAuthorityClient, authority_public_key: bytes) -> None:
        if type(client) is not WireAuthorityClient or type(authority_public_key) is not bytes or len(authority_public_key) != 32:
            raise ValueError("authority adapter configuration is malformed")
        try:
            public_verifier = Ed25519PublicKey.from_public_bytes(authority_public_key)
        except ValueError as exc:
            raise ValueError("authority adapter configuration is malformed") from exc
        self.authority_id, self.authority_version, self._client, self._public_verifier = client.authority_id, client.authority_version, client, public_verifier

    def consume_once(self, consumption: ExternalNonceConsumptionRequest) -> ExternalNonceConsumptionReceipt:
        if type(consumption) is not ExternalNonceConsumptionRequest:
            raise ValueError("consumption must be an exact ExternalNonceConsumptionRequest")
        receipt = self._client.consume(consumption)
        if type(receipt) is not ExternalNonceConsumptionReceipt or receipt.authority_id != self.authority_id or receipt.authority_version != self.authority_version:
            raise ValueError("authority receipt identity mismatch")
        request_fingerprint = hashlib.sha256(consumption.canonical_bytes()).hexdigest()
        invocation_challenge_digest = hashlib.sha256(consumption.invocation_challenge).hexdigest()
        expected_digest = hashlib.sha256(b"kis-nonce-authority-receipt-digest-v2\0" + _receipt_payload(self._client._identity, consumption)).hexdigest()
        if receipt.request_fingerprint != request_fingerprint or receipt.invocation_challenge_digest != invocation_challenge_digest or receipt.receipt_digest != expected_digest:
            raise ValueError("authority receipt digest mismatch")
        try:
            signature = base64.b64decode(receipt.signature_b64url + "==", altchars=b"-_", validate=True)
            if len(signature) != 64 or base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii") != receipt.signature_b64url:
                raise ValueError("authority receipt signature encoding mismatch")
            self._public_verifier.verify(signature, _receipt_payload(self._client._identity, consumption))
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("authority receipt signature mismatch") from exc
        return receipt
