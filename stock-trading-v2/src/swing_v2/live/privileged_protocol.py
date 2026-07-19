"""Non-submitting, one-shot privileged-dispatch authorization contract.

This module has no broker client, transport, credential, dotenv, socket, or halt-control
API. It validates a short-lived signed envelope and requests one-shot nonce consumption
from an injected *external* authority. It does not itself provide durable replay defense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import base64
import hashlib
import hmac
import json
import secrets
from typing import Protocol, runtime_checkable

PROTOCOL_VERSION = "privileged-dispatch-v1"
OPERATOR_CONFIRMATION_PHRASE = "I APPROVE THIS EXACT PRODUCTION KRX LIMIT ORDER"
_MAX_TTL_SECONDS = 60
_HEX64 = frozenset("0123456789abcdef")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _require_string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a nonempty plain str")
    return value


def _require_digest(value: object, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in _HEX64 for character in value):
        raise ValueError(f"{name} must be a lowercase sha256 digest")
    return value


def _decimal_text(value: object) -> str:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError("limit_price must be a positive finite plain Decimal")
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _require_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise ValueError(f"{name} must be an exact UTC datetime")
    return value


def compute_account_binding_digest(account_reference: object) -> str:
    """Derive a non-raw account binding; callers must not serialize the reference."""
    _require_string(account_reference, "account_reference")
    return hashlib.sha256(_canonical_json({"account_reference": account_reference})).hexdigest()


@dataclass(frozen=True, slots=True)
class AllowedBrokerAction:
    broker: str
    symbol: str
    classification: str
    side: str
    quantity: int
    limit_price: Decimal
    order_mode: str

    def __post_init__(self) -> None:
        _validate_action(self.broker, self.symbol, self.classification, self.side, self.quantity, self.limit_price, self.order_mode)


def _validate_action(broker: object, symbol: object, classification: object, side: object, quantity: object, limit_price: object, order_mode: object) -> None:
    if type(broker) is not str or broker != "KIS":
        raise ValueError("only the explicit KIS broker contract is supported")
    if type(symbol) is not str or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("symbol must be one exact six-digit KRX symbol")
    if type(classification) is not str or classification not in {"STOCK", "DOMESTIC_INDEX_OR_SECTOR"}:
        raise ValueError("classification is not explicitly allowed")
    if type(side) is not str or side not in {"BUY", "SELL"}:
        raise ValueError("side is not explicitly allowed")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive plain int")
    _decimal_text(limit_price)
    if type(order_mode) is not str or order_mode != "LIMIT":
        raise ValueError("conditional or non-limit order modes are denied")


def compute_action_fingerprint(action: object) -> str:
    if type(action) is not AllowedBrokerAction:
        raise ValueError("action must be an exact AllowedBrokerAction")
    _validate_action(action.broker, action.symbol, action.classification, action.side, action.quantity, action.limit_price, action.order_mode)
    return hashlib.sha256(_canonical_json({
        "broker": action.broker, "symbol": action.symbol, "classification": action.classification,
        "side": action.side, "quantity": action.quantity, "limit_price": _decimal_text(action.limit_price),
        "order_mode": action.order_mode,
    })).hexdigest()


@dataclass(frozen=True, slots=True)
class OperatorAuthorizationRequest:
    protocol_version: str
    environment: str
    account_binding_digest: str
    live_intent_fingerprint: str
    action_fingerprint: str
    broker: str
    symbol: str
    classification: str
    side: str
    quantity: int
    limit_price: Decimal
    order_mode: str
    issued_at: datetime
    expires_at: datetime
    nonce: bytes = field(repr=False)
    operator_confirmation: str

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION or type(self.protocol_version) is not str:
            raise ValueError("unsupported protocol version")
        if type(self.environment) is not str or self.environment != "PRODUCTION":
            raise ValueError("only PRODUCTION authorization envelopes are supported")
        _require_digest(self.account_binding_digest, "account_binding_digest")
        _require_digest(self.live_intent_fingerprint, "live_intent_fingerprint")
        _require_digest(self.action_fingerprint, "action_fingerprint")
        _validate_action(self.broker, self.symbol, self.classification, self.side, self.quantity, self.limit_price, self.order_mode)
        issued_at = _require_utc(self.issued_at, "issued_at")
        expires_at = _require_utc(self.expires_at, "expires_at")
        if expires_at <= issued_at or (expires_at - issued_at).total_seconds() > _MAX_TTL_SECONDS:
            raise ValueError("authorization timing exceeds the narrow maximum TTL")
        if type(self.nonce) is not bytes or len(self.nonce) != 32:
            raise ValueError("nonce must be an exact opaque 32-byte value")
        if type(self.operator_confirmation) is not str or self.operator_confirmation != OPERATOR_CONFIRMATION_PHRASE:
            raise ValueError("explicit operator confirmation phrase is required")


def _request_payload(request: OperatorAuthorizationRequest) -> dict[str, object]:
    return {
        "protocol_version": request.protocol_version, "environment": request.environment,
        "account_binding_digest": request.account_binding_digest, "live_intent_fingerprint": request.live_intent_fingerprint,
        "action_fingerprint": request.action_fingerprint, "broker": request.broker, "symbol": request.symbol,
        "classification": request.classification, "side": request.side, "quantity": request.quantity,
        "limit_price": _decimal_text(request.limit_price), "order_mode": request.order_mode,
        "issued_at": request.issued_at.isoformat(), "expires_at": request.expires_at.isoformat(),
        "nonce_b64url": base64.urlsafe_b64encode(request.nonce).decode("ascii"),
        "operator_confirmation": request.operator_confirmation,
    }


@dataclass(frozen=True, slots=True)
class SignedAuthorizationEnvelope:
    request: OperatorAuthorizationRequest
    signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.request) is not OperatorAuthorizationRequest or type(self.signature) is not bytes or len(self.signature) != 32:
            raise ValueError("envelope fields are malformed")


@dataclass(frozen=True, slots=True)
class ExternalNonceConsumptionRequest:
    """Canonical binding passed to the separately privileged nonce authority."""

    nonce: bytes = field(repr=False)
    action_fingerprint: str
    account_binding_digest: str
    expires_at: datetime
    invocation_challenge: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.nonce) is not bytes or len(self.nonce) != 32:
            raise ValueError("external nonce consumption request is malformed")
        _require_digest(self.action_fingerprint, "external action_fingerprint")
        _require_digest(self.account_binding_digest, "external account_binding_digest")
        _require_utc(self.expires_at, "external expires_at")
        if type(self.invocation_challenge) is not bytes or len(self.invocation_challenge) != 32:
            raise ValueError("external invocation_challenge must be an exact opaque 32-byte value")

    def canonical_bytes(self) -> bytes:
        """Stable authority input; an authority must atomically persist this consumption."""
        return _canonical_json({
            "nonce_b64url": base64.urlsafe_b64encode(self.nonce).decode("ascii"),
            "action_fingerprint": self.action_fingerprint,
            "account_binding_digest": self.account_binding_digest,
            "expires_at": self.expires_at.isoformat(),
            "invocation_challenge_b64url": base64.urlsafe_b64encode(self.invocation_challenge).decode("ascii"),
        })


@dataclass(frozen=True, slots=True)
class ExternalNonceConsumptionReceipt:
    """Opaque evidence returned by the external authority; not broker authorization."""

    authority_id: str
    authority_version: int
    request_fingerprint: str
    invocation_challenge_digest: str
    receipt_digest: str
    signature_b64url: str

    def __post_init__(self) -> None:
        _require_string(self.authority_id, "external receipt authority_id")
        if type(self.authority_version) is not int or self.authority_version <= 0:
            raise ValueError("external receipt authority_version is malformed")
        _require_digest(self.request_fingerprint, "external receipt request fingerprint")
        _require_digest(self.invocation_challenge_digest, "external receipt invocation challenge digest")
        _require_digest(self.receipt_digest, "external receipt digest")
        if type(self.signature_b64url) is not str or len(self.signature_b64url) != 86 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in self.signature_b64url):
            raise ValueError("external receipt Ed25519 signature is malformed")


@runtime_checkable
class ExternalNonceConsumptionAuthority(Protocol):
    """External privileged dependency; this module intentionally has no implementation."""

    authority_id: str
    authority_version: int

    def consume_once(self, consumption: ExternalNonceConsumptionRequest) -> ExternalNonceConsumptionReceipt:
        """Atomically consume the canonical binding or raise without a receipt."""
        ...


@dataclass(frozen=True, slots=True)
class ApprovedDispatch:
    """Immutable non-executable action snapshot, not broker authorization or execution."""

    account_binding_digest: str
    live_intent_fingerprint: str
    action_fingerprint: str
    broker: str
    symbol: str
    classification: str
    side: str
    quantity: int
    limit_price: Decimal
    order_mode: str
    nonce_consumption_receipt: ExternalNonceConsumptionReceipt


class PrivilegedAuthorizationIssuer:
    """Test-only HMAC issuer; production issuance belongs outside the strategy process."""
    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValueError("signing key must be an exact 32-byte value")
        self._key = key

    def __repr__(self) -> str:
        return "PrivilegedAuthorizationIssuer(<redacted>)"

    def issue(self, request: OperatorAuthorizationRequest) -> SignedAuthorizationEnvelope:
        if type(request) is not OperatorAuthorizationRequest:
            raise ValueError("request must be an exact OperatorAuthorizationRequest")
        return SignedAuthorizationEnvelope(request, hmac.digest(self._key, _canonical_json(_request_payload(request)), "sha256"))


def _validate_external_authority(authority: object) -> tuple[ExternalNonceConsumptionAuthority, str, int]:
    if not isinstance(authority, ExternalNonceConsumptionAuthority) or not callable(getattr(authority, "consume_once", None)):
        raise ValueError("external nonce authority dependency is malformed")
    authority_id = _require_string(authority.authority_id, "external nonce authority_id")
    if type(authority.authority_version) is not int or authority.authority_version <= 0:
        raise ValueError("external nonce authority_version is malformed")
    return authority, authority_id, authority.authority_version


class PrivilegedDispatchVerifier:
    """Verify an envelope then delegate atomic consumption to an external authority."""
    __slots__ = ("_key", "_expected_account_binding", "_allowed_actions", "_external_nonce_authority", "_authority_id", "_authority_version", "_clock", "_random_bytes")

    def __init__(self, *, key: bytes, expected_account_binding: str, allowed_actions: frozenset[AllowedBrokerAction], external_nonce_authority: ExternalNonceConsumptionAuthority, clock, random_bytes=secrets.token_bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValueError("verification key must be an exact 32-byte value")
        _require_digest(expected_account_binding, "expected_account_binding")
        if type(allowed_actions) is not frozenset or not allowed_actions or any(type(action) is not AllowedBrokerAction for action in allowed_actions):
            raise ValueError("allowed_actions must be a nonempty frozenset of exact actions")
        authority, authority_id, authority_version = _validate_external_authority(external_nonce_authority)
        if not callable(clock) or not callable(random_bytes):
            raise ValueError("verifier dependencies are malformed")
        self._key, self._expected_account_binding = key, expected_account_binding
        self._allowed_actions = frozenset(AllowedBrokerAction(action.broker, action.symbol, action.classification, action.side, action.quantity, action.limit_price, action.order_mode) for action in allowed_actions)
        self._external_nonce_authority, self._authority_id, self._authority_version, self._clock, self._random_bytes = authority, authority_id, authority_version, clock, random_bytes

    def __repr__(self) -> str:
        return "PrivilegedDispatchVerifier(<redacted>)"

    def verify_and_consume(self, envelope: SignedAuthorizationEnvelope) -> ApprovedDispatch:
        if type(envelope) is not SignedAuthorizationEnvelope or type(envelope.request) is not OperatorAuthorizationRequest or type(envelope.signature) is not bytes:
            raise ValueError("envelope is malformed")
        request = envelope.request
        request.__post_init__()
        snapshot_payload = _request_payload(request)
        expected_signature = hmac.digest(self._key, _canonical_json(snapshot_payload), "sha256")
        if not hmac.compare_digest(envelope.signature, expected_signature):
            raise ValueError("authorization signature mismatch")
        now = _require_utc(self._clock(), "clock result")
        if request.issued_at > now or request.expires_at <= now or request.expires_at <= request.issued_at or (request.expires_at - request.issued_at).total_seconds() > _MAX_TTL_SECONDS:
            raise ValueError("authorization timing is invalid")
        if request.account_binding_digest != self._expected_account_binding:
            raise ValueError("authorization account binding mismatch")
        action = AllowedBrokerAction(request.broker, request.symbol, request.classification, request.side, request.quantity, request.limit_price, request.order_mode)
        if action not in self._allowed_actions or compute_action_fingerprint(action) != request.action_fingerprint:
            raise ValueError("authorization action is not explicitly allowed")
        if _request_payload(request) != snapshot_payload:
            raise ValueError("authorization mutated during verification")
        try:
            invocation_challenge = self._random_bytes(32)
        except BaseException as exc:
            raise ValueError("invocation challenge generation failed closed") from exc
        if type(invocation_challenge) is not bytes or len(invocation_challenge) != 32:
            raise ValueError("invocation challenge generator returned malformed value")
        if _request_payload(request) != snapshot_payload:
            raise ValueError("authorization mutated during challenge generation")
        consumption = ExternalNonceConsumptionRequest(request.nonce, request.action_fingerprint, request.account_binding_digest, request.expires_at, invocation_challenge)
        try:
            receipt = self._external_nonce_authority.consume_once(consumption)
        except ValueError:
            raise
        except BaseException as exc:
            raise ValueError("external nonce authority failed closed") from exc
        if type(receipt) is not ExternalNonceConsumptionReceipt:
            raise ValueError("external nonce receipt is malformed")
        try:
            receipt.__post_init__()
        except (TypeError, ValueError) as exc:
            raise ValueError("external nonce receipt is malformed") from exc
        if receipt.authority_id != self._authority_id or receipt.authority_version != self._authority_version:
            raise ValueError("external nonce receipt authority mismatch")
        if not hmac.compare_digest(receipt.invocation_challenge_digest, hashlib.sha256(invocation_challenge).hexdigest()):
            raise ValueError("external nonce receipt invocation challenge mismatch")
        if _request_payload(request) != snapshot_payload:
            raise ValueError("authorization mutated during consume")
        return ApprovedDispatch(request.account_binding_digest, request.live_intent_fingerprint, request.action_fingerprint, action.broker, action.symbol, action.classification, action.side, action.quantity, action.limit_price, action.order_mode, receipt)
