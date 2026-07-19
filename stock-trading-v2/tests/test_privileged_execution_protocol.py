"""Tests for the non-submitting privileged executor authorization contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import swing_v2.live.privileged_protocol as protocol
from swing_v2.live.privileged_protocol import (
    AllowedBrokerAction,
    ExternalNonceConsumptionReceipt,
    ExternalNonceConsumptionRequest,
    OperatorAuthorizationRequest,
    PrivilegedAuthorizationIssuer,
    PrivilegedDispatchVerifier,
    OPERATOR_CONFIRMATION_PHRASE,
    PROTOCOL_VERSION,
    compute_account_binding_digest,
    compute_action_fingerprint,
)
from swing_v2.live.external_nonce_authority import (
    AuthorityIdentity, DurableNonceAuthorityCore, ExternalNonceAuthorityAdapter,
    PeerCredentials, UnixAuthorityWireHandler, WireAuthorityClient,
)


class DeterministicNonProductionNonceAuthority:
    """Test fixture only; it models a separately-owned durable authority contract."""

    authority_id = "test-nonce-authority"
    authority_version = 1

    def __init__(self) -> None:
        self._consumed: set[tuple[bytes, str, str, datetime]] = set()

    def consume_once(self, consumption: ExternalNonceConsumptionRequest) -> ExternalNonceConsumptionReceipt:
        if type(consumption) is not ExternalNonceConsumptionRequest:
            raise ValueError("malformed external nonce consumption request")
        key = (consumption.nonce, consumption.action_fingerprint, consumption.account_binding_digest, consumption.expires_at)
        if key in self._consumed:
            raise ValueError("nonce already consumed by non-production test authority")
        self._consumed.add(key)
        digest = hashlib.sha256(
            b"non-production-test-receipt-v1\0" + consumption.canonical_bytes()
        ).hexdigest()
        return ExternalNonceConsumptionReceipt(self.authority_id, self.authority_version, digest, hashlib.sha256(consumption.invocation_challenge).hexdigest(), digest, "A" * 86)


class PrivilegedExecutionProtocolTest(unittest.TestCase):
    def test_valid_signed_envelope_returns_non_executable_dispatch_with_external_receipt(self) -> None:
        now = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)
        key = b"k" * 32
        action = AllowedBrokerAction("KIS", "005930", "STOCK", "BUY", 3, Decimal("71000"), "LIMIT")
        request = self._request(now, action, nonce=b"n" * 32)
        dispatch = self._verifier(key, action, now, DeterministicNonProductionNonceAuthority()).verify_and_consume(
            PrivilegedAuthorizationIssuer(key).issue(request)
        )

        self.assertEqual(dispatch.symbol, "005930")
        self.assertEqual(dispatch.limit_price, Decimal("71000"))
        self.assertEqual(dispatch.action_fingerprint, request.action_fingerprint)
        self.assertEqual(dispatch.nonce_consumption_receipt.authority_id, "test-nonce-authority")
        self.assertEqual(dispatch.nonce_consumption_receipt.authority_version, 1)
        self.assertEqual(len(dispatch.nonce_consumption_receipt.receipt_digest), 64)
        self.assertFalse(hasattr(dispatch.nonce_consumption_receipt, "nonce"))
        self.assertFalse(hasattr(dispatch.nonce_consumption_receipt, "account_reference"))
        self.assertFalse(hasattr(dispatch.nonce_consumption_receipt, "key"))
        self.assertFalse(hasattr(dispatch.nonce_consumption_receipt, "invocation_challenge"))
        self.assertFalse(hasattr(dispatch, "invocation_challenge"))
        with self.assertRaises((AttributeError, TypeError)):
            dispatch.quantity = 4  # type: ignore[misc]

    def test_verifier_requires_an_external_nonce_authority_and_rejects_malformed_authority(self) -> None:
        now, key, action = self._setup()
        for malformed in (None, object()):
            with self.subTest(malformed=type(malformed).__name__), self.assertRaisesRegex(ValueError, "external nonce authority"):
                self._verifier(key, action, now, malformed)  # type: ignore[arg-type]
        envelope = PrivilegedAuthorizationIssuer(key).issue(self._request(now, action, nonce=b"u" * 32))
        with self.assertRaisesRegex(ValueError, "external nonce receipt"):
            self._verifier(key, action, now, _ReplayUnsafeAuthorityWithoutTypedReceipt()).verify_and_consume(envelope)

    def test_replay_is_rejected_by_a_fresh_verifier_using_the_same_external_authority(self) -> None:
        now, key, action = self._setup()
        authority = DeterministicNonProductionNonceAuthority()
        envelope = PrivilegedAuthorizationIssuer(key).issue(self._request(now, action, nonce=b"x" * 32))
        self._verifier(key, action, now, authority).verify_and_consume(envelope)
        with self.assertRaisesRegex(ValueError, "consumed"):
            self._verifier(key, action, now, authority).verify_and_consume(envelope)

    def test_fresh_verifier_rejects_cached_genuine_ed25519_receipt_for_prior_invocation(self) -> None:
        now, key, action = self._setup()
        envelope = PrivilegedAuthorizationIssuer(key).issue(self._request(now, action, nonce=b"w" * 32))
        identity = AuthorityIdentity("test-nonce-authority", 1, frozenset({2001}), "/run/test-nonce-authority.sock")
        public_key = Ed25519PrivateKey.from_private_bytes(b"p" * 32).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700)
            handler = UnixAuthorityWireHandler(DurableNonceAuthorityCore(identity=identity, database_path=root / "consumed.sqlite", authority_private_key=b"p" * 32, authority_uid=2000, clock=lambda: now), authority_uid=2000)
            cached: list[bytes] = []
            def first_exchange(payload: bytes) -> bytes:
                response = handler.handle_payload(payload, PeerCredentials(2001, 1, 1))
                cached.append(response)
                return response
            first_authority = ExternalNonceAuthorityAdapter(client=WireAuthorityClient(identity=identity, exchange=first_exchange), authority_public_key=public_key)
            self._verifier(key, action, now, first_authority, random_bytes=lambda _size: b"1" * 32).verify_and_consume(envelope)
            hostile_authority = ExternalNonceAuthorityAdapter(client=WireAuthorityClient(identity=identity, exchange=lambda _payload: cached[0]), authority_public_key=public_key)
            with self.assertRaisesRegex(ValueError, "receipt"):
                self._verifier(key, action, now, hostile_authority, random_bytes=lambda _size: b"2" * 32).verify_and_consume(envelope)

    def test_receipt_challenge_digest_substitution_fails_closed(self) -> None:
        now, key, action = self._setup()
        envelope = PrivilegedAuthorizationIssuer(key).issue(self._request(now, action, nonce=b"d" * 32))
        receipt = ExternalNonceConsumptionReceipt("test-nonce-authority", 1, "a" * 64, "0" * 64, "a" * 64, "A" * 86)
        with self.assertRaisesRegex(ValueError, "invocation challenge mismatch"):
            self._verifier(key, action, now, _ReceiptAuthority(receipt), random_bytes=lambda _size: b"d" * 32).verify_and_consume(envelope)

    def test_malformed_challenge_generator_fails_closed_before_authority(self) -> None:
        now, key, action = self._setup()
        authority = DeterministicNonProductionNonceAuthority()
        envelope = PrivilegedAuthorizationIssuer(key).issue(self._request(now, action, nonce=b"g" * 32))
        for generator in (lambda _size: b"short", lambda _size: bytearray(b"c" * 32), lambda _size: (_ for _ in ()).throw(RuntimeError("no entropy"))):
            with self.subTest(generator=generator), self.assertRaisesRegex(ValueError, "invocation challenge"):
                PrivilegedDispatchVerifier(
                    key=key, expected_account_binding=compute_account_binding_digest("account-reference-for-test"),
                    allowed_actions=frozenset({action}), external_nonce_authority=authority,
                    clock=lambda: now, random_bytes=generator,
                ).verify_and_consume(envelope)
            self.assertEqual(authority._consumed, set())

    def test_malformed_or_forged_external_receipts_fail_closed(self) -> None:
        now, key, action = self._setup()
        envelope = PrivilegedAuthorizationIssuer(key).issue(self._request(now, action, nonce=b"f" * 32))
        for authority in (
            _ReceiptAuthority(object()),
            _ReceiptAuthority(ExternalNonceConsumptionReceipt("wrong-authority", 1, "a" * 64, "a" * 64, "a" * 64, "A" * 86)),
            _ReceiptAuthority(_forged_receipt("test-nonce-authority", 1, "not-a-digest", "a" * 64, "a" * 64, "A" * 86)),
            _ReceiptAuthority(ExternalNonceConsumptionReceipt("test-nonce-authority", 2, "a" * 64, "a" * 64, "a" * 64, "A" * 86)),
        ):
            with self.subTest(authority=repr(authority)), self.assertRaisesRegex(ValueError, "external nonce receipt"):
                self._verifier(key, action, now, authority).verify_and_consume(envelope)

    def test_mutation_during_external_consumption_rejects_after_authority_call(self) -> None:
        now, key, action = self._setup()
        request = self._request(now, action, nonce=b"z" * 32)
        authority = _MutatingAuthority(request)
        with self.assertRaisesRegex(ValueError, "mutated during consume"):
            self._verifier(key, action, now, authority).verify_and_consume(PrivilegedAuthorizationIssuer(key).issue(request))
        self.assertEqual(authority.calls, 1)

    def test_signature_account_allowlist_and_timing_fail_closed_before_authority(self) -> None:
        now, key, action = self._setup()
        cases = (
            ("wrong key", lambda request: (PrivilegedAuthorizationIssuer(b"t" * 32).issue(request), key, action, now)),
            ("wrong account", lambda request: (PrivilegedAuthorizationIssuer(key).issue(request), key, action, now, "b" * 64)),
            ("allowlist", lambda request: (PrivilegedAuthorizationIssuer(key).issue(request), key, AllowedBrokerAction("KIS", "005930", "STOCK", "SELL", 1, Decimal("71000"), "LIMIT"), now)),
            ("expired", lambda request: (PrivilegedAuthorizationIssuer(key).issue(self._request(now - timedelta(seconds=31), action, nonce=request.nonce)), key, action, now)),
            ("future", lambda request: (PrivilegedAuthorizationIssuer(key).issue(self._request(now + timedelta(seconds=1), action, nonce=request.nonce)), key, action, now)),
        )
        for label, arrange in cases:
            with self.subTest(label=label):
                authority = DeterministicNonProductionNonceAuthority()
                request = self._request(now, action, nonce=(label.encode() + b"0" * 32)[:32])
                values = arrange(request)
                envelope, verifier_key, allowed, clock = values[:4]
                account = values[4] if len(values) == 5 else compute_account_binding_digest("account-reference-for-test")
                with self.assertRaises(ValueError):
                    PrivilegedDispatchVerifier(key=verifier_key, expected_account_binding=account, allowed_actions=frozenset({allowed}), external_nonce_authority=authority, clock=lambda: clock).verify_and_consume(envelope)
                self.assertEqual(authority._consumed, set())

    def test_tampered_signed_material_and_str_subtypes_are_rejected(self) -> None:
        now, key, action = self._setup()
        request = self._request(now, action, nonce=b"q" * 32)
        envelope = PrivilegedAuthorizationIssuer(key).issue(request)
        object.__setattr__(request, "quantity", 2)
        authority = DeterministicNonProductionNonceAuthority()
        with self.assertRaises(ValueError):
            self._verifier(key, action, now, authority).verify_and_consume(envelope)
        self.assertEqual(authority._consumed, set())
        class EvilStr(str):
            pass
        with self.assertRaises(ValueError):
            OperatorAuthorizationRequest(EvilStr(PROTOCOL_VERSION), "PRODUCTION", "a" * 64, "b" * 64, "c" * 64, "KIS", "005930", "STOCK", "BUY", 1, Decimal("1"), "LIMIT", now, now + timedelta(seconds=1), b"n" * 32, OPERATOR_CONFIRMATION_PHRASE)
        with self.assertRaises(ValueError):
            PrivilegedAuthorizationIssuer(type("ByteSubclass", (bytes,), {})(b"k" * 32))

    def test_production_protocol_has_no_local_nonce_registry_public_surface(self) -> None:
        forbidden = {"NonceRegistry", "NonceRegistryAnchor", "provision_nonce_registry_anchor"}
        self.assertFalse(forbidden & set(vars(protocol)))
        source = protocol.__file__
        self.assertIsNotNone(source)
        with open(source, encoding="utf-8") as handle:
            self.assertFalse(forbidden & {name for name in handle.read().split()})

    def test_protocol_module_exposes_no_transport_or_halt_api(self) -> None:
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(protocol))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        methods = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertFalse({"requests", "dotenv", "production_execution", "production_reconciliation"} & imported)
        self.assertFalse({"get", "post", "clear_halt", "submit"} & methods)

    @staticmethod
    def _setup() -> tuple[datetime, bytes, AllowedBrokerAction]:
        return datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc), b"s" * 32, AllowedBrokerAction("KIS", "005930", "STOCK", "BUY", 1, Decimal("71000"), "LIMIT")

    @staticmethod
    def _request(now: datetime, action: AllowedBrokerAction, *, nonce: bytes) -> OperatorAuthorizationRequest:
        return OperatorAuthorizationRequest(PROTOCOL_VERSION, "PRODUCTION", compute_account_binding_digest("account-reference-for-test"), "a" * 64, compute_action_fingerprint(action), action.broker, action.symbol, action.classification, action.side, action.quantity, action.limit_price, action.order_mode, now, now + timedelta(seconds=30), nonce, OPERATOR_CONFIRMATION_PHRASE)

    @staticmethod
    def _verifier(key: bytes, action: AllowedBrokerAction, now: datetime, authority: object, random_bytes=lambda _size: b"c" * 32) -> PrivilegedDispatchVerifier:
        return PrivilegedDispatchVerifier(key=key, expected_account_binding=compute_account_binding_digest("account-reference-for-test"), allowed_actions=frozenset({action}), external_nonce_authority=authority, clock=lambda: now, random_bytes=random_bytes)


def _forged_receipt(authority_id: str, authority_version: int, request_fingerprint: str, invocation_challenge_digest: str, receipt_digest: str, signature_b64url: str) -> ExternalNonceConsumptionReceipt:
    """Bypass constructor only to model a malicious authority response."""
    receipt = object.__new__(ExternalNonceConsumptionReceipt)
    object.__setattr__(receipt, "authority_id", authority_id)
    object.__setattr__(receipt, "authority_version", authority_version)
    object.__setattr__(receipt, "request_fingerprint", request_fingerprint)
    object.__setattr__(receipt, "invocation_challenge_digest", invocation_challenge_digest)
    object.__setattr__(receipt, "receipt_digest", receipt_digest)
    object.__setattr__(receipt, "signature_b64url", signature_b64url)
    return receipt


class _ReplayUnsafeAuthorityWithoutTypedReceipt:
    authority_id = "test-nonce-authority"
    authority_version = 1

    def consume_once(self, consumption: ExternalNonceConsumptionRequest) -> object:
        return object()


class _ReceiptAuthority:
    authority_id = "test-nonce-authority"
    authority_version = 1

    def __init__(self, receipt: object) -> None:
        self._receipt = receipt

    def consume_once(self, consumption: ExternalNonceConsumptionRequest) -> object:
        return self._receipt


class _MutatingAuthority(DeterministicNonProductionNonceAuthority):
    def __init__(self, request: OperatorAuthorizationRequest) -> None:
        super().__init__()
        self._request = request
        self.calls = 0

    def consume_once(self, consumption: ExternalNonceConsumptionRequest) -> ExternalNonceConsumptionReceipt:
        self.calls += 1
        receipt = super().consume_once(consumption)
        object.__setattr__(self._request, "quantity", 2)
        return receipt


if __name__ == "__main__":
    unittest.main()
