"""Safety-boundary contracts for the intentionally non-submitting live layer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from swing_v2.live.audit import IntentAuditWriter, load_audit_record
from swing_v2.live.gate import LIVE_OPERATOR_CONFIRMATION, LiveExecutionConfig, require_live_execution_enabled
from swing_v2.live.intent import LiveOrderIntent, OrderMode, Side
from swing_v2.live.risk import AccountRiskSnapshot, PretradeLimits, validate_pretrade


class IntentAuditTest(unittest.TestCase):
    def test_write_once_record_has_canonical_digest_and_refuses_duplicate(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 3, Decimal("71000"), OrderMode.LIMIT)
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = IntentAuditWriter(Path(temporary_directory))
            record_path = writer.write(intent)
            record = load_audit_record(record_path)

            self.assertEqual(record["intent_id"], intent.intent_id)
            self.assertRegex(record["digest"], r"^sha256:[0-9a-f]{64}$")
            original = record_path.read_bytes()
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                writer.write(intent)
            self.assertEqual(record_path.read_bytes(), original)

    def test_rejects_tampered_intent_id_before_creating_audit_record(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        object.__setattr__(intent, "intent_id", "0" * 64)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            writer = IntentAuditWriter(root)

            with self.assertRaisesRegex(ValueError, "identity"):
                writer.write(intent)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_rejects_malicious_intent_id_str_subclass_before_creating_audit_record(self) -> None:
        class MaliciousIntentId(str):
            def __ne__(self, other: object) -> bool:
                return False

            def __format__(self, format_spec: str) -> str:
                return "attacker-output"

        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        object.__setattr__(intent, "intent_id", MaliciousIntentId(intent.intent_id))

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            writer = IntentAuditWriter(root)

            with self.assertRaisesRegex(ValueError, "identity"):
                writer.write(intent)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_rejects_injected_id_computer_and_field_mutation_before_creating_audit_record(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        object.__setattr__(intent, "quantity", 2)
        object.__setattr__(intent, "_compute_intent_id", lambda: intent.intent_id)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            writer = IntentAuditWriter(root)

            with self.assertRaisesRegex(ValueError, "identity"):
                writer.write(intent)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_rejects_intent_mutated_between_snapshot_and_create_without_record_output(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            writer = IntentAuditWriter(root)

            def mutate_intent() -> None:
                object.__setattr__(intent, "quantity", 2)

            writer._before_create = mutate_intent  # type: ignore[attr-defined]
            with self.assertRaisesRegex(ValueError, "integrity|identity"):
                writer.write(intent)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_refuses_symlink_roots_and_malformed_records(self) -> None:

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            link = root / "linked-audit"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                IntentAuditWriter(link)
            malformed = root / ("intent-" + "0" * 64 + ".json")
            malformed.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "malformed|metadata"):
                load_audit_record(malformed)
            unsafe = root / ".." / ("intent-" + "1" * 64 + ".json")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                load_audit_record(unsafe)

    def test_rejects_group_writable_audit_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "audit"
            root.mkdir(mode=0o700)
            root.chmod(0o720)

            with self.assertRaisesRegex(ValueError, "group/world writable"):
                IntentAuditWriter(root)

    def test_loader_rejects_record_with_insecure_metadata(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        with tempfile.TemporaryDirectory() as temporary_directory:
            record_path = IntentAuditWriter(Path(temporary_directory)).write(intent)
            record_path.chmod(0o640)

            with self.assertRaisesRegex(ValueError, "metadata"):
                load_audit_record(record_path)


    def test_refuses_root_replaced_by_symlink_after_writer_creation(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "audit"
            outside = base / "outside"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            outside.mkdir(mode=0o700)
            outside.chmod(0o700)
            writer = IntentAuditWriter(root)
            root.rmdir()
            root.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "identity|non-symlink"):
                writer.write(intent)
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_fd_anchored_writer_cannot_write_outside_during_root_swap(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root, parked, outside = base / "audit", base / "parked", base / "outside"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            outside.mkdir(mode=0o700)
            outside.chmod(0o700)
            writer = IntentAuditWriter(root)

            def swap_root() -> None:
                root.rename(parked)
                root.symlink_to(outside, target_is_directory=True)

            writer._before_create = swap_root  # type: ignore[attr-defined]
            with self.assertRaisesRegex(ValueError, "identity|non-symlink"):
                writer.write(intent)
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_rejects_root_replaced_with_different_secure_directory(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root, parked = base / "audit", base / "parked"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            writer = IntentAuditWriter(root)
            root.rename(parked)
            root.mkdir(mode=0o700)
            root.chmod(0o700)

            with self.assertRaisesRegex(ValueError, "identity"):
                writer.write(intent)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_owner_can_delete_and_recreate_a_locally_digested_record(self) -> None:
        """SHA-256 is corruption detection, not a same-UID append-only guarantee."""
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT)
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = IntentAuditWriter(Path(temporary_directory))
            record_path = writer.write(intent)
            record_path.unlink()
            recreated = writer.write(intent)

            self.assertEqual(recreated, record_path)
            self.assertEqual(load_audit_record(recreated)["intent_id"], intent.intent_id)


class PretradeRiskGateTest(unittest.TestCase):
    def test_rejects_position_count_at_independent_limit(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 3, Decimal("71000"), OrderMode.LIMIT)
        snapshot = AccountRiskSnapshot(
            planned_or_open_positions=5, equity=Decimal("10000000"),
            daily_loss=Decimal("0"), proposed_position_risk=Decimal("100000"),
        )

        with self.assertRaisesRegex(ValueError, "positions"):
            validate_pretrade(intent, snapshot)


    def test_rejects_each_independent_risk_limit(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "DOMESTIC_INDEX_OR_SECTOR", Side.BUY, 20, Decimal("50000"), OrderMode.LIMIT)
        limits = PretradeLimits(max_order_notional=Decimal("999999"))
        cases = (
            (AccountRiskSnapshot(0, Decimal("10000000"), Decimal("0"), Decimal("100001")), "position risk"),
            (AccountRiskSnapshot(0, Decimal("10000000"), Decimal("300001"), Decimal("0")), "daily loss"),
            (AccountRiskSnapshot(0, Decimal("10000000"), Decimal("0"), Decimal("0")), "notional"),
        )
        for snapshot, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                validate_pretrade(intent, snapshot, limits=limits)

    def test_rejects_nonfinite_injected_account_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            AccountRiskSnapshot(0, Decimal("Infinity"), Decimal("0"), Decimal("0"))

    def test_rejects_snapshot_subclass_before_forged_equity_can_bypass_risk_limit(self) -> None:
        class ForgedEquitySnapshot(AccountRiskSnapshot):
            def __getattribute__(self, name: str) -> object:
                if name == "equity":
                    return Decimal("1000000")
                return super().__getattribute__(name)

        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("1"), OrderMode.LIMIT)
        forged = ForgedEquitySnapshot(0, Decimal("1"), Decimal("0"), Decimal("100"))

        with self.assertRaisesRegex(ValueError, "exact AccountRiskSnapshot"):
            validate_pretrade(intent, forged)

    def test_rejects_limits_subclass_before_forged_notional_cap_can_bypass_limit(self) -> None:
        class ForgedNotionalLimits(PretradeLimits):
            def __getattribute__(self, name: str) -> object:
                if name == "max_order_notional":
                    return Decimal("3000000")
                return super().__getattribute__(name)

        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 20, Decimal("100000"), OrderMode.LIMIT)
        snapshot = AccountRiskSnapshot(0, Decimal("10000000"), Decimal("0"), Decimal("0"))
        forged = ForgedNotionalLimits()

        with self.assertRaisesRegex(ValueError, "exact PretradeLimits"):
            validate_pretrade(intent, snapshot, limits=forged)

    def test_rejects_subclass_that_forges_low_notional_for_limit_exceeding_order(self) -> None:
        class ForgedNotionalIntent(LiveOrderIntent):
            @property
            def notional(self) -> Decimal:
                return Decimal("1")

        forged = ForgedNotionalIntent(
            "swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY,
            20, Decimal("100000"), OrderMode.LIMIT,
        )
        snapshot = AccountRiskSnapshot(0, Decimal("10000000"), Decimal("0"), Decimal("0"))

        with self.assertRaisesRegex(ValueError, "exact LiveOrderIntent"):
            validate_pretrade(forged, snapshot)

    def test_rejects_snapshot_mutated_after_construction(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("1"), OrderMode.LIMIT)
        snapshot = AccountRiskSnapshot(5, Decimal("10000000"), Decimal("0"), Decimal("0"))
        object.__setattr__(snapshot, "planned_or_open_positions", 0)

        with self.assertRaisesRegex(ValueError, "integrity"):
            validate_pretrade(intent, snapshot)

    def test_rejects_limits_mutated_after_construction(self) -> None:
        intent = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 20, Decimal("100000"), OrderMode.LIMIT)
        snapshot = AccountRiskSnapshot(0, Decimal("10000000"), Decimal("0"), Decimal("0"))
        limits = PretradeLimits()
        object.__setattr__(limits, "max_order_notional", Decimal("3000000"))

        with self.assertRaisesRegex(ValueError, "integrity"):
            validate_pretrade(intent, snapshot, limits=limits)


class LiveOrderIntentTest(unittest.TestCase):
    def test_stock_intent_has_deterministic_sha256_identity(self) -> None:
        intent = LiveOrderIntent(
            strategy="swing-v2", strategy_version="1", signal_date=date(2026, 7, 18),
            symbol="005930", classification="STOCK", side=Side.BUY, quantity=3,
            limit_price=Decimal("71000"), order_mode=OrderMode.LIMIT,
        )

        self.assertEqual(len(intent.intent_id), 64)
        self.assertEqual(intent.intent_id, LiveOrderIntent(
            strategy="swing-v2", strategy_version="1", signal_date=date(2026, 7, 18),
            symbol="005930", classification="STOCK", side=Side.BUY, quantity=3,
            limit_price=Decimal("71000"), order_mode=OrderMode.LIMIT,
        ).intent_id)


    def test_rejects_disallowed_classification_and_nonfinite_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "classification"):
            LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "BAD", "ETF", Side.BUY, 1, Decimal("1"), OrderMode.LIMIT)
        with self.assertRaisesRegex(ValueError, "finite"):
            LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("NaN"), OrderMode.LIMIT)

    def test_id_changes_when_immutable_decision_input_changes(self) -> None:
        base = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 3, Decimal("71000"), OrderMode.LIMIT)
        changed = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 4, Decimal("71000"), OrderMode.LIMIT)
        self.assertNotEqual(base.intent_id, changed.intent_id)

    def test_id_binds_classification_to_prevent_audit_identity_collision(self) -> None:
        stock = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 3, Decimal("71000"), OrderMode.LIMIT)
        sector = LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "DOMESTIC_INDEX_OR_SECTOR", Side.BUY, 3, Decimal("71000"), OrderMode.LIMIT)

        self.assertNotEqual(stock.intent_id, sector.intent_id)


class LiveExecutionGateTest(unittest.TestCase):
    def test_default_gate_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "disabled"):
            require_live_execution_enabled(LiveExecutionConfig())
    def test_malformed_truthy_enable_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "plain bool"):
            LiveExecutionConfig(live_trading_enabled="true")  # type: ignore[arg-type]

    def test_rejects_duck_typed_live_execution_configuration(self) -> None:
        duck = SimpleNamespace(
            live_trading_enabled=True,
            operator_confirmation=LIVE_OPERATOR_CONFIRMATION,
        )

        with self.assertRaisesRegex(ValueError, "LiveExecutionConfig"):
            require_live_execution_enabled(duck)  # type: ignore[arg-type]

    def test_explicit_confirmation_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirmation"):
            require_live_execution_enabled(LiveExecutionConfig(live_trading_enabled=True))

    def test_rejects_config_mutated_after_construction(self) -> None:
        config = LiveExecutionConfig()
        object.__setattr__(config, "live_trading_enabled", True)
        object.__setattr__(config, "operator_confirmation", LIVE_OPERATOR_CONFIRMATION)

        with self.assertRaisesRegex(ValueError, "integrity"):
            require_live_execution_enabled(config)


if __name__ == "__main__":
    unittest.main()
