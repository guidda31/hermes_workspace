"""Mocked-only contract tests for the isolated KIS production submitter."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import multiprocessing
from pathlib import Path
import os
import tempfile
import threading
from typing import Any
import unittest
from unittest import mock

from swing_v2.kis import KisCredentials
from swing_v2.live.audit import IntentAuditWriter
from swing_v2.live.gate import LIVE_OPERATOR_CONFIRMATION, LiveExecutionConfig
from swing_v2.live.intent import LiveOrderIntent, OrderMode, Side
from swing_v2.live.risk import AccountRiskSnapshot, PretradeLimits
from swing_v2.live.production_execution import (
    AmbiguousBrokerState,
    BrokerOrderReceipt,
    CashLimitOrderRequest,
    KisProductionTradingClient,
    LiveAmendmentConfig,
    prepare_amendment_or_cancel,
)
import swing_v2.live.production_execution as production_execution


class FakeResponse:
    def __init__(self, payload: object, *, status_error: Exception | None = None) -> None:
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, str]) -> FakeResponse:
        self.calls.append((url, headers, json))
        return self.response


def approved_config() -> LiveExecutionConfig:
    return LiveExecutionConfig(True, LIVE_OPERATOR_CONFIRMATION)


def permitted_snapshot() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(0, Decimal("10000000"), Decimal("0"), Decimal("0"))


def intent() -> LiveOrderIntent:
    return LiveOrderIntent("swing-v2", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 3, Decimal("71000"), OrderMode.LIMIT)


class ForkSignalingSession:
    """Local-only transport that exposes the child's attempted fake POST via IPC."""

    def __init__(self, post_attempted: Any) -> None:
        self._post_attempted = post_attempted

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, str]) -> FakeResponse:
        self._post_attempted.set()
        return FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "fork", "ODNO": "child"}})


def submit_from_forked_child(writer: IntentAuditWriter, post_attempted: Any,
                             finished: Any, outcomes: Any) -> None:
    """Exercise the inherited writer's production path without any real KIS transport."""
    try:
        client = KisProductionTradingClient(
            credentials=KisCredentials("key", "secret"), access_token="child-token",
            account_number="12345678-01", session=ForkSignalingSession(post_attempted), audit_writer=writer,
        )
        acknowledgement = client.submit_cash_limit_order(
            config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits(),
        )
        outcomes.put(("ok", acknowledgement.order_number))
    except BaseException as exc:
        outcomes.put(("error", repr(exc)))
    finally:
        finished.set()


class KisProductionTradingClientTest(unittest.TestCase):
    def test_cash_request_rejects_non_krx_or_non_limit_values_before_transport(self) -> None:
        with self.assertRaisesRegex(ValueError, "classification"):
            CashLimitOrderRequest("12345678-01", "005930", "ETF", Side.BUY, 1, Decimal("1"))
        with self.assertRaisesRegex(ValueError, "quantity"):
            CashLimitOrderRequest("12345678-01", "005930", "STOCK", Side.BUY, 0, Decimal("1"))
        with self.assertRaisesRegex(ValueError, "finite"):
            CashLimitOrderRequest("12345678-01", "005930", "STOCK", Side.SELL, 1, Decimal("NaN"))

    def test_approved_audited_limit_buy_posts_exact_production_wire_request(self) -> None:
        session = FakeSession(FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "00001", "ODNO": "12345"}}))
        with tempfile.TemporaryDirectory() as directory:
            client = KisProductionTradingClient(
                credentials=KisCredentials("app-key", "app-secret"), access_token="token",
                account_number="12345678-01", session=session,
                audit_writer=IntentAuditWriter(Path(directory)),
            )

            acknowledgement = client.submit_cash_limit_order(
                config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits(),
            )

        self.assertEqual(acknowledgement.order_number, "12345")
        self.assertEqual(len(session.calls), 1)
        url, headers, body = session.calls[0]
        self.assertEqual(url, "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash")
        self.assertEqual(headers["tr_id"], "TTTC0012U")
        self.assertEqual(headers["authorization"], "Bearer token")
        self.assertEqual(body, {
            "CANO": "12345678", "ACNT_PRDT_CD": "01", "PDNO": "005930", "ORD_DVSN": "00",
            "ORD_QTY": "3", "ORD_UNPR": "71000", "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01", "CNDT_PRIC": "0",
        })

    def test_duplicate_exact_intent_is_rejected_before_a_second_post(self) -> None:
        session = FakeSession(FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "1", "ODNO": "2"}}))
        with tempfile.TemporaryDirectory() as directory:
            client = KisProductionTradingClient(credentials=KisCredentials("key", "secret"), access_token="token", account_number="12345678-01", session=session, audit_writer=IntentAuditWriter(Path(directory)))
            client.submit_cash_limit_order(config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits())
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                client.submit_cash_limit_order(config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits())
        self.assertEqual(len(session.calls), 1)

    def test_disabled_gate_and_risk_failure_never_post(self) -> None:
        session = FakeSession(FakeResponse({}))
        with tempfile.TemporaryDirectory() as directory:
            client = KisProductionTradingClient(credentials=KisCredentials("key", "secret"), access_token="token", account_number="12345678-01", session=session, audit_writer=IntentAuditWriter(Path(directory)))
            with self.assertRaisesRegex(ValueError, "disabled"):
                client.submit_cash_limit_order(config=LiveExecutionConfig(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits())
            with self.assertRaisesRegex(ValueError, "notional"):
                client.submit_cash_limit_order(config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits(max_order_notional=Decimal("1")))
        self.assertEqual(session.calls, [])

    def test_network_failure_halts_without_retry_and_keeps_audit(self) -> None:
        class FailingSession:
            def __init__(self) -> None: self.calls = 0
            def post(self, url: str, *, headers: dict[str, str], json: dict[str, str]) -> object:
                self.calls += 1
                raise ConnectionError("unavailable")
        session = FailingSession()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = KisProductionTradingClient(credentials=KisCredentials("key", "secret"), access_token="token", account_number="12345678-01", session=session, audit_writer=IntentAuditWriter(root))
            with self.assertRaisesRegex(AmbiguousBrokerState, "reconcile"):
                client.submit_cash_limit_order(config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits())
            self.assertEqual(len([item for item in root.iterdir() if item.name.startswith("intent-")]), 1)
            self.assertEqual(len([item for item in root.iterdir() if item.name.startswith("ambiguous-halt-")]), 1)
        self.assertEqual(session.calls, 1)

    def test_transport_ambiguity_durably_blocks_changed_intents_in_same_and_fresh_clients(self) -> None:
        class FailingSession:
            def __init__(self) -> None:
                self.calls = 0

            def post(self, url: str, *, headers: dict[str, str], json: dict[str, str]) -> object:
                self.calls += 1
                raise ConnectionError("unavailable")

        session = FailingSession()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = KisProductionTradingClient(
                credentials=KisCredentials("key", "secret"), access_token="token",
                account_number="12345678-01", session=session,
                audit_writer=IntentAuditWriter(root),
            )
            with self.assertRaisesRegex(AmbiguousBrokerState, "reconcile"):
                client.submit_cash_limit_order(
                    config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits(),
                )
            changed = LiveOrderIntent(
                "another-strategy", "different-version", date(2026, 7, 18), "005930",
                "STOCK", Side.SELL, 1, Decimal("71000"), OrderMode.LIMIT,
            )
            with self.assertRaisesRegex(AmbiguousBrokerState, "reconcile"):
                client.submit_cash_limit_order(
                    config=approved_config(), intent=changed, snapshot=permitted_snapshot(), limits=PretradeLimits(),
                )
            fresh_client = KisProductionTradingClient(
                credentials=KisCredentials("key", "secret"), access_token="token",
                account_number="12345678-01", session=session,
                audit_writer=IntentAuditWriter(root),
            )
            with self.assertRaisesRegex(AmbiguousBrokerState, "reconcile"):
                fresh_client.submit_cash_limit_order(
                    config=approved_config(), intent=changed, snapshot=permitted_snapshot(), limits=PretradeLimits(),
                )
            marker_contents = [item.read_text(encoding="utf-8") for item in root.iterdir() if item.name.startswith("ambiguous-halt-")]
            self.assertEqual(len(marker_contents), 1)
            self.assertNotIn("12345678-01", marker_contents[0])
            self.assertNotIn("token", marker_contents[0])
        self.assertEqual(session.calls, 1)

    def test_mutation_after_audit_keeps_post_bound_to_immutable_buy_snapshot(self) -> None:
        session = FakeSession(FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "1", "ODNO": "2"}}))
        original_intent = intent()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = IntentAuditWriter(root)
            original_write = writer.write

            def write_then_mutate(audited_intent: LiveOrderIntent) -> Path:
                record_path = original_write(audited_intent)
                object.__setattr__(original_intent, "side", Side.SELL)
                return record_path

            writer.write = write_then_mutate  # type: ignore[method-assign]
            client = KisProductionTradingClient(
                credentials=KisCredentials("key", "secret"), access_token="token",
                account_number="12345678-01", session=session, audit_writer=writer,
            )
            client.submit_cash_limit_order(
                config=approved_config(), intent=original_intent, snapshot=permitted_snapshot(), limits=PretradeLimits(),
            )
            audit_records = [item for item in root.iterdir() if item.name.startswith("intent-")]
            self.assertEqual(len(audit_records), 1)
            self.assertIn('"side":"BUY"', audit_records[0].read_text(encoding="utf-8"))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["tr_id"], "TTTC0012U")

    def test_mutation_after_legacy_pretrade_seam_cannot_raise_posted_notional(self) -> None:
        """The only risk-checked action must be a detached immutable snapshot."""
        session = FakeSession(FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "1", "ODNO": "2"}}))
        original_intent = intent()
        real_validate = production_execution.validate_pretrade

        def validate_then_mutate(*args: object, **kwargs: object) -> None:
            real_validate(*args, **kwargs)
            changed = LiveOrderIntent(
                original_intent.strategy, original_intent.strategy_version, original_intent.signal_date,
                original_intent.symbol, original_intent.classification, original_intent.side,
                101, original_intent.limit_price, original_intent.order_mode,
            )
            object.__setattr__(original_intent, "quantity", changed.quantity)
            object.__setattr__(original_intent, "intent_id", changed.intent_id)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            production_execution, "validate_pretrade", side_effect=validate_then_mutate,
        ):
            client = KisProductionTradingClient(
                credentials=KisCredentials("key", "secret"), access_token="token",
                account_number="12345678-01", session=session, audit_writer=IntentAuditWriter(Path(directory)),
            )
            client.submit_cash_limit_order(
                config=approved_config(), intent=original_intent, snapshot=permitted_snapshot(),
                limits=PretradeLimits(max_order_notional=Decimal("1000000")),
            )

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]["ORD_QTY"], "3")

    def test_halt_created_after_first_check_blocks_post_at_second_check(self) -> None:
        session = FakeSession(FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "1", "ODNO": "2"}}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = IntentAuditWriter(root)
            other_client_writer = IntentAuditWriter(root)
            writer._after_first_halt_check = lambda: other_client_writer._record_ambiguous_halt("12345678-01")
            client = KisProductionTradingClient(
                credentials=KisCredentials("key", "secret"), access_token="token",
                account_number="12345678-01", session=session, audit_writer=writer,
            )
            with self.assertRaisesRegex(AmbiguousBrokerState, "durably halted"):
                client.submit_cash_limit_order(
                    config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits(),
                )
        self.assertEqual(session.calls, [])

    def test_unsafe_account_lock_path_fails_closed_before_post(self) -> None:
        session = FakeSession(FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "1", "ODNO": "2"}}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = IntentAuditWriter(root)
            binding = writer._ambiguous_halt_binding("12345678-01")
            os.symlink(root / "not-a-lock", root / f"account-lock-{binding}.lock")
            client = KisProductionTradingClient(
                credentials=KisCredentials("key", "secret"), access_token="token",
                account_number="12345678-01", session=session, audit_writer=writer,
            )
            with self.assertRaisesRegex(ValueError, "halt lock is unsafe"):
                client.submit_cash_limit_order(
                    config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits(),
                )
        self.assertEqual(session.calls, [])

    def test_same_account_clients_serialize_the_post_critical_section(self) -> None:
        started = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        class BlockingSession(FakeSession):
            def post(self, url: str, *, headers: dict[str, str], json: dict[str, str]) -> FakeResponse:
                self.calls.append((url, headers, json))
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("test did not release first submitter")
                return self.response

        first_session = BlockingSession(FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "1", "ODNO": "2"}}))
        second_session = FakeSession(FakeResponse({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "1", "ODNO": "3"}}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = KisProductionTradingClient(credentials=KisCredentials("key", "secret"), access_token="one", account_number="12345678-01", session=first_session, audit_writer=IntentAuditWriter(root))
            second = KisProductionTradingClient(credentials=KisCredentials("key", "secret"), access_token="two", account_number="12345678-01", session=second_session, audit_writer=IntentAuditWriter(root))

            first_thread = threading.Thread(target=lambda: first.submit_cash_limit_order(config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits()))
            def submit_second() -> None:
                try:
                    second.submit_cash_limit_order(
                        config=approved_config(),
                        intent=LiveOrderIntent("other", "1", date(2026, 7, 18), "005930", "STOCK", Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT),
                        snapshot=permitted_snapshot(), limits=PretradeLimits(),
                    )
                except BaseException as exc:
                    failures.append(exc)
            second_thread = threading.Thread(target=submit_second)
            first_thread.start()
            self.assertTrue(started.wait(timeout=1))
            second_thread.start()
            self.assertFalse(second_session.calls)
            release.set()
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(second_session.calls), 1)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "POSIX fork is unavailable")
    def test_forked_child_inherited_writer_blocks_before_fake_post_until_parent_releases_account_lock(self) -> None:
        """A forked child must not mistake the parent's active flock state for nesting."""
        context = multiprocessing.get_context("fork")
        post_attempted = context.Event()
        finished = context.Event()
        outcomes = context.Queue()
        with tempfile.TemporaryDirectory() as directory:
            writer = IntentAuditWriter(Path(directory))
            with writer._account_halt_lock("12345678-01"):
                child = context.Process(
                    target=submit_from_forked_child,
                    args=(writer, post_attempted, finished, outcomes),
                )
                child.start()
                self.assertFalse(post_attempted.wait(timeout=0.3), "child bypassed the inherited account flock")
                self.assertFalse(finished.is_set(), "child reported an instant nested acquisition")
            self.assertTrue(post_attempted.wait(timeout=2), "child did not proceed after parent release")
            self.assertTrue(finished.wait(timeout=2), "child submit did not finish")
            child.join(timeout=2)

        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 0)
        self.assertEqual(outcomes.get(timeout=1), ("ok", "child"))

    def test_kis_error_or_missing_identifiers_durably_halt_without_retry(self) -> None:
        for payload in ({"rt_cd": "1"}, {"rt_cd": "0", "output": {"ODNO": "2"}}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                session = FakeSession(FakeResponse(payload))
                client = KisProductionTradingClient(credentials=KisCredentials("key", "secret"), access_token="token", account_number="12345678-01", session=session, audit_writer=IntentAuditWriter(Path(directory)))
                with self.assertRaises(AmbiguousBrokerState):
                    client.submit_cash_limit_order(config=approved_config(), intent=intent(), snapshot=permitted_snapshot(), limits=PretradeLimits())
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(len([item for item in Path(directory).iterdir() if item.name.startswith("ambiguous-halt-")]), 1)

    def test_prepare_amendment_is_serialization_only_and_requires_separate_approval(self) -> None:
        receipt = BrokerOrderReceipt("00001", "12345")
        config = LiveAmendmentConfig(True, "KIS_LIVE_AMEND_CANCEL_OPERATOR_CONFIRMED")
        prepared = prepare_amendment_or_cancel(config=config, account_number="12345678-01", original_receipt=receipt, quantity=Decimal("2"), limit_price=Decimal("72000"), cancel=False)
        self.assertEqual(prepared.endpoint, "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-rvsecncl")
        self.assertEqual(prepared.headers["tr_id"], "TTTC0013U")
        self.assertEqual(prepared.body["RVSE_CNCL_DVSN_CD"], "01")
        self.assertEqual(prepared.body["ORD_QTY"], "2")
        with self.assertRaisesRegex(ValueError, "separate"):
            prepare_amendment_or_cancel(config=LiveAmendmentConfig(), account_number="12345678-01", original_receipt=receipt, quantity=Decimal("2"), limit_price=Decimal("72000"), cancel=False)


if __name__ == "__main__":
    unittest.main()
