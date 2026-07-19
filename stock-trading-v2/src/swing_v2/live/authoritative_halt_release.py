"""Non-executing authoritative halt-release evidence/attestation contract.

Phase 1 only evaluates a separately privileged authority's signed, atomic evidence
claim.  It performs no broker read, order action, token handling, network access,
filesystem access, or halt-marker mutation.  An eligible result is *only* a cue for
an independent operator review; it is never a halt clear.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
import secrets
from typing import Callable, Protocol, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

AUTHORITATIVE_HALT_RELEASE_PROTOCOL_VERSION = "authoritative-halt-release-v1"
EXTERNAL_REVIEW_CHALLENGE_PROTOCOL_ID = "external-review-challenge-consumption-v1"
EXTERNAL_REVIEW_CHALLENGE_PROTOCOL_VERSION = 1
OPERATOR_REVIEW_PHRASE = "I REQUEST SEPARATE OPERATOR REVIEW OF THIS EXACT HALT RELEASE EVIDENCE"
_MAX_FRESHNESS = timedelta(seconds=120)
_MAX_FILL_QUERY_RANGE = timedelta(days=31)
_REVIEW_CHALLENGE_TTL = timedelta(seconds=60)
_HEX64 = frozenset("0123456789abcdef")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _plain(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty exact plain str")
    return value


def _digest(value: object, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in _HEX64 for character in value):
        raise ValueError(f"{name} must be a lowercase sha256 digest")
    return value


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise ValueError(f"{name} must be an exact UTC datetime")
    return value


def _signature_text(value: object) -> str:
    if type(value) is not str or len(value) != 86 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("Ed25519 signature is malformed")
    return value


def _canonical_ed25519_signature(value: object) -> bytes:
    """Decode only the one unpadded Base64URL spelling of an Ed25519 signature."""
    encoded = _signature_text(value).encode("ascii")
    signature = base64.b64decode(encoded + b"==", altchars=b"-_", validate=True)
    if len(signature) != 64 or base64.urlsafe_b64encode(signature).rstrip(b"=") != encoded:
        raise ValueError("Ed25519 signature is noncanonical")
    return signature


@dataclass(frozen=True, slots=True)
class BrokerAcknowledgementReference:
    """Opaque KIS branch/order acknowledgement identity; it says nothing about fills."""
    branch_id: str
    order_id: str

    def __post_init__(self) -> None:
        _plain(self.branch_id, "branch_id"); _plain(self.order_id, "order_id")
        if self.branch_id == self.order_id:
            raise ValueError("broker acknowledgement branch/order collision is rejected")


@dataclass(frozen=True, slots=True)
class HaltReleaseRequest:
    """Immutable, challenge-bound request without raw account or marker path."""
    protocol_version: str
    expected_account_binding_hash: str
    original_audited_action_fingerprint: str
    broker_acknowledgement: BrokerAcknowledgementReference
    ambiguity_marker_identity: str
    ambiguity_marker_digest: str
    submitted_at: datetime
    requested_at: datetime
    operator_review_phrase: str
    operator_review_challenge: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not str or self.protocol_version != AUTHORITATIVE_HALT_RELEASE_PROTOCOL_VERSION:
            raise ValueError("unsupported halt-release protocol version")
        _digest(self.expected_account_binding_hash, "expected_account_binding_hash")
        _digest(self.original_audited_action_fingerprint, "original_audited_action_fingerprint")
        if type(self.broker_acknowledgement) is not BrokerAcknowledgementReference:
            raise ValueError("broker acknowledgement must be exact")
        self.broker_acknowledgement.__post_init__()
        _plain(self.ambiguity_marker_identity, "ambiguity_marker_identity")
        _digest(self.ambiguity_marker_digest, "ambiguity_marker_digest")
        submitted, requested = _utc(self.submitted_at, "submitted_at"), _utc(self.requested_at, "requested_at")
        if requested < submitted:
            raise ValueError("halt-release request time ordering is invalid")
        if type(self.operator_review_phrase) is not str or self.operator_review_phrase != OPERATOR_REVIEW_PHRASE:
            raise ValueError("explicit operator review phrase is required")
        if type(self.operator_review_challenge) is not bytes or len(self.operator_review_challenge) != 32:
            raise ValueError("operator review challenge must be an exact opaque 32-byte value")

    def fingerprint(self) -> str:
        self.__post_init__()
        return hashlib.sha256(_canonical_json(_request_payload(self))).hexdigest()


def _request_payload(item: HaltReleaseRequest) -> dict[str, object]:
    return {"protocol_version": item.protocol_version, "expected_account_binding_hash": item.expected_account_binding_hash,
        "original_audited_action_fingerprint": item.original_audited_action_fingerprint,
        "broker_branch_id": item.broker_acknowledgement.branch_id, "broker_order_id": item.broker_acknowledgement.order_id,
        "ambiguity_marker_identity": item.ambiguity_marker_identity, "ambiguity_marker_digest": item.ambiguity_marker_digest,
        "submitted_at": item.submitted_at.isoformat(), "requested_at": item.requested_at.isoformat(),
        "operator_review_phrase": item.operator_review_phrase,
        "operator_review_challenge_digest": hashlib.sha256(item.operator_review_challenge).hexdigest()}


@dataclass(frozen=True, slots=True)
class HaltReleaseChallengeConsumptionRequest:
    """Exact one-shot request for a separately privileged challenge authority."""
    protocol_id: str
    protocol_version: int
    release_request_fingerprint: str
    expected_account_binding_hash: str
    operator_review_challenge: bytes = field(repr=False)
    expires_at: datetime
    authority_invocation_challenge: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.protocol_id) is not str or self.protocol_id != EXTERNAL_REVIEW_CHALLENGE_PROTOCOL_ID:
            raise ValueError("unsupported review challenge protocol")
        if type(self.protocol_version) is not int or self.protocol_version != EXTERNAL_REVIEW_CHALLENGE_PROTOCOL_VERSION:
            raise ValueError("unsupported review challenge protocol version")
        _digest(self.release_request_fingerprint, "release_request_fingerprint")
        _digest(self.expected_account_binding_hash, "expected_account_binding_hash")
        if type(self.operator_review_challenge) is not bytes or len(self.operator_review_challenge) != 32:
            raise ValueError("operator_review_challenge must be an exact opaque 32-byte value")
        _utc(self.expires_at, "expires_at")
        if type(self.authority_invocation_challenge) is not bytes or len(self.authority_invocation_challenge) != 32:
            raise ValueError("authority_invocation_challenge must be an exact opaque 32-byte value")

    def canonical_bytes(self) -> bytes:
        self.__post_init__()
        return _canonical_json({"protocol_id": self.protocol_id, "protocol_version": self.protocol_version,
            "release_request_fingerprint": self.release_request_fingerprint,
            "expected_account_binding_hash": self.expected_account_binding_hash,
            "operator_review_challenge_b64url": base64.urlsafe_b64encode(self.operator_review_challenge).decode("ascii"),
            "expires_at": self.expires_at.isoformat(),
            "authority_invocation_challenge_b64url": base64.urlsafe_b64encode(self.authority_invocation_challenge).decode("ascii")})


@dataclass(frozen=True, slots=True)
class HaltReleaseChallengeConsumptionReceipt:
    """Signed authority receipt; raw request challenges never appear in it."""
    authority_id: str
    authority_version: int
    request_fingerprint: str
    invocation_challenge_digest: str
    receipt_digest: str
    signature_b64url: str = field(repr=False)

    def __post_init__(self) -> None:
        _plain(self.authority_id, "receipt authority_id")
        if type(self.authority_version) is not int or self.authority_version <= 0:
            raise ValueError("receipt authority_version is malformed")
        _digest(self.request_fingerprint, "receipt request_fingerprint")
        _digest(self.invocation_challenge_digest, "receipt invocation_challenge_digest")
        _digest(self.receipt_digest, "receipt_digest")
        _signature_text(self.signature_b64url)


def _challenge_receipt_digest(authority_id: str, authority_version: int, consumption: HaltReleaseChallengeConsumptionRequest) -> str:
    return hashlib.sha256(b"authoritative-halt-release-review-receipt-digest-v1\0" + _canonical_json({
        "authority_id": authority_id, "authority_version": authority_version,
        "consumption_request_fingerprint": hashlib.sha256(consumption.canonical_bytes()).hexdigest(),
        "authority_invocation_challenge_digest": hashlib.sha256(consumption.authority_invocation_challenge).hexdigest(),
    })).hexdigest()


def _challenge_receipt_payload(authority_id: str, authority_version: int, consumption: HaltReleaseChallengeConsumptionRequest, receipt_digest: str) -> bytes:
    """Signed binding for an externally durable consumption event, not a local fallback."""
    return b"authoritative-halt-release-review-receipt-v1\0" + _canonical_json({
        "authority_id": authority_id, "authority_version": authority_version,
        "consumption_request_fingerprint": hashlib.sha256(consumption.canonical_bytes()).hexdigest(),
        "expected_account_binding_hash": consumption.expected_account_binding_hash,
        "operator_review_challenge_digest": hashlib.sha256(consumption.operator_review_challenge).hexdigest(),
        "expires_at": consumption.expires_at.isoformat(),
        "authority_invocation_challenge_digest": hashlib.sha256(consumption.authority_invocation_challenge).hexdigest(),
        "receipt_digest": receipt_digest,
    })


def sign_challenge_consumption_receipt_for_test(*, authority_id: str, authority_version: int, request: HaltReleaseChallengeConsumptionRequest, private_seed: bytes) -> HaltReleaseChallengeConsumptionReceipt:
    """Fixture-only signer; deployment requires a separately privileged durable authority service."""
    if type(authority_id) is not str or type(authority_version) is not int or type(request) is not HaltReleaseChallengeConsumptionRequest or type(private_seed) is not bytes or len(private_seed) != 32:
        raise ValueError("test review receipt signer inputs are malformed")
    request.__post_init__()
    try:
        signer = Ed25519PrivateKey.from_private_bytes(private_seed)
    except ValueError as exc:
        raise ValueError("test review receipt signer inputs are malformed") from exc
    receipt_digest = _challenge_receipt_digest(authority_id, authority_version, request)
    payload = _challenge_receipt_payload(authority_id, authority_version, request, receipt_digest)
    return HaltReleaseChallengeConsumptionReceipt(
        authority_id, authority_version, hashlib.sha256(request.canonical_bytes()).hexdigest(),
        hashlib.sha256(request.authority_invocation_challenge).hexdigest(), receipt_digest,
        base64.urlsafe_b64encode(signer.sign(payload)).rstrip(b"=").decode("ascii"),
    )


class ExternalReviewChallengeAuthority(Protocol):
    """Separately privileged, durable, atomic one-shot consumption boundary."""
    def consume(self, request: HaltReleaseChallengeConsumptionRequest) -> HaltReleaseChallengeConsumptionReceipt: ...


@dataclass(frozen=True, slots=True)
class ReconciliationEvidenceScope:
    """Completeness/freshness claims only; raw account, balances and positions stay external."""
    observed_started_at: datetime
    observed_ended_at: datetime
    open_orders_source_id: str
    open_orders_complete: bool
    fills_source_id: str
    fills_range_started_at: datetime
    fills_range_ended_at: datetime
    fills_bounded: bool
    fills_page_complete: bool
    balance_source_id: str
    balance_complete: bool
    positions_source_id: str
    positions_complete: bool
    cash_source_id: str
    cash_complete: bool
    atomic_collection_id: str
    atomic_collection_declared: bool

    def __post_init__(self) -> None:
        start, end = _utc(self.observed_started_at, "observed_started_at"), _utc(self.observed_ended_at, "observed_ended_at")
        fill_start, fill_end = _utc(self.fills_range_started_at, "fills_range_started_at"), _utc(self.fills_range_ended_at, "fills_range_ended_at")
        if end < start or fill_end < fill_start or fill_end < start:
            raise ValueError("evidence time ordering is invalid")
        for name in ("open_orders_source_id", "fills_source_id", "balance_source_id", "positions_source_id", "cash_source_id", "atomic_collection_id"):
            _plain(getattr(self, name), name)
        for name in ("open_orders_complete", "fills_bounded", "fills_page_complete", "balance_complete", "positions_complete", "cash_complete", "atomic_collection_declared"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be exact bool")

    def canonical_digest(self) -> str:
        self.__post_init__()
        return hashlib.sha256(_canonical_json(_scope_payload(self))).hexdigest()


def _scope_payload(scope: ReconciliationEvidenceScope) -> dict[str, object]:
    return {name: getattr(scope, name).isoformat() if name.endswith("_at") else getattr(scope, name) for name in (
        "observed_started_at", "observed_ended_at", "open_orders_source_id", "open_orders_complete", "fills_source_id",
        "fills_range_started_at", "fills_range_ended_at", "fills_bounded", "fills_page_complete", "balance_source_id",
        "balance_complete", "positions_source_id", "positions_complete", "cash_source_id", "cash_complete",
        "atomic_collection_id", "atomic_collection_declared")}


class AttestationDisposition(str, Enum):
    NO_CONTRADICTION = "NO_CONTRADICTION"
    CONTRADICTION = "CONTRADICTION"


class HaltReleaseDecision(str, Enum):
    ELIGIBLE_FOR_SEPARATE_OPERATOR_REVIEW = "ELIGIBLE_FOR_SEPARATE_OPERATOR_REVIEW"
    UNRESOLVED = "UNRESOLVED"
    CONTRADICTION = "CONTRADICTION"


@dataclass(frozen=True, slots=True)
class AuthoritativeReconciliationAttestation:
    """Signed opaque authority receipt. No raw challenge, account, or evidence records."""
    authority_id: str
    authority_version: int
    request_fingerprint: str
    evidence_scope_digest: str
    disposition: AttestationDisposition
    attestation_id: str
    attestation_digest: str = "0" * 64
    signature_b64url: str = field(default="A" * 86, repr=False)

    def __post_init__(self) -> None:
        _plain(self.authority_id, "authority_id")
        if type(self.authority_version) is not int or self.authority_version <= 0:
            raise ValueError("authority_version is malformed")
        _digest(self.request_fingerprint, "request_fingerprint"); _digest(self.evidence_scope_digest, "evidence_scope_digest")
        if type(self.disposition) is not AttestationDisposition:
            raise ValueError("attestation disposition is malformed")
        _plain(self.attestation_id, "attestation_id"); _digest(self.attestation_digest, "attestation_digest"); _signature_text(self.signature_b64url)


def _attestation_payload(item: AuthoritativeReconciliationAttestation, request_fingerprint: str, scope_digest: str) -> bytes:
    return b"authoritative-halt-release-attestation-v1\0" + _canonical_json({"authority_id": item.authority_id,
        "authority_version": item.authority_version, "request_fingerprint": request_fingerprint,
        "evidence_scope_digest": scope_digest, "disposition": item.disposition.value, "attestation_id": item.attestation_id})


def sign_attestation_for_test(attestation: AuthoritativeReconciliationAttestation, *, request: HaltReleaseRequest, evidence_scope: ReconciliationEvidenceScope, private_seed: bytes) -> AuthoritativeReconciliationAttestation:
    """Test fixture helper only; deployment must use a separately owned authority service."""
    if type(attestation) is not AuthoritativeReconciliationAttestation or type(request) is not HaltReleaseRequest or type(evidence_scope) is not ReconciliationEvidenceScope or type(private_seed) is not bytes or len(private_seed) != 32:
        raise ValueError("test signer inputs are malformed")
    request_fingerprint, scope_digest = request.fingerprint(), evidence_scope.canonical_digest()
    if attestation.request_fingerprint != request_fingerprint or attestation.evidence_scope_digest != scope_digest:
        raise ValueError("test attestation bindings are malformed")
    try:
        signer = Ed25519PrivateKey.from_private_bytes(private_seed)
    except ValueError as exc:
        raise ValueError("test signer inputs are malformed") from exc
    payload = _attestation_payload(attestation, request_fingerprint, scope_digest)
    digest = hashlib.sha256(b"authoritative-halt-release-attestation-digest-v1\0" + payload).hexdigest()
    signature = base64.urlsafe_b64encode(signer.sign(payload)).rstrip(b"=").decode("ascii")
    return replace(attestation, attestation_digest=digest, signature_b64url=signature)


def _valid_scope(scope: object, request: HaltReleaseRequest, now: datetime) -> bool:
    if type(scope) is not ReconciliationEvidenceScope:
        return False
    try:
        scope.__post_init__()
        return (scope.observed_started_at <= now and scope.observed_ended_at <= now and
            now - scope.observed_started_at <= _MAX_FRESHNESS and now - scope.observed_ended_at <= _MAX_FRESHNESS and
            scope.observed_ended_at - scope.observed_started_at <= _MAX_FRESHNESS and
            scope.fills_range_ended_at == scope.observed_ended_at and
            scope.fills_range_ended_at - scope.fills_range_started_at <= _MAX_FILL_QUERY_RANGE and
            scope.fills_range_started_at <= request.submitted_at <= scope.fills_range_ended_at and
            scope.open_orders_complete is True and scope.fills_bounded is True and scope.fills_page_complete is True and
            scope.balance_complete is True and scope.positions_complete is True and scope.cash_complete is True and
            scope.atomic_collection_declared is True)
    except (TypeError, ValueError):
        return False


def _expected_snapshot(*, expected_account_binding_hash: object, expected_original_audited_action_fingerprint: object, expected_broker_acknowledgement: object, expected_ambiguity_marker_identity: object, expected_ambiguity_marker_digest: object, authority_id: object, authority_version: object, authority_public_key: object, review_challenge_authority_id: object, review_challenge_authority_version: object, review_challenge_authority_public_key: object) -> tuple[str, str, str, str, str, str, str, int, bytes, str, int, bytes]:
    """Copy exact primitives before external consumption; re-run after it to close TOCTOU."""
    _digest(expected_account_binding_hash, "expected account binding")
    _digest(expected_original_audited_action_fingerprint, "expected original fingerprint")
    _plain(expected_ambiguity_marker_identity, "expected ambiguity marker identity")
    _digest(expected_ambiguity_marker_digest, "expected ambiguity marker digest")
    if type(expected_broker_acknowledgement) is not BrokerAcknowledgementReference:
        raise ValueError("expected broker acknowledgement is malformed")
    expected_broker_acknowledgement.__post_init__()
    _plain(authority_id, "attestation authority_id")
    if type(authority_version) is not int or authority_version <= 0:
        raise ValueError("attestation authority_version is malformed")
    if type(authority_public_key) is not bytes or len(authority_public_key) != 32:
        raise ValueError("attestation authority key is malformed")
    _plain(review_challenge_authority_id, "review authority_id")
    if type(review_challenge_authority_version) is not int or review_challenge_authority_version <= 0:
        raise ValueError("review authority_version is malformed")
    if type(review_challenge_authority_public_key) is not bytes or len(review_challenge_authority_public_key) != 32:
        raise ValueError("review authority key is malformed")
    return (expected_account_binding_hash, expected_original_audited_action_fingerprint,
        expected_broker_acknowledgement.branch_id, expected_broker_acknowledgement.order_id,
        expected_ambiguity_marker_identity, expected_ambiguity_marker_digest, authority_id,
        authority_version, authority_public_key, review_challenge_authority_id,
        review_challenge_authority_version, review_challenge_authority_public_key)  # type: ignore[return-value]


def _verify_review_receipt(receipt: object, *, consumption: HaltReleaseChallengeConsumptionRequest, authority_id: str, authority_version: int, authority_public_key: bytes) -> bool:
    if type(receipt) is not HaltReleaseChallengeConsumptionReceipt:
        return False
    try:
        receipt.__post_init__()
        if receipt.authority_id != authority_id or receipt.authority_version != authority_version:
            return False
        request_fingerprint = hashlib.sha256(consumption.canonical_bytes()).hexdigest()
        invocation_digest = hashlib.sha256(consumption.authority_invocation_challenge).hexdigest()
        receipt_digest = _challenge_receipt_digest(authority_id, authority_version, consumption)
        payload = _challenge_receipt_payload(authority_id, authority_version, consumption, receipt_digest)
        if not (hmac.compare_digest(receipt.request_fingerprint, request_fingerprint) and hmac.compare_digest(receipt.invocation_challenge_digest, invocation_digest) and hmac.compare_digest(receipt.receipt_digest, receipt_digest)):
            return False
        signature = _canonical_ed25519_signature(receipt.signature_b64url)
        Ed25519PublicKey.from_public_bytes(authority_public_key).verify(signature, payload)
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False


def evaluate_halt_release(request: object, evidence_scope: object, attestation: object, *, expected_account_binding_hash: object, expected_original_audited_action_fingerprint: object, expected_broker_acknowledgement: object, expected_ambiguity_marker_identity: object, expected_ambiguity_marker_digest: object, authority_id: object, authority_version: object, authority_public_key: object, review_challenge_authority: ExternalReviewChallengeAuthority, review_challenge_authority_id: object, review_challenge_authority_version: object, review_challenge_authority_public_key: object, clock: Callable[[], datetime], random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> HaltReleaseDecision:
    """Pure fail-closed decision; it cannot clear, delete, or mutate a halt marker."""
    if type(request) is not HaltReleaseRequest:
        return HaltReleaseDecision.UNRESOLVED
    try:
        request.__post_init__()
        now = _utc(clock(), "clock result")
    except (TypeError, ValueError, BaseException):
        return HaltReleaseDecision.UNRESOLVED
    if (request.submitted_at > now or request.requested_at > now or
            now - request.requested_at > _MAX_FRESHNESS):
        return HaltReleaseDecision.UNRESOLVED
    try:
        snapshot = _expected_snapshot(expected_account_binding_hash=expected_account_binding_hash, expected_original_audited_action_fingerprint=expected_original_audited_action_fingerprint, expected_broker_acknowledgement=expected_broker_acknowledgement, expected_ambiguity_marker_identity=expected_ambiguity_marker_identity, expected_ambiguity_marker_digest=expected_ambiguity_marker_digest, authority_id=authority_id, authority_version=authority_version, authority_public_key=authority_public_key, review_challenge_authority_id=review_challenge_authority_id, review_challenge_authority_version=review_challenge_authority_version, review_challenge_authority_public_key=review_challenge_authority_public_key)
    except (TypeError, ValueError):
        return HaltReleaseDecision.UNRESOLVED
    expected_account_binding, expected_original_fingerprint, expected_branch_id, expected_order_id, expected_marker_identity, expected_marker_digest, expected_authority_id, expected_authority_version, expected_authority_public_key, expected_review_authority_id, expected_review_authority_version, expected_review_authority_public_key = snapshot
    if (not hmac.compare_digest(request.expected_account_binding_hash, expected_account_binding) or
            not hmac.compare_digest(request.original_audited_action_fingerprint, expected_original_fingerprint) or
            not hmac.compare_digest(request.ambiguity_marker_identity, expected_marker_identity) or
            not hmac.compare_digest(request.ambiguity_marker_digest, expected_marker_digest) or
            request.broker_acknowledgement.branch_id != expected_branch_id or request.broker_acknowledgement.order_id != expected_order_id):
        return HaltReleaseDecision.CONTRADICTION
    if not _valid_scope(evidence_scope, request, now):
        return HaltReleaseDecision.UNRESOLVED
    if type(attestation) is not AuthoritativeReconciliationAttestation:
        return HaltReleaseDecision.UNRESOLVED
    try:
        attestation.__post_init__()
        if attestation.authority_id != expected_authority_id or attestation.authority_version != expected_authority_version:
            return HaltReleaseDecision.UNRESOLVED
        request_fingerprint, scope_digest = request.fingerprint(), evidence_scope.canonical_digest()
        if not hmac.compare_digest(attestation.request_fingerprint, request_fingerprint) or not hmac.compare_digest(attestation.evidence_scope_digest, scope_digest):
            return HaltReleaseDecision.UNRESOLVED
        payload = _attestation_payload(attestation, request_fingerprint, scope_digest)
        expected_digest = hashlib.sha256(b"authoritative-halt-release-attestation-digest-v1\0" + payload).hexdigest()
        if not hmac.compare_digest(attestation.attestation_digest, expected_digest):
            return HaltReleaseDecision.UNRESOLVED
        raw_signature = _canonical_ed25519_signature(attestation.signature_b64url)
        Ed25519PublicKey.from_public_bytes(expected_authority_public_key).verify(raw_signature, payload)
    except (ValueError, TypeError, InvalidSignature):
        return HaltReleaseDecision.UNRESOLVED
    if attestation.disposition is AttestationDisposition.CONTRADICTION:
        return HaltReleaseDecision.CONTRADICTION
    if now > request.requested_at + _REVIEW_CHALLENGE_TTL:
        return HaltReleaseDecision.UNRESOLVED
    try:
        if not callable(random_bytes):
            return HaltReleaseDecision.UNRESOLVED
        invocation_challenge = random_bytes(32)
        if type(invocation_challenge) is not bytes or len(invocation_challenge) != 32:
            return HaltReleaseDecision.UNRESOLVED
        consumption = HaltReleaseChallengeConsumptionRequest(
            EXTERNAL_REVIEW_CHALLENGE_PROTOCOL_ID, EXTERNAL_REVIEW_CHALLENGE_PROTOCOL_VERSION,
            request_fingerprint, expected_account_binding, request.operator_review_challenge,
            request.requested_at + _REVIEW_CHALLENGE_TTL, invocation_challenge)
        consume = getattr(review_challenge_authority, "consume")
        if not callable(consume):
            return HaltReleaseDecision.UNRESOLVED
        consumed = consume(consumption)
        # Validate the signed receipt before observing caller-controlled values again.
        if not _verify_review_receipt(consumed, consumption=consumption, authority_id=expected_review_authority_id, authority_version=expected_review_authority_version, authority_public_key=expected_review_authority_public_key):
            return HaltReleaseDecision.UNRESOLVED
        # The external authority can run arbitrary code: revalidate every expected input snapshot.
        if _expected_snapshot(expected_account_binding_hash=expected_account_binding_hash, expected_original_audited_action_fingerprint=expected_original_audited_action_fingerprint, expected_broker_acknowledgement=expected_broker_acknowledgement, expected_ambiguity_marker_identity=expected_ambiguity_marker_identity, expected_ambiguity_marker_digest=expected_ambiguity_marker_digest, authority_id=authority_id, authority_version=authority_version, authority_public_key=authority_public_key, review_challenge_authority_id=review_challenge_authority_id, review_challenge_authority_version=review_challenge_authority_version, review_challenge_authority_public_key=review_challenge_authority_public_key) != snapshot:
            return HaltReleaseDecision.UNRESOLVED
        request.__post_init__()
        if not _valid_scope(evidence_scope, request, now):
            return HaltReleaseDecision.UNRESOLVED
        if request.fingerprint() != request_fingerprint or cast(ReconciliationEvidenceScope, evidence_scope).canonical_digest() != scope_digest:
            return HaltReleaseDecision.UNRESOLVED
    except (TypeError, ValueError, BaseException):
        return HaltReleaseDecision.UNRESOLVED
    return HaltReleaseDecision.ELIGIBLE_FOR_SEPARATE_OPERATOR_REVIEW
