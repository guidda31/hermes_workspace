"""TDD coverage for non-mutating deployment preflight boundaries."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
import ast
import base64
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swing_v2.live.deployment_preflight import (
    DeploymentInspection, DeploymentPreflightReport, FileMetadata, KeyDeclaration,
    Principal, ReasonCode, ServiceUnit, assess_deployment,
)

AUTHORITY = Principal("kis-nonce-authority", 2000, 3000, frozenset())
EXECUTOR = Principal("kis-executor", 2001, 3001, frozenset({3002}))
OPERATOR = Principal("operator", 1000, 1000, frozenset())
HARDENING = frozenset({"NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=yes", "UMask=0077"})
DIGEST = "a" * 64


def meta(kind: str, uid: int, gid: int, mode: int, *, symlink: bool = False, nlink: int = 1, present: bool = True) -> FileMetadata:
    return FileMetadata(kind, uid, gid, mode, symlink, nlink, present)


def authority_unit(**changes: object) -> ServiceUnit:
    values: dict[str, object] = dict(name="kis-nonce-authority.service", declared_path="/etc/systemd/system/kis-nonce-authority.service", user="kis-nonce-authority", group="kis-nonce-authority", launcher="/usr/local/libexec/kis-nonce-authority-launcher", launcher_template_only=False, hardening=HARDENING, effective_config_verified=True, effective_config_sha256=DIGEST)
    values.update(changes)
    return ServiceUnit(**values)  # type: ignore[arg-type]


def executor_unit(**changes: object) -> ServiceUnit:
    values: dict[str, object] = dict(name="kis-executor.service", declared_path="/etc/systemd/system/kis-executor.service", user="kis-executor", group="kis-executor", launcher="/usr/local/libexec/kis-executor-launcher", launcher_template_only=False, hardening=HARDENING, effective_config_verified=True, effective_config_sha256="b" * 64)
    values.update(changes)
    return ServiceUnit(**values)  # type: ignore[arg-type]


def good_inspection(**changes: object) -> DeploymentInspection:
    values: dict[str, object] = {
        "operator": OPERATOR, "authority": AUTHORITY, "executor": EXECUTOR,
        "authority_unit": authority_unit(), "executor_unit": executor_unit(),
        "authority_private_dir": meta("directory", 2000, 3000, 0o700),
        "authority_private_key": meta("regular", 2000, 3000, 0o600),
        "authority_database": meta("regular", 2000, 3000, 0o600),
        "executor_public_dir": meta("directory", 2001, 3001, 0o700),
        "executor_public_key": meta("regular", 2001, 3001, 0o644),
        "authority_public_key": KeyDeclaration(True, DIGEST),
        "executor_public_key_declaration": KeyDeclaration(True, DIGEST),
        "socket_dir": meta("directory", 2000, 3002, 0o750),
        "socket": meta("socket", 2000, 3002, 0o660),
        "socket_group": Principal("kis-authority-clients", 3002, 3002, frozenset()),
        "socket_group_members": frozenset({"kis-executor"}),
        "required_units_present": True, "active_units": frozenset(),
        "active_status_known": True, "peer_authentication_confirmed": False,
    }
    values.update(changes)
    return DeploymentInspection(**values)  # type: ignore[arg-type]


class DeploymentPreflightTest(unittest.TestCase):
    def test_verified_effective_fixture_is_only_eligible_for_operator_review(self) -> None:
        report = assess_deployment(good_inspection())
        self.assertIs(type(report), DeploymentPreflightReport)
        self.assertTrue(report.ready_for_operator_review)
        self.assertEqual(report.failed_reason_codes, ())
        self.assertFalse(hasattr(report, "ready_for_trading"))
        self.assertFalse(report.peer_authentication_confirmed_by_preflight)

    def test_unverified_base_or_dropin_altered_unit_is_never_eligible(self) -> None:
        inspection = good_inspection(authority_unit=authority_unit(effective_config_verified=False, effective_config_sha256=None))
        report = assess_deployment(inspection)
        self.assertFalse(report.ready_for_operator_review)
        self.assertIn(ReasonCode.EFFECTIVE_UNIT_CONFIG_UNVERIFIED, report.failed_reason_codes)

    def test_principal_primary_and_supplementary_group_collisions_fail_closed(self) -> None:
        cases = (
            good_inspection(operator=Principal("operator", 1000, 3000, frozenset())),
            good_inspection(operator=Principal("operator", 1000, 1000, frozenset({3001}))),
            good_inspection(operator=Principal("operator", 1000, 1000, frozenset({3002}))),
            good_inspection(authority=Principal("kis-nonce-authority", 2000, 3000, frozenset({3002}))),
        )
        for inspection in cases:
            with self.subTest(inspection=inspection):
                self.assertIn(ReasonCode.OPERATOR_PRIVILEGE_COLLISION if inspection.operator != OPERATOR else ReasonCode.SOCKET_CONTRACT_INVALID, assess_deployment(inspection).failed_reason_codes)

    def test_service_supplementary_groups_cannot_cross_isolate_each_other(self) -> None:
        cases = (
            # Authority must not join executor's primary private group.
            good_inspection(authority=Principal("kis-nonce-authority", 2000, 3000, frozenset({3001}))),
            # Executor must not join authority's primary private group.
            good_inspection(executor=Principal("kis-executor", 2001, 3001, frozenset({3000, 3002}))),
            # Neither service can share a non-socket supplementary group.
            good_inspection(authority=Principal("kis-nonce-authority", 2000, 3000, frozenset({3999})), executor=Principal("kis-executor", 2001, 3001, frozenset({3002, 3999}))),
        )
        for inspection in cases:
            with self.subTest(inspection=inspection):
                self.assertIn(ReasonCode.PRINCIPAL_NOT_SEPARATE, assess_deployment(inspection).failed_reason_codes)

    def test_exact_service_principal_names_are_a_production_contract(self) -> None:
        authority = Principal("reviewed-authority-replacement", 2000, 3000, frozenset())
        executor = Principal("reviewed-executor-replacement", 2001, 3001, frozenset({3002}))
        inspection = good_inspection(
            authority=authority,
            executor=executor,
            authority_unit=authority_unit(user=authority.name, group=authority.name),
            executor_unit=executor_unit(user=executor.name, group=executor.name),
            socket_group_members=frozenset({executor.name}),
        )
        self.assertIn(ReasonCode.PRINCIPAL_NOT_SEPARATE, assess_deployment(inspection).failed_reason_codes)

    def test_operator_cannot_reuse_a_fixed_service_principal_name(self) -> None:
        inspection = good_inspection(operator=Principal("kis-executor", 1000, 1000, frozenset()))
        self.assertIn(ReasonCode.OPERATOR_PRIVILEGE_COLLISION, assess_deployment(inspection).failed_reason_codes)

    def test_socket_group_is_dedicated_and_exactly_executor_member(self) -> None:
        cases = (
            good_inspection(socket_group_members=frozenset()),
            good_inspection(socket_group_members=frozenset({"kis-executor", "operator"})),
            good_inspection(socket_group_members=frozenset({"kis-executor", "kis-nonce-authority"})),
            good_inspection(socket_group_members=frozenset({"other"})),
        )
        for inspection in cases:
            with self.subTest(inspection=inspection):
                self.assertIn(ReasonCode.SOCKET_CONTRACT_INVALID, assess_deployment(inspection).failed_reason_codes)

    def test_unsafe_metadata_keys_and_units_fail_closed(self) -> None:
        cases = (
            good_inspection(authority_private_dir=meta("directory", 2000, 3000, 0o770)),
            good_inspection(authority_database=meta("regular", 2000, 3000, 0o600, symlink=True)),
            good_inspection(authority_unit=authority_unit(launcher_template_only=True)),
            good_inspection(executor_unit=executor_unit(hardening=frozenset())),
            good_inspection(executor_public_key_declaration=KeyDeclaration(True, "b" * 64)),
            good_inspection(active_units=frozenset({"kis-executor.service"})),
        )
        for inspection in cases:
            with self.subTest(inspection=inspection):
                self.assertFalse(assess_deployment(inspection).ready_for_operator_review)

    def test_hostile_nested_primitive_subclasses_are_rejected(self) -> None:
        class HostileInt(int):
            pass
        inspection = good_inspection(authority=Principal("kis-nonce-authority", HostileInt(2000), 3000, frozenset()))
        self.assertIn(ReasonCode.INSPECTION_MUTATED, assess_deployment(inspection).failed_reason_codes)

    def test_hostile_nested_values_never_escape_the_assessment_boundary(self) -> None:
        class ExplosiveInt(int):
            def __hash__(self) -> int:
                raise RuntimeError("hash must not run")

            def __eq__(self, other: object) -> bool:
                raise RuntimeError("comparison must not run")

        class ExplosiveFrozenSet(frozenset[object]):
            def __iter__(self):  # type: ignore[override]
                raise RuntimeError("iteration must not run")

        inspections = []
        hostile_uid = good_inspection()
        object.__setattr__(hostile_uid, "authority", Principal("kis-nonce-authority", ExplosiveInt(2000), 3000, frozenset()))
        inspections.append(hostile_uid)
        hostile_groups = good_inspection()
        object.__setattr__(hostile_groups, "socket_group_members", ExplosiveFrozenSet({"kis-executor"}))
        inspections.append(hostile_groups)
        hostile_field = good_inspection()
        object.__setattr__(hostile_field, "active_units", object())
        inspections.append(hostile_field)
        for inspection in inspections:
            with self.subTest(inspection=inspection):
                report = assess_deployment(inspection)
                self.assertFalse(report.ready_for_operator_review)
                self.assertIn(ReasonCode.INSPECTION_MUTATED, report.failed_reason_codes)

    def test_nested_service_metadata_and_group_mutation_are_detected(self) -> None:
        for field_name, changed in (
            ("authority_unit", replace(authority_unit(), launcher="changed")),
            ("authority_private_key", meta("regular", 2000, 3000, 0o644)),
            ("socket_group_members", frozenset({"operator"})),
        ):
            inspection = good_inspection()
            object.__setattr__(inspection, field_name, changed)
            with self.subTest(field_name=field_name):
                self.assertIn(ReasonCode.INSPECTION_MUTATED, assess_deployment(inspection).failed_reason_codes)

    def test_report_is_immutable_and_contains_no_sensitive_material(self) -> None:
        report = assess_deployment(good_inspection())
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            report.ready_for_operator_review = False  # type: ignore[misc]
        self.assertNotIn("private", repr(report).lower())
        self.assertNotIn(DIGEST, repr(report))

    def test_core_has_no_subprocess_network_or_os_mutation_surface(self) -> None:
        import swing_v2.live.deployment_preflight as module
        tree = ast.parse(inspect.getsource(module))
        imports = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        methods = {node.name.lower() for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertFalse({"subprocess", "requests", "httpx", "urllib", "dotenv", "os"} & imports)
        self.assertFalse({"start", "enable", "install", "sudo", "order", "submit", "post", "get"} & methods)


class LocalAdapterSafetyTest(unittest.TestCase):
    def test_public_key_reader_rejects_traversal_oversize_and_race_without_returning_bytes(self) -> None:
        from swing_v2.live import local_deployment_preflight as local
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32
            (root / "authority-ed25519.pub").write_text("ssh-ed25519 " + base64.b64encode(blob).decode("ascii") + " comment\n", encoding="ascii")
            (root / "authority-ed25519.pub").chmod(0o644)
            self.assertIsNotNone(local._safe_public_key(root, "authority-ed25519.pub"))
            self.assertIsNone(local._safe_public_key(root, "../authority-ed25519.pub"))
            (root / "authority-ed25519.pub").write_bytes(b"x" * 4097)
            self.assertIsNone(local._safe_public_key(root, "authority-ed25519.pub"))
        with patch.object(local, "_read_anchored", return_value=None):
            self.assertIsNone(local._safe_public_key(Path("/irrelevant"), "authority-ed25519.pub"))
        # A seam returning unsafe replacement bytes cannot leak them into the declaration.
        with patch.object(local, "_read_anchored", return_value=b"authority-private-material"):
            self.assertIsNone(local._safe_public_key(Path("/irrelevant"), "authority-ed25519.pub"))

    def test_local_inspection_is_diagnosis_only_and_cannot_be_eligible(self) -> None:
        from swing_v2.live import local_deployment_preflight as local
        inspection = local.local_inspection()
        self.assertFalse(inspection.authority_unit.effective_config_verified if inspection.authority_unit else False)
        self.assertFalse(assess_deployment(inspection).ready_for_operator_review)


if __name__ == "__main__":
    unittest.main()
