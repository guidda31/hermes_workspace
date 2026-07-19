"""TDD coverage for the inactive external nonce-consumption authority template."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from swing_v2.live.privileged_protocol import ExternalNonceConsumptionRequest
from swing_v2.live.external_nonce_authority import (
    AuthorityIdentity, DurableNonceAuthorityCore, ExternalNonceAuthorityAdapter,
    PeerCredentials, UnixAuthorityWireHandler, WireAuthorityClient,
)

NOW = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)
PRIVATE_SEED = b"a" * 32
PUBLIC_KEY = Ed25519PrivateKey.from_private_bytes(PRIVATE_SEED).public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)


def request(*, expires_at: datetime | None = None, nonce: bytes = b"n" * 32, invocation_challenge: bytes = b"c" * 32) -> ExternalNonceConsumptionRequest:
    return ExternalNonceConsumptionRequest(nonce, "a" * 64, "b" * 64, expires_at or NOW + timedelta(seconds=30), invocation_challenge)


def identity(*, executor_uids: frozenset[int] = frozenset({2001}), socket_path: str = "/run/kis-nonce-authority/authority.sock") -> AuthorityIdentity:
    return AuthorityIdentity("kis-nonce-authority", 1, executor_uids, socket_path)


class ExternalNonceAuthorityTest(unittest.TestCase):
    def _core(self, root: Path, *, database: str = "db.sqlite") -> DurableNonceAuthorityCore:
        return DurableNonceAuthorityCore(identity=identity(), database_path=root / database, authority_private_key=PRIVATE_SEED, authority_uid=2000, clock=lambda: NOW)

    def test_first_consumption_receipt_is_signed_typed_and_reopen_rejects_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700)
            database = root / "consumed.sqlite3"
            core = self._core(root, database="consumed.sqlite3")
            receipt = core.consume_once(request())
            self.assertEqual((receipt.authority_id, receipt.authority_version), ("kis-nonce-authority", 1))
            self.assertEqual(len(receipt.request_fingerprint), 64)
            self.assertEqual(len(receipt.receipt_digest), 64)
            self.assertEqual(len(receipt.signature_b64url), 86)
            self.assertFalse(hasattr(receipt, "nonce")); self.assertFalse(hasattr(receipt, "account_reference")); self.assertFalse(hasattr(receipt, "key")); self.assertFalse(hasattr(receipt, "invocation_challenge"))
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            self.assertNotIn(PRIVATE_SEED, database.read_bytes())
            with self.assertRaisesRegex(ValueError, "already consumed"):
                self._core(root, database="consumed.sqlite3").consume_once(request())

    def test_durable_nonce_replay_is_rejected_even_with_a_new_invocation_challenge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700); core = self._core(root)
            core.consume_once(request(nonce=b"r" * 32, invocation_challenge=b"1" * 32))
            with self.assertRaisesRegex(ValueError, "already consumed"):
                core.consume_once(request(nonce=b"r" * 32, invocation_challenge=b"2" * 32))

    def test_expired_and_malformed_request_fail_before_database_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700); core = self._core(root)
            with self.assertRaisesRegex(ValueError, "expired"): core.consume_once(request(expires_at=NOW))
            with self.assertRaisesRegex(ValueError, "exact ExternalNonceConsumptionRequest"): core.consume_once(object())  # type: ignore[arg-type]
            with sqlite3.connect(root / "db.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM consumed_requests").fetchone()[0], 0)

    def test_unsafe_parent_or_database_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); unsafe = root / "unsafe"; unsafe.mkdir(); os.chmod(unsafe, 0o755)
            with self.assertRaisesRegex(ValueError, "unsafe database parent"):
                DurableNonceAuthorityCore(identity=identity(), database_path=unsafe / "db.sqlite", authority_private_key=PRIVATE_SEED, authority_uid=2000, clock=lambda: NOW)
            safe = root / "safe"; safe.mkdir(); os.chmod(safe, 0o700)
            target = root / "target.sqlite"; target.write_bytes(b"not a database"); link = safe / "db.sqlite"; link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                DurableNonceAuthorityCore(identity=identity(), database_path=link, authority_private_key=PRIVATE_SEED, authority_uid=2000, clock=lambda: NOW)

    def test_thread_race_allows_exactly_one_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700); core = self._core(root); outcomes: list[str] = []
            def consume() -> None:
                try: core.consume_once(request())
                except ValueError: outcomes.append("rejected")
                else: outcomes.append("accepted")
            threads = [threading.Thread(target=consume) for _ in range(8)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual((outcomes.count("accepted"), outcomes.count("rejected")), (1, 7))

    def test_wire_requires_authenticated_distinct_allowlisted_peer_and_strict_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700)
            with self.assertRaisesRegex(ValueError, "authority UID"):
                DurableNonceAuthorityCore(identity=identity(executor_uids=frozenset({2000})), database_path=root / "same-uid.sqlite", authority_private_key=PRIVATE_SEED, authority_uid=2000, clock=lambda: NOW)
            handler = UnixAuthorityWireHandler(self._core(root), authority_uid=2000, max_request_bytes=1024)
            for peer in (None, PeerCredentials(2000, 1, 1), PeerCredentials(2002, 1, 1)):
                with self.subTest(peer=peer), self.assertRaisesRegex(ValueError, "peer"): handler.handle_payload(_wire_request(), peer)  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValueError, "duplicate JSON"): handler.handle_payload(b'{"nonce_b64url":"' + b'bg' * 22 + b'","nonce_b64url":"x"}', PeerCredentials(2001, 1, 1))
            malformed_challenge = _wire_request().replace(b'"invocation_challenge_b64url":"Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M="', b'"invocation_challenge_b64url":"not-base64!"')
            with self.assertRaisesRegex(ValueError, "request"): handler.handle_payload(malformed_challenge, PeerCredentials(2001, 1, 1))
            duplicate_challenge = _wire_request()[:-1] + b',"invocation_challenge_b64url":"' + base64.urlsafe_b64encode(b"d" * 32) + b'"}'
            with self.assertRaisesRegex(ValueError, "duplicate JSON"): handler.handle_payload(duplicate_challenge, PeerCredentials(2001, 1, 1))
            with self.assertRaisesRegex(ValueError, "too large"): handler.handle_payload(b"x" * 1025, PeerCredentials(2001, 1, 1))
            with self.assertRaisesRegex(ValueError, "schema"): handler.handle_payload(b"{}", PeerCredentials(2001, 1, 1))
            response = json.loads(handler.handle_payload(_wire_request(), PeerCredentials(2001, 1, 1)))
            self.assertEqual(set(response), {"authority_id", "authority_version", "request_fingerprint", "invocation_challenge_digest", "receipt_digest", "signature_b64url"})

    def test_adapter_verifies_ed25519_authority_receipt_and_wrong_public_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700); handler = UnixAuthorityWireHandler(self._core(root), authority_uid=2000)
            client = WireAuthorityClient(identity=identity(), exchange=lambda data: handler.handle_payload(data, PeerCredentials(2001, 1, 1)))
            receipt = ExternalNonceAuthorityAdapter(client=client, authority_public_key=PUBLIC_KEY).consume_once(request())
            self.assertEqual(receipt.authority_id, "kis-nonce-authority")
            with self.assertRaisesRegex(ValueError, "receipt"):
                ExternalNonceAuthorityAdapter(client=client, authority_public_key=b"z" * 32).consume_once(request(nonce=b"m" * 32))

    def test_cached_genuine_receipt_for_another_invocation_challenge_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700); handler = UnixAuthorityWireHandler(self._core(root), authority_uid=2000)
            genuine = handler.handle_payload(_wire_request(invocation_challenge=b"1" * 32), PeerCredentials(2001, 1, 1))
            cached_client = WireAuthorityClient(identity=identity(), exchange=lambda _data: genuine)
            with self.assertRaisesRegex(ValueError, "receipt"):
                ExternalNonceAuthorityAdapter(client=cached_client, authority_public_key=PUBLIC_KEY).consume_once(request(invocation_challenge=b"2" * 32))

    def test_public_key_only_malicious_wire_client_cannot_forge_or_swap_signed_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700); handler = UnixAuthorityWireHandler(self._core(root), authority_uid=2000)
            client = WireAuthorityClient(identity=identity(), exchange=lambda data: handler.handle_payload(data, PeerCredentials(2001, 1, 1)))
            adapter = ExternalNonceAuthorityAdapter(client=client, authority_public_key=PUBLIC_KEY)
            valid = adapter.consume_once(request(nonce=b"v" * 32))
            self.assertNotIn(PRIVATE_SEED, repr(adapter).encode()); self.assertNotIn(PRIVATE_SEED, repr(client).encode())
            self.assertFalse(any("private" in slot or "signer" in slot for slot in adapter.__slots__))
            self.assertFalse(any("private" in slot or "signer" in slot for slot in client.__slots__))
            forged = _wire_receipt(valid.authority_id, valid.authority_version, "0" * 64, "0" * 64, "0" * 64, "A" * 86)
            with self.assertRaisesRegex(ValueError, "receipt"):
                ExternalNonceAuthorityAdapter(client=WireAuthorityClient(identity=identity(), exchange=lambda _data: forged), authority_public_key=PUBLIC_KEY).consume_once(request(nonce=b"f" * 32))
            swapped = _wire_receipt(valid.authority_id, valid.authority_version, valid.request_fingerprint, valid.invocation_challenge_digest, valid.receipt_digest, valid.signature_b64url)
            wrong_identity = _wire_receipt("other-authority", valid.authority_version, valid.request_fingerprint, valid.invocation_challenge_digest, valid.receipt_digest, valid.signature_b64url)
            with self.assertRaisesRegex(ValueError, "identity"):
                ExternalNonceAuthorityAdapter(client=WireAuthorityClient(identity=identity(), exchange=lambda _data: wrong_identity), authority_public_key=PUBLIC_KEY).consume_once(request(nonce=b"i" * 32))
            with self.assertRaisesRegex(ValueError, "receipt"):
                ExternalNonceAuthorityAdapter(client=WireAuthorityClient(identity=identity(), exchange=lambda _data: swapped), authority_public_key=PUBLIC_KEY).consume_once(request(nonce=b"s" * 32))
            with self.assertRaisesRegex(ValueError, "receipt"):
                ExternalNonceAuthorityAdapter(client=WireAuthorityClient(identity=identity(), exchange=lambda _data: swapped), authority_public_key=PUBLIC_KEY).consume_once(ExternalNonceConsumptionRequest(b"v" * 32, "c" * 64, "b" * 64, NOW + timedelta(seconds=30), b"c" * 32))

    def test_private_and_public_key_inputs_require_exact_32_byte_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); os.chmod(root, 0o700)
            for bad_key in (b"x" * 31, bytearray(PRIVATE_SEED), type("BytesSubclass", (bytes,), {})(PRIVATE_SEED)):
                with self.subTest(type=type(bad_key).__name__), self.assertRaisesRegex(ValueError, "configuration"):
                    DurableNonceAuthorityCore(identity=identity(), database_path=root / "db.sqlite", authority_private_key=bad_key, authority_uid=2000, clock=lambda: NOW)  # type: ignore[arg-type]
            client = WireAuthorityClient(identity=identity(), exchange=lambda _data: b"{}")
            for bad_key in (b"x" * 31, bytearray(PUBLIC_KEY), type("BytesSubclass", (bytes,), {})(PUBLIC_KEY)):
                with self.subTest(type=type(bad_key).__name__), self.assertRaisesRegex(ValueError, "configuration"):
                    ExternalNonceAuthorityAdapter(client=client, authority_public_key=bad_key)  # type: ignore[arg-type]

    def test_module_has_no_broker_network_or_order_surface(self) -> None:
        import ast, inspect
        import swing_v2.live.external_nonce_authority as module
        tree = ast.parse(inspect.getsource(module)); imports = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}; methods = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertFalse({"requests", "httpx", "urllib", "KIS", "production_execution", "production_reconciliation"} & imports); self.assertFalse({"submit", "order", "post", "get"} & methods)


def _wire_request(*, invocation_challenge: bytes = b"c" * 32) -> bytes:
    return json.dumps({"nonce_b64url": base64.urlsafe_b64encode(b"n" * 32).decode("ascii"), "action_fingerprint": "a" * 64, "account_binding_digest": "b" * 64, "expires_at": (NOW + timedelta(seconds=30)).isoformat(), "invocation_challenge_b64url": base64.urlsafe_b64encode(invocation_challenge).decode("ascii")}, sort_keys=True, separators=(",", ":")).encode("ascii")


def _wire_receipt(authority_id: str, authority_version: int, request_fingerprint: str, invocation_challenge_digest: str, receipt_digest: str, signature_b64url: str) -> bytes:
    return json.dumps({"authority_id": authority_id, "authority_version": authority_version, "request_fingerprint": request_fingerprint, "invocation_challenge_digest": invocation_challenge_digest, "receipt_digest": receipt_digest, "signature_b64url": signature_b64url}, sort_keys=True, separators=(",", ":")).encode("ascii")


if __name__ == "__main__": unittest.main()
