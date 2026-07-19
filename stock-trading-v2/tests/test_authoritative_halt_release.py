"""TDD coverage for the non-executing authoritative halt-release evidence contract."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import ast
import inspect
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from swing_v2.live.authoritative_halt_release import (
    AUTHORITATIVE_HALT_RELEASE_PROTOCOL_VERSION,
    OPERATOR_REVIEW_PHRASE,
    AuthoritativeReconciliationAttestation,
    AttestationDisposition,
    BrokerAcknowledgementReference,
    HaltReleaseChallengeConsumptionRequest,
    HaltReleaseChallengeConsumptionReceipt,
    HaltReleaseDecision,
    HaltReleaseRequest,
    ReconciliationEvidenceScope,
    evaluate_halt_release,
    sign_attestation_for_test,
    sign_challenge_consumption_receipt_for_test,
)

NOW = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)
PRIVATE_SEED = b"a" * 32
PUBLIC_KEY = Ed25519PrivateKey.from_private_bytes(PRIVATE_SEED).public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)
REVIEW_PRIVATE_SEED = b"s" * 32
REVIEW_PUBLIC_KEY = Ed25519PrivateKey.from_private_bytes(REVIEW_PRIVATE_SEED).public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)
REVIEW_AUTHORITY_ID = "external-review-challenge-authority"
REVIEW_AUTHORITY_VERSION = 1


class InMemoryReviewChallengeAuthority:
    """Non-production signer fixture; production authority is separately privileged."""
    def __init__(self) -> None:
        self.consumed: set[tuple[str, bytes]] = set()
        self.calls: list[HaltReleaseChallengeConsumptionRequest] = []
        self.receipts: list[HaltReleaseChallengeConsumptionReceipt] = []

    def consume(self, item: HaltReleaseChallengeConsumptionRequest) -> object:
        self.calls.append(item)
        binding = (item.release_request_fingerprint, item.operator_review_challenge)
        if binding in self.consumed:
            raise ValueError("replayed")
        self.consumed.add(binding)
        receipt = sign_challenge_consumption_receipt_for_test(
            authority_id=REVIEW_AUTHORITY_ID, authority_version=REVIEW_AUTHORITY_VERSION,
            request=item, private_seed=REVIEW_PRIVATE_SEED,
        )
        self.receipts.append(receipt)
        return receipt


class MalformedReviewChallengeAuthority:
    def consume(self, item: object) -> object:
        return None


class CachedReceiptReviewChallengeAuthority:
    def __init__(self, receipt: HaltReleaseChallengeConsumptionReceipt) -> None:
        self.receipt = receipt
        self.calls: list[HaltReleaseChallengeConsumptionRequest] = []

    def consume(self, request: HaltReleaseChallengeConsumptionRequest) -> HaltReleaseChallengeConsumptionReceipt:
        self.calls.append(request)
        return self.receipt


class MutatingReviewChallengeAuthority:
    def __init__(self, acknowledgement: BrokerAcknowledgementReference) -> None:
        self.acknowledgement = acknowledgement

    def consume(self, request: HaltReleaseChallengeConsumptionRequest) -> HaltReleaseChallengeConsumptionReceipt:
        object.__setattr__(self.acknowledgement, "order_id", "mutated-order")
        return sign_challenge_consumption_receipt_for_test(
            authority_id=REVIEW_AUTHORITY_ID, authority_version=REVIEW_AUTHORITY_VERSION,
            request=request, private_seed=REVIEW_PRIVATE_SEED,
        )


def reference(*, branch: str = "01234", order: str = "12345678") -> BrokerAcknowledgementReference:
    return BrokerAcknowledgementReference(branch, order)


def request(**changes: object) -> HaltReleaseRequest:
    values: dict[str, object] = {
        "protocol_version": AUTHORITATIVE_HALT_RELEASE_PROTOCOL_VERSION,
        "expected_account_binding_hash": "a" * 64,
        "original_audited_action_fingerprint": "b" * 64,
        "broker_acknowledgement": reference(),
        "ambiguity_marker_identity": "halt-marker-opaque-id",
        "ambiguity_marker_digest": "c" * 64,
        "submitted_at": NOW - timedelta(seconds=30),
        "requested_at": NOW - timedelta(seconds=10),
        "operator_review_phrase": OPERATOR_REVIEW_PHRASE,
        "operator_review_challenge": b"r" * 32,
    }
    values.update(changes)
    return HaltReleaseRequest(**values)  # type: ignore[arg-type]


def scope(**changes: object) -> ReconciliationEvidenceScope:
    values: dict[str, object] = {
        "observed_started_at": NOW - timedelta(seconds=20),
        "observed_ended_at": NOW - timedelta(seconds=5),
        "open_orders_source_id": "authority-open-orders-query",
        "open_orders_complete": True,
        "fills_source_id": "authority-fills-query",
        "fills_range_started_at": NOW - timedelta(minutes=5),
        "fills_range_ended_at": NOW - timedelta(seconds=5),
        "fills_bounded": True,
        "fills_page_complete": True,
        "balance_source_id": "authority-balance-query",
        "balance_complete": True,
        "positions_source_id": "authority-positions-query",
        "positions_complete": True,
        "cash_source_id": "authority-cash-query",
        "cash_complete": True,
        "atomic_collection_id": "authority-atomic-generation-7",
        "atomic_collection_declared": True,
    }
    values.update(changes)
    return ReconciliationEvidenceScope(**values)  # type: ignore[arg-type]


def attestation(item: HaltReleaseRequest, evidence: ReconciliationEvidenceScope, *, disposition: AttestationDisposition = AttestationDisposition.NO_CONTRADICTION, **changes: object) -> AuthoritativeReconciliationAttestation:
    values: dict[str, object] = {
        "authority_id": "external-reconciliation-authority",
        "authority_version": 1,
        "request_fingerprint": item.fingerprint(),
        "evidence_scope_digest": evidence.canonical_digest(),
        "disposition": disposition,
        "attestation_id": "authority-attestation-opaque-id",
    }
    values.update(changes)
    unsigned = AuthoritativeReconciliationAttestation(signature_b64url="A" * 86, **values)  # type: ignore[arg-type]
    return sign_attestation_for_test(unsigned, request=item, evidence_scope=evidence, private_seed=PRIVATE_SEED)


def evaluate(item: HaltReleaseRequest, evidence: ReconciliationEvidenceScope, signed: AuthoritativeReconciliationAttestation, **changes: object) -> HaltReleaseDecision:
    values: dict[str, object] = {
        "expected_account_binding_hash": "a" * 64,
        "expected_original_audited_action_fingerprint": "b" * 64,
        "expected_broker_acknowledgement": reference(),
        "expected_ambiguity_marker_identity": "halt-marker-opaque-id",
        "expected_ambiguity_marker_digest": "c" * 64,
        "authority_id": "external-reconciliation-authority",
        "authority_version": 1,
        "authority_public_key": PUBLIC_KEY,
        "review_challenge_authority": InMemoryReviewChallengeAuthority(),
        "review_challenge_authority_id": REVIEW_AUTHORITY_ID,
        "review_challenge_authority_version": REVIEW_AUTHORITY_VERSION,
        "review_challenge_authority_public_key": REVIEW_PUBLIC_KEY,
        "random_bytes": lambda size: b"i" * size,
        "clock": lambda: NOW,
    }
    values.update(changes)
    return evaluate_halt_release(item, evidence, signed, **values)  # type: ignore[arg-type]


class AuthoritativeHaltReleaseTest(unittest.TestCase):
    def test_exact_current_marker_identity_and_digest_are_required_and_mismatch_is_contradiction(self) -> None:
        item, evidence = request(), scope()
        signed = attestation(item, evidence)
        self.assertIs(evaluate(item, evidence, signed, expected_ambiguity_marker_identity="other-marker"), HaltReleaseDecision.CONTRADICTION)
        self.assertIs(evaluate(item, evidence, signed, expected_ambiguity_marker_digest="d" * 64), HaltReleaseDecision.CONTRADICTION)

    def test_shared_external_authority_consumes_a_signed_request_once_across_fresh_evaluators(self) -> None:
        item, evidence = request(), scope()
        signed, authority = attestation(item, evidence), InMemoryReviewChallengeAuthority()
        self.assertIs(evaluate(item, evidence, signed, review_challenge_authority=authority), HaltReleaseDecision.ELIGIBLE_FOR_SEPARATE_OPERATOR_REVIEW)
        self.assertIs(evaluate(item, evidence, signed, review_challenge_authority=authority), HaltReleaseDecision.UNRESOLVED)
        self.assertEqual(len(authority.calls), 2)
        consumed = authority.calls[0]
        self.assertEqual(consumed.release_request_fingerprint, item.fingerprint())
        self.assertEqual(consumed.expected_account_binding_hash, "a" * 64)
        self.assertEqual(consumed.operator_review_challenge, b"r" * 32)
        self.assertEqual(consumed.expires_at, item.requested_at + timedelta(seconds=60))

    def test_expired_review_challenge_is_not_sent_to_authority(self) -> None:
        item, evidence = request(submitted_at=NOW - timedelta(seconds=70), requested_at=NOW - timedelta(seconds=61)), scope(fills_range_started_at=NOW - timedelta(seconds=70))
        authority = InMemoryReviewChallengeAuthority()
        self.assertIs(evaluate(item, evidence, attestation(item, evidence), review_challenge_authority=authority), HaltReleaseDecision.UNRESOLVED)
        self.assertEqual(authority.calls, [])

    def test_malformed_or_failed_challenge_authority_never_makes_decision_eligible(self) -> None:
        item, evidence = request(), scope()
        self.assertIs(evaluate(item, evidence, attestation(item, evidence), review_challenge_authority=MalformedReviewChallengeAuthority()), HaltReleaseDecision.UNRESOLVED)

    def test_mutation_after_challenge_consumption_fails_closed(self) -> None:
        item, evidence = request(), scope()
        expected_acknowledgement = reference()
        self.assertIs(evaluate(item, evidence, attestation(item, evidence), expected_broker_acknowledgement=expected_acknowledgement, review_challenge_authority=MutatingReviewChallengeAuthority(expected_acknowledgement)), HaltReleaseDecision.UNRESOLVED)

    def test_cached_genuine_receipt_from_another_evaluator_invocation_is_unresolved(self) -> None:
        item, evidence = request(), scope()
        genuine_authority = InMemoryReviewChallengeAuthority()
        self.assertIs(evaluate(item, evidence, attestation(item, evidence), review_challenge_authority=genuine_authority, random_bytes=lambda size: b"1" * size), HaltReleaseDecision.ELIGIBLE_FOR_SEPARATE_OPERATOR_REVIEW)
        cached = CachedReceiptReviewChallengeAuthority(genuine_authority.receipts[0])
        self.assertIs(evaluate(item, evidence, attestation(item, evidence), review_challenge_authority=cached, random_bytes=lambda size: b"2" * size), HaltReleaseDecision.UNRESOLVED)
        self.assertEqual(len(cached.calls), 1)

    def test_synthetic_forged_swapped_and_wrong_key_review_receipts_are_unresolved(self) -> None:
        item, evidence = request(), scope()
        consumed = HaltReleaseChallengeConsumptionRequest("external-review-challenge-consumption-v1", 1, item.fingerprint(), "a" * 64, b"r" * 32, item.requested_at + timedelta(seconds=60), b"i" * 32)
        genuine = sign_challenge_consumption_receipt_for_test(authority_id=REVIEW_AUTHORITY_ID, authority_version=REVIEW_AUTHORITY_VERSION, request=consumed, private_seed=REVIEW_PRIVATE_SEED)
        synthetic = HaltReleaseChallengeConsumptionReceipt(REVIEW_AUTHORITY_ID, REVIEW_AUTHORITY_VERSION, genuine.request_fingerprint, genuine.invocation_challenge_digest, genuine.receipt_digest, "A" * 86)
        cases = (
            (CachedReceiptReviewChallengeAuthority(synthetic), {}),
            (CachedReceiptReviewChallengeAuthority(genuine), {"review_challenge_authority_public_key": b"z" * 32}),
            (CachedReceiptReviewChallengeAuthority(replace(genuine, authority_id="other")), {}),
            (CachedReceiptReviewChallengeAuthority(replace(genuine, invocation_challenge_digest="0" * 64)), {}),
        )
        for authority, changes in cases:
            with self.subTest(authority=authority, changes=changes):
                self.assertIs(evaluate(item, evidence, attestation(item, evidence), review_challenge_authority=authority, **changes), HaltReleaseDecision.UNRESOLVED)

    def test_malformed_or_failing_invocation_randomness_rejects_before_authority(self) -> None:
        item, evidence = request(), scope(); authority = InMemoryReviewChallengeAuthority()
        for random_bytes in (lambda _size: b"x" * 31, lambda _size: (_ for _ in ()).throw(RuntimeError("rng failed"))):
            with self.subTest(random_bytes=random_bytes):
                self.assertIs(evaluate(item, evidence, attestation(item, evidence), review_challenge_authority=authority, random_bytes=random_bytes), HaltReleaseDecision.UNRESOLVED)
        self.assertEqual(authority.calls, [])

    def test_scope_requires_fresh_bounded_observation_and_fill_range_containing_submission(self) -> None:
        item = request(submitted_at=NOW - timedelta(days=20))
        valid_old_submission_scope = scope(fills_range_started_at=NOW - timedelta(days=20))
        self.assertIs(evaluate(item, valid_old_submission_scope, attestation(item, valid_old_submission_scope)), HaltReleaseDecision.ELIGIBLE_FOR_SEPARATE_OPERATOR_REVIEW)
        invalid_scopes = (
            scope(observed_started_at=NOW - timedelta(seconds=121)),
            scope(observed_started_at=NOW - timedelta(seconds=130), observed_ended_at=NOW - timedelta(seconds=121), fills_range_ended_at=NOW - timedelta(seconds=121)),
            scope(observed_started_at=NOW - timedelta(seconds=121), observed_ended_at=NOW),
            scope(fills_range_ended_at=NOW - timedelta(seconds=1)),
            scope(fills_range_started_at=NOW - timedelta(days=32)),
            scope(fills_range_started_at=NOW - timedelta(days=10)),
        )
        for evidence in invalid_scopes:
            with self.subTest(evidence=evidence):
                self.assertIs(evaluate(item, evidence, attestation(item, evidence)), HaltReleaseDecision.UNRESOLVED)

    def test_only_complete_fresh_atomic_exact_signed_evidence_is_eligible_for_separate_operator_review(self) -> None:
        item, evidence = request(), scope()
        result = evaluate(item, evidence, attestation(item, evidence))
        self.assertIs(result, HaltReleaseDecision.ELIGIBLE_FOR_SEPARATE_OPERATOR_REVIEW)
        self.assertNotIn("CLEAR", result.value)

    def test_any_missing_partial_stale_or_non_atomic_source_is_unresolved(self) -> None:
        for changes in (
            {"open_orders_complete": False}, {"fills_bounded": False}, {"fills_page_complete": False},
            {"balance_complete": False}, {"positions_complete": False}, {"cash_complete": False},
            {"atomic_collection_declared": False}, {"observed_started_at": NOW - timedelta(seconds=130), "observed_ended_at": NOW - timedelta(seconds=121), "fills_range_ended_at": NOW - timedelta(seconds=121)},
        ):
            with self.subTest(changes=changes):
                item, evidence = request(), scope(**changes)
                self.assertIs(evaluate(item, evidence, attestation(item, evidence)), HaltReleaseDecision.UNRESOLVED)

    def test_material_request_mismatch_and_authority_contradiction_are_contradictions(self) -> None:
        item, evidence = request(), scope()
        signed = attestation(item, evidence)
        self.assertIs(evaluate(item, evidence, signed, expected_account_binding_hash="d" * 64), HaltReleaseDecision.CONTRADICTION)
        self.assertIs(evaluate(item, evidence, signed, expected_original_audited_action_fingerprint="d" * 64), HaltReleaseDecision.CONTRADICTION)
        self.assertIs(evaluate(item, evidence, signed, expected_broker_acknowledgement=reference(order="other")), HaltReleaseDecision.CONTRADICTION)
        contradictory = attestation(item, evidence, disposition=AttestationDisposition.CONTRADICTION)
        self.assertIs(evaluate(item, evidence, contradictory), HaltReleaseDecision.CONTRADICTION)

    def test_forged_swapped_wrong_key_and_prior_challenge_attestations_are_unresolved(self) -> None:
        item, evidence = request(), scope(); valid = attestation(item, evidence)
        forged = AuthoritativeReconciliationAttestation(valid.authority_id, valid.authority_version, valid.request_fingerprint, valid.evidence_scope_digest, valid.disposition, valid.attestation_id, valid.attestation_digest, "B" * 86)
        self.assertIs(evaluate(item, evidence, forged), HaltReleaseDecision.UNRESOLVED)
        self.assertIs(evaluate(item, evidence, valid, authority_public_key=b"z" * 32), HaltReleaseDecision.UNRESOLVED)
        prior = request(operator_review_challenge=b"p" * 32)
        cached = attestation(prior, evidence)
        self.assertIs(evaluate(item, evidence, cached), HaltReleaseDecision.UNRESOLVED)

    def test_noncanonical_attestation_signature_encodings_are_unresolved_before_authority_consumption(self) -> None:
        item, evidence = request(), scope(); valid = attestation(item, evidence)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        alternate_final = alphabet[alphabet.index(valid.signature_b64url[-1]) ^ 1]
        noncanonical = (
            valid.signature_b64url[:-1] + alternate_final,
            valid.signature_b64url + "=",
            valid.signature_b64url[:-1] + "!",
        )
        for signature_b64url in noncanonical:
            with self.subTest(signature_b64url=signature_b64url):
                authority, candidate = InMemoryReviewChallengeAuthority(), replace(valid)
                object.__setattr__(candidate, "signature_b64url", signature_b64url)
                self.assertIs(evaluate(item, evidence, candidate, review_challenge_authority=authority), HaltReleaseDecision.UNRESOLVED)
                self.assertEqual(authority.calls, [])

    def test_mutation_toctou_and_spoofed_values_fail_closed_without_side_effect(self) -> None:
        item, evidence = request(), scope(); signed = attestation(item, evidence)
        object.__setattr__(item, "original_audited_action_fingerprint", "e" * 64)
        self.assertIs(evaluate(item, evidence, signed), HaltReleaseDecision.CONTRADICTION)
        item, evidence = request(), scope(); signed = attestation(item, evidence)
        object.__setattr__(evidence, "cash_complete", False)
        self.assertIs(evaluate(item, evidence, signed), HaltReleaseDecision.UNRESOLVED)
        class SpoofedString(str):
            def __eq__(self, other: object) -> bool: return True
        with self.assertRaises(ValueError): request(ambiguity_marker_identity=SpoofedString("opaque"))
        with self.assertRaises(FrozenInstanceError): setattr(signed, "attestation_id", "changed")

    def test_bad_times_collision_and_no_sensitive_raw_values_are_rejected_or_hidden(self) -> None:
        with self.assertRaises(ValueError): request(submitted_at=NOW + timedelta(seconds=1))
        with self.assertRaises(ValueError): request(requested_at=NOW - timedelta(seconds=31))
        with self.assertRaises(ValueError): request(broker_acknowledgement=reference(branch="same", order="same"))
        expired = request(submitted_at=NOW - timedelta(seconds=180), requested_at=NOW - timedelta(seconds=121))
        fresh_scope = scope()
        self.assertIs(evaluate(expired, fresh_scope, attestation(expired, fresh_scope)), HaltReleaseDecision.UNRESOLVED)
        item, evidence = request(), scope()
        signed = attestation(item, evidence)
        self.assertFalse(hasattr(signed, "operator_review_challenge")); self.assertFalse(hasattr(signed, "account_number")); self.assertFalse(hasattr(signed, "balances"))
        self.assertNotIn(b"r" * 32, repr(signed).encode())

    def test_module_has_no_kis_submit_halt_mutation_network_or_filesystem_surface(self) -> None:
        import swing_v2.live.authoritative_halt_release as module
        tree = ast.parse(inspect.getsource(module))
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        methods = {node.name.lower() for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertFalse({"requests", "httpx", "urllib", "production_execution", "production_reconciliation", "kis", "os", "pathlib", "socket", "dotenv"} & imports)
        self.assertFalse({"submit", "cancel", "amend", "clear_halt", "delete_halt", "unlink", "write", "open", "get", "post"} & methods)


if __name__ == "__main__":
    unittest.main()
