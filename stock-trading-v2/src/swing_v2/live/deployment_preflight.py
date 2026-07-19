"""Pure, non-mutating eligibility assessment for deployment review (Phase 1).

The caller supplies already-resolved, externally collected evidence.  This module does
not read a host, service manager, key, broker, or network.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import hmac
import secrets
from typing import Final

_SEAL_KEY: Final = secrets.token_bytes(32)
_REQUIRED_HARDENING: Final = frozenset({"NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=yes", "UMask=0077"})
_MAX_ID: Final = 2**31 - 1
_HEX: Final = frozenset("0123456789abcdef")
AUTHORITY_PRINCIPAL_NAME: Final = "kis-nonce-authority"
EXECUTOR_PRINCIPAL_NAME: Final = "kis-executor"


class ReasonCode(str, Enum):
    PRINCIPAL_ENTRY_MISSING = "principal_entry_missing"
    PRINCIPAL_NOT_SEPARATE = "principal_not_separate"
    OPERATOR_PRIVILEGE_COLLISION = "operator_privilege_collision"
    REQUIRED_UNIT_MISSING = "required_unit_missing"
    UNIT_IDENTITY_INVALID = "unit_identity_invalid"
    TEMPLATE_ONLY_LAUNCHER = "template_only_launcher"
    MISSING_SYSTEMD_HARDENING = "missing_systemd_hardening"
    EFFECTIVE_UNIT_CONFIG_UNVERIFIED = "effective_unit_config_unverified"
    UNSAFE_FILESYSTEM_METADATA = "unsafe_filesystem_metadata"
    AUTHORITY_PRIVATE_KEY_EXPOSED = "authority_private_key_exposed"
    AUTHORITY_DATABASE_EXPOSED = "authority_database_exposed"
    PUBLIC_KEY_DECLARATION_INVALID = "public_key_declaration_invalid"
    ACCOUNT_KEY_SHARING = "account_key_sharing"
    SOCKET_CONTRACT_INVALID = "socket_contract_invalid"
    ACTIVE_BEFORE_VERIFIED_CONFIGURATION = "active_before_verified_configuration"
    ACTIVE_STATUS_UNVERIFIED = "active_status_unverified"
    INSPECTION_MUTATED = "inspection_mutated"


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    uid: int
    gid: int
    supplementary_gids: frozenset[int]


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """Metadata only. Paths and contents never enter a report."""
    kind: str
    uid: int
    gid: int
    mode: int
    is_symlink: bool
    nlink: int
    present: bool


@dataclass(frozen=True, slots=True)
class KeyDeclaration:
    """Public-key presence and a digest only; never key material."""
    present: bool
    sha256_digest: str | None


@dataclass(frozen=True, slots=True)
class ServiceUnit:
    """Values must come from systemd-resolved effective configuration evidence."""
    name: str
    declared_path: str
    user: str
    group: str
    launcher: str
    launcher_template_only: bool
    hardening: frozenset[str]
    effective_config_verified: bool
    effective_config_sha256: str | None


@dataclass(frozen=True, slots=True)
class DeploymentInspection:
    operator: Principal | None
    authority: Principal | None
    executor: Principal | None
    authority_unit: ServiceUnit | None
    executor_unit: ServiceUnit | None
    authority_private_dir: FileMetadata
    authority_private_key: FileMetadata
    authority_database: FileMetadata
    executor_public_dir: FileMetadata
    executor_public_key: FileMetadata
    authority_public_key: KeyDeclaration
    executor_public_key_declaration: KeyDeclaration
    socket_dir: FileMetadata
    socket: FileMetadata
    socket_group: Principal | None
    socket_group_members: frozenset[str]
    required_units_present: bool
    active_units: frozenset[str]
    active_status_known: bool
    peer_authentication_confirmed: bool
    _seal: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_seal", _seal_inspection(self))


@dataclass(frozen=True, slots=True)
class DeploymentPreflightReport:
    """A narrow non-sensitive result; it cannot represent trading readiness."""
    ready_for_operator_review: bool
    failed_reason_codes: tuple[ReasonCode, ...]
    peer_authentication_confirmed_by_preflight: bool = False


@dataclass(frozen=True, slots=True)
class _InspectionSnapshot:
    """Only exact primitive values cross the hostile-inspection boundary."""
    operator: tuple[str, int, int, tuple[int, ...]] | None
    authority: tuple[str, int, int, tuple[int, ...]] | None
    executor: tuple[str, int, int, tuple[int, ...]] | None
    socket_group: tuple[str, int, int, tuple[int, ...]] | None
    metadata: tuple[tuple[str, int, int, int, bool, int, bool], ...]
    keys: tuple[tuple[bool, str | None], ...]
    units: tuple[tuple[str, str, str, str, str, bool, tuple[str, ...], bool, str | None] | None, ...]
    socket_group_members: tuple[str, ...]
    required_units_present: bool
    active_units: tuple[str, ...]
    active_status_known: bool
    peer_authentication_confirmed: bool
    seal: bytes


def assess_deployment(inspection: DeploymentInspection) -> DeploymentPreflightReport:
    """Fail closed on incomplete, altered, or unsafe supplied evidence."""
    before = _guarded_snapshot(inspection)
    if before is None:
        return _not_ready()
    reasons: set[ReasonCode] = set()
    if not hmac.compare_digest(_seal_for_snapshot(before), before.seal):
        reasons.add(ReasonCode.INSPECTION_MUTATED)

    authority, executor, operator, socket_group = before.authority, before.executor, before.operator, before.socket_group
    if authority is None or executor is None or operator is None:
        reasons.add(ReasonCode.PRINCIPAL_ENTRY_MISSING)
    else:
        authority_name, authority_uid, authority_gid, authority_supplementary = authority
        executor_name, executor_uid, executor_gid, executor_supplementary = executor
        operator_name, operator_uid, operator_gid, operator_supplementary = operator
        if (authority_name != AUTHORITY_PRINCIPAL_NAME or executor_name != EXECUTOR_PRINCIPAL_NAME or authority_uid == executor_uid or authority_gid == executor_gid or authority_name == executor_name or set((authority_gid, *authority_supplementary)) & set((executor_gid, *executor_supplementary))):
            reasons.add(ReasonCode.PRINCIPAL_NOT_SEPARATE)
        socket_gid = None if socket_group is None else socket_group[2]
        forbidden_groups = set((authority_gid, *authority_supplementary, executor_gid, *executor_supplementary))
        if socket_gid is not None:
            forbidden_groups.add(socket_gid)
        if operator_name in (AUTHORITY_PRINCIPAL_NAME, EXECUTOR_PRINCIPAL_NAME) or operator_uid in (authority_uid, executor_uid) or operator_gid in forbidden_groups or bool(set(operator_supplementary) & forbidden_groups):
            reasons.add(ReasonCode.OPERATOR_PRIVILEGE_COLLISION)

    authority_unit, executor_unit = before.units
    if not before.required_units_present or authority_unit is None or executor_unit is None:
        reasons.add(ReasonCode.REQUIRED_UNIT_MISSING)
    else:
        _check_unit_snapshot(authority_unit, "kis-nonce-authority.service", AUTHORITY_PRINCIPAL_NAME, reasons)
        _check_unit_snapshot(executor_unit, "kis-executor.service", EXECUTOR_PRINCIPAL_NAME, reasons)

    authority_private_dir, authority_private_key, authority_database, executor_public_dir, executor_public_key, socket_dir, socket = before.metadata
    for item in (authority_private_dir, executor_public_dir, socket_dir):
        if not _safe_directory_snapshot(item):
            reasons.add(ReasonCode.UNSAFE_FILESYSTEM_METADATA)
    for item in (authority_private_key, authority_database, executor_public_key):
        if not _safe_regular_file_snapshot(item):
            reasons.add(ReasonCode.UNSAFE_FILESYSTEM_METADATA)
    if authority is not None:
        if not _owned_exactly_snapshot(authority_private_dir, authority, 0o700) or not _owned_exactly_snapshot(authority_private_key, authority, 0o600):
            reasons.add(ReasonCode.AUTHORITY_PRIVATE_KEY_EXPOSED)
        if not _owned_exactly_snapshot(authority_database, authority, 0o600):
            reasons.add(ReasonCode.AUTHORITY_DATABASE_EXPOSED)
    if executor is not None and (not _owned_exactly_snapshot(executor_public_dir, executor, 0o700) or not _owned_exactly_snapshot(executor_public_key, executor, 0o644)):
        reasons.add(ReasonCode.PUBLIC_KEY_DECLARATION_INVALID)

    authority_key, executor_key = before.keys
    if not authority_key[0] or not _valid_digest(authority_key[1]) or not executor_key[0] or not _valid_digest(executor_key[1]):
        reasons.add(ReasonCode.PUBLIC_KEY_DECLARATION_INVALID)
    elif authority_key[1] != executor_key[1]:
        reasons.add(ReasonCode.ACCOUNT_KEY_SHARING)
    if not _safe_socket_contract_snapshot(before):
        reasons.add(ReasonCode.SOCKET_CONTRACT_INVALID)
    if not before.active_status_known:
        reasons.add(ReasonCode.ACTIVE_STATUS_UNVERIFIED)
    if before.active_units:
        reasons.add(ReasonCode.ACTIVE_BEFORE_VERIFIED_CONFIGURATION)

    after = _guarded_snapshot(inspection)
    if after is None or after != before or not hmac.compare_digest(_seal_for_snapshot(after), after.seal):
        reasons.add(ReasonCode.INSPECTION_MUTATED)
    return DeploymentPreflightReport(not reasons, tuple(sorted(reasons, key=lambda code: code.value)))


def _not_ready() -> DeploymentPreflightReport:
    return DeploymentPreflightReport(False, (ReasonCode.INSPECTION_MUTATED,))


def _check_unit_snapshot(unit: tuple[str, str, str, str, str, bool, tuple[str, ...], bool, str | None], expected_name: str, expected_principal_name: str, reasons: set[ReasonCode]) -> None:
    name, declared_path, user, group, launcher, template_only, hardening, effective_verified, effective_digest = unit
    if name != expected_name or not declared_path.startswith("/"):
        reasons.add(ReasonCode.UNIT_IDENTITY_INVALID)
        return
    if user != expected_principal_name or group != expected_principal_name:
        reasons.add(ReasonCode.UNIT_IDENTITY_INVALID)
    if not launcher or template_only:
        reasons.add(ReasonCode.TEMPLATE_ONLY_LAUNCHER)
    if not _REQUIRED_HARDENING.issubset(hardening):
        reasons.add(ReasonCode.MISSING_SYSTEMD_HARDENING)
    if not effective_verified or not _valid_digest(effective_digest):
        reasons.add(ReasonCode.EFFECTIVE_UNIT_CONFIG_UNVERIFIED)


def _safe_directory_snapshot(value: tuple[str, int, int, int, bool, int, bool]) -> bool:
    kind, _uid, _gid, mode, is_symlink, nlink, present = value
    return present and kind == "directory" and not is_symlink and nlink == 1 and mode & 0o022 == 0


def _safe_regular_file_snapshot(value: tuple[str, int, int, int, bool, int, bool]) -> bool:
    kind, _uid, _gid, mode, is_symlink, nlink, present = value
    return present and kind == "regular" and not is_symlink and nlink == 1 and mode & 0o022 == 0


def _owned_exactly_snapshot(value: tuple[str, int, int, int, bool, int, bool], owner: tuple[str, int, int, tuple[int, ...]], mode: int) -> bool:
    _kind, uid, gid, actual_mode, _is_symlink, _nlink, present = value
    return present and uid == owner[1] and gid == owner[2] and actual_mode == mode


def _safe_socket_contract_snapshot(snapshot: _InspectionSnapshot) -> bool:
    authority, executor, group = snapshot.authority, snapshot.executor, snapshot.socket_group
    if authority is None or executor is None or group is None:
        return False
    authority_name, authority_uid, authority_gid, authority_supplementary = authority
    executor_name, _executor_uid, executor_gid, executor_supplementary = executor
    _group_name, _group_uid, socket_gid, _group_supplementary = group
    socket_dir = snapshot.metadata[5]
    socket = snapshot.metadata[6]
    return (_safe_directory_snapshot(socket_dir) and socket[6] and socket[0] == "socket" and not socket[4] and socket[5] == 1 and socket_dir[1] == authority_uid and socket[1] == authority_uid and socket_dir[2] == socket_gid and socket[2] == socket_gid and socket_dir[3] == 0o750 and socket[3] == 0o660 and socket_gid != authority_gid and socket_gid != executor_gid and socket_gid not in authority_supplementary and socket_gid in executor_supplementary and snapshot.socket_group_members == (executor_name,))


def _check_unit(unit: ServiceUnit, expected_name: str, principal: Principal | None, reasons: set[ReasonCode]) -> None:
    if not _valid_unit(unit) or unit.name != expected_name or not unit.declared_path.startswith("/"):
        reasons.add(ReasonCode.UNIT_IDENTITY_INVALID)
        return
    if type(principal) is not Principal or unit.user != principal.name or unit.group != principal.name:
        reasons.add(ReasonCode.UNIT_IDENTITY_INVALID)
    if not unit.launcher or unit.launcher_template_only:
        reasons.add(ReasonCode.TEMPLATE_ONLY_LAUNCHER)
    if not _REQUIRED_HARDENING.issubset(unit.hardening):
        reasons.add(ReasonCode.MISSING_SYSTEMD_HARDENING)
    if not unit.effective_config_verified or not _valid_digest(unit.effective_config_sha256):
        reasons.add(ReasonCode.EFFECTIVE_UNIT_CONFIG_UNVERIFIED)


def _safe_directory(value: object) -> bool:
    return _valid_metadata(value) and value.present and value.kind == "directory" and not value.is_symlink and value.nlink == 1 and value.mode & 0o022 == 0


def _safe_regular_file(value: object) -> bool:
    return _valid_metadata(value) and value.present and value.kind == "regular" and not value.is_symlink and value.nlink == 1 and value.mode & 0o022 == 0


def _owned_exactly(value: object, owner: Principal, mode: int) -> bool:
    return _valid_metadata(value) and value.present and value.uid == owner.uid and value.gid == owner.gid and value.mode == mode


def _valid_key_declaration(value: object) -> bool:
    return type(value) is KeyDeclaration and type(value.present) is bool and value.present and _valid_digest(value.sha256_digest)


def _safe_socket_contract(inspection: DeploymentInspection, authority: Principal | None, executor: Principal | None) -> bool:
    group = inspection.socket_group
    if type(authority) is not Principal or type(executor) is not Principal or type(group) is not Principal or type(inspection.socket_group_members) is not frozenset:
        return False
    # The socket group is dedicated: executor is its one and only explicit member.
    return (_safe_directory(inspection.socket_dir) and _valid_metadata(inspection.socket) and inspection.socket.present and inspection.socket.kind == "socket" and not inspection.socket.is_symlink and inspection.socket.nlink == 1 and inspection.socket_dir.uid == authority.uid and inspection.socket.uid == authority.uid and inspection.socket_dir.gid == group.gid and inspection.socket.gid == group.gid and inspection.socket_dir.mode == 0o750 and inspection.socket.mode == 0o660 and group.gid not in {authority.gid, executor.gid} and group.gid in executor.supplementary_gids and group.gid not in authority.supplementary_gids and inspection.socket_group_members == frozenset({executor.name}))


def _valid_name(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 128 and all(character.isascii() and (character.isalnum() or character in "-_.@") for character in value)


def _valid_id(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_ID


def _valid_digest(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(character in _HEX for character in value)


def _valid_principal(value: object) -> bool:
    return type(value) is Principal and _valid_name(value.name) and _valid_id(value.uid) and _valid_id(value.gid) and type(value.supplementary_gids) is frozenset and all(_valid_id(item) for item in value.supplementary_gids)


def _valid_metadata(value: object) -> bool:
    return type(value) is FileMetadata and type(value.kind) is str and value.kind in {"missing", "directory", "regular", "socket", "other"} and _valid_id(value.uid) and _valid_id(value.gid) and type(value.mode) is int and 0 <= value.mode <= 0o7777 and type(value.is_symlink) is bool and type(value.nlink) is int and 0 <= value.nlink <= _MAX_ID and type(value.present) is bool


def _valid_unit(value: object) -> bool:
    return type(value) is ServiceUnit and all(_valid_name(item) for item in (value.name, value.user, value.group)) and type(value.declared_path) is str and value.declared_path.startswith("/") and "\x00" not in value.declared_path and type(value.launcher) is str and type(value.launcher_template_only) is bool and type(value.hardening) is frozenset and all(type(item) is str and "\x00" not in item for item in value.hardening) and type(value.effective_config_verified) is bool and (value.effective_config_sha256 is None or _valid_digest(value.effective_config_sha256))


def _guarded_snapshot(value: object) -> _InspectionSnapshot | None:
    """Reject malformed nested evidence before it can invoke hostile methods."""
    try:
        snapshot = _canonical_snapshot(value)
        if snapshot is None or type(value._seal) is not bytes:
            return None
        return replace(snapshot, seal=value._seal)
    except BaseException:
        return None


def _canonical_snapshot(value: object) -> _InspectionSnapshot | None:
    if type(value) is not DeploymentInspection:
        return None
    operator = _principal_snapshot(value.operator)
    authority = _principal_snapshot(value.authority)
    executor = _principal_snapshot(value.executor)
    socket_group = _principal_snapshot(value.socket_group)
    if any(item is False for item in (operator, authority, executor, socket_group)):
        return None
    metadata_values = (value.authority_private_dir, value.authority_private_key, value.authority_database, value.executor_public_dir, value.executor_public_key, value.socket_dir, value.socket)
    metadata = tuple(_metadata_snapshot(item) for item in metadata_values)
    if any(item is None for item in metadata):
        return None
    keys = tuple(_key_snapshot(item) for item in (value.authority_public_key, value.executor_public_key_declaration))
    if any(item is None for item in keys):
        return None
    units = tuple(_unit_snapshot(item) for item in (value.authority_unit, value.executor_unit))
    if any(item is False for item in units):
        return None
    socket_members = _names_snapshot(value.socket_group_members)
    active_units = _names_snapshot(value.active_units)
    if socket_members is None or active_units is None or type(value.required_units_present) is not bool or type(value.active_status_known) is not bool or type(value.peer_authentication_confirmed) is not bool:
        return None
    return _InspectionSnapshot(operator, authority, executor, socket_group, metadata, keys, units, socket_members, value.required_units_present, active_units, value.active_status_known, value.peer_authentication_confirmed, b"")


def _principal_snapshot(value: object) -> tuple[str, int, int, tuple[int, ...]] | None | bool:
    if value is None:
        return None
    if type(value) is not Principal:
        return False
    if not _valid_name(value.name) or not _valid_id(value.uid) or not _valid_id(value.gid) or type(value.supplementary_gids) is not frozenset:
        return False
    gids = tuple(value.supplementary_gids)
    if not all(_valid_id(item) for item in gids):
        return False
    return (value.name, value.uid, value.gid, tuple(sorted(gids)))


def _metadata_snapshot(value: object) -> tuple[str, int, int, int, bool, int, bool] | None:
    if not _valid_metadata(value):
        return None
    return (value.kind, value.uid, value.gid, value.mode, value.is_symlink, value.nlink, value.present)


def _key_snapshot(value: object) -> tuple[bool, str | None] | None:
    if not _valid_key_declaration_shape(value):
        return None
    return (value.present, value.sha256_digest)


def _unit_snapshot(value: object) -> tuple[str, str, str, str, str, bool, tuple[str, ...], bool, str | None] | None | bool:
    if value is None:
        return None
    if not _valid_unit(value):
        return False
    return (value.name, value.declared_path, value.user, value.group, value.launcher, value.launcher_template_only, tuple(sorted(value.hardening)), value.effective_config_verified, value.effective_config_sha256)


def _names_snapshot(value: object) -> tuple[str, ...] | None:
    if type(value) is not frozenset:
        return None
    names = tuple(value)
    if not all(_valid_name(item) for item in names):
        return None
    return tuple(sorted(names))


def _valid_key_declaration_shape(value: object) -> bool:
    return type(value) is KeyDeclaration and type(value.present) is bool and (value.sha256_digest is None or _valid_digest(value.sha256_digest))


def _seal_for_snapshot(snapshot: _InspectionSnapshot) -> bytes:
    data = (snapshot.operator, snapshot.authority, snapshot.executor, snapshot.socket_group, snapshot.metadata, snapshot.keys, snapshot.units, snapshot.socket_group_members, snapshot.required_units_present, snapshot.active_units, snapshot.active_status_known, snapshot.peer_authentication_confirmed)
    return hmac.new(_SEAL_KEY, repr(data).encode("utf-8"), hashlib.sha256).digest()


def _seal_inspection(value: DeploymentInspection) -> bytes:
    snapshot = _canonical_snapshot(value)
    return _seal_for_snapshot(snapshot) if snapshot is not None else b""


def main() -> int:
    """Run diagnosis only; it cannot establish eligible effective-unit evidence."""
    from swing_v2.live.local_deployment_preflight import local_inspection
    if __name__ == "__main__":
        from swing_v2.live.deployment_preflight import assess_deployment as canonical_assessment
        report = canonical_assessment(local_inspection())
    else:
        report = assess_deployment(local_inspection())
    print("ELIGIBLE_FOR_OPERATOR_REVIEW" if report.ready_for_operator_review else "NOT_READY")
    print(",".join(code.value for code in report.failed_reason_codes) or "no_failures")
    return 0 if report.ready_for_operator_review else 1


if __name__ == "__main__":
    raise SystemExit(main())
