"""Mocked-only TDD contract for KIS production reconciliation reads."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import unittest

import swing_v2.live.production_reconciliation as reconciliation

from swing_v2.kis import KisCredentials
from swing_v2.live.intent import Side
from swing_v2.live.production_reconciliation import (
    AmbiguousHaltAssessment,
    BrokerOrderReference,
    KisProductionReconciliationClient,
    ReconciliationError,
    assess_ambiguous_halt,
)


class FakeResponse:
    def __init__(self, payload: object, *, status_error: Exception | None = None) -> None:
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self) -> None:
        if self.status_error:
            raise self.status_error

    def json(self) -> object:
        return self.payload


class FakeGetSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get(self, url: str, *, headers: dict[str, str], params: dict[str, str]) -> FakeResponse:
        self.calls.append((url, headers, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(session: FakeGetSession) -> KisProductionReconciliationClient:
    return KisProductionReconciliationClient(
        credentials=KisCredentials("app-key", "app-secret"), access_token="injected-token",
        account_number="12345678-01", session=session,
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )


def order(*, number: str = "100", side: str = "02", quantity: str = "3") -> dict[str, str]:
    return {"KRX_FWDG_ORD_ORGNO": "001", "ODNO": number, "PDNO": "005930", "SLL_BUY_DVSN_CD": side,
            "ORD_QTY": quantity, "ORD_UNPR": "71000", "TOT_CCLD_QTY": "0"}


def fill(*, number: str = "100", side: str = "02", quantity: str = "3", filled: str = "3") -> dict[str, str]:
    return {"ORD_GNO_BRNO": "001", "ODNO": number, "PDNO": "005930", "SLL_BUY_DVSN_CD": side,
            "ORD_QTY": quantity, "TOT_CCLD_QTY": filled, "AVG_PRVS": "71000"}


class KisProductionReconciliationClientTest(unittest.TestCase):
    def test_open_orders_uses_exact_real_krx_read_only_wire_contract(self) -> None:
        session = FakeGetSession([FakeResponse({"rt_cd": "0", "output": [order()]})])
        result = client(session).read_open_orders()
        self.assertEqual(result[0].order_number, "100")
        self.assertEqual(result[0].side, Side.BUY)
        self.assertEqual(len(session.calls), 1)
        url, headers, params = session.calls[0]
        self.assertEqual(url, "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl")
        self.assertEqual(headers["tr_id"], "TTTC0084R")
        self.assertEqual(params, {"CANO": "12345678", "ACNT_PRDT_CD": "01", "INQR_DVSN_1": "0", "INQR_DVSN_2": "0", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "EXCG_ID_DVSN_CD": "KRX"})
        self.assertFalse(hasattr(session, "post"))

    def test_daily_fills_uses_real_tr_and_exact_dates_krx(self) -> None:
        session = FakeGetSession([FakeResponse({"rt_cd": "0", "output1": [fill()], "output2": {"tot_ord_qty": "3"}})])
        result = client(session).read_daily_order_fills("20260701", "20260718")
        self.assertEqual(result[0].filled_quantity, 3)
        _, headers, params = session.calls[0]
        self.assertEqual(headers["tr_id"], "TTTC0081R")
        self.assertEqual(params["INQR_STRT_DT"], "20260701")
        self.assertEqual(params["INQR_END_DT"], "20260718")
        self.assertEqual(params["EXCG_ID_DVSN_CD"], "KRX")

    def test_balance_uses_exact_real_wire_contract_and_builds_snapshot_digest(self) -> None:
        session = FakeGetSession([FakeResponse({"rt_cd": "0", "output1": [{"PDNO": "005930", "HLDG_QTY": "7", "PCHS_AVG_PRIC": "70000"}], "output2": {"dnca_tot_amt": "100"}})])
        snapshot = client(session).read_balance()
        self.assertEqual(snapshot.holdings[0].quantity, 7)
        self.assertEqual(snapshot.observed_at, datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(len(snapshot.account_binding_hash), 64)
        self.assertNotIn("12345678", snapshot.account_binding_hash)
        self.assertEqual(len(snapshot.digest), 64)
        _, headers, params = session.calls[0]
        self.assertEqual(headers["tr_id"], "TTTC8434R")
        self.assertEqual(params["AFHR_FLPR_YN"], "N")
        self.assertEqual(params["INQR_DVSN"], "02")
        self.assertNotIn("EXCG_ID_DVSN_CD", params)  # official balance wire shape has no exchange parameter

    def test_errors_malformed_data_and_duplicate_broker_identity_fail_closed_without_retry(self) -> None:
        bad_payloads = [
            {"rt_cd": "1", "output": []}, {"rt_cd": "0", "output": {}},
            {"rt_cd": "0", "output": [order(quantity="-1")]},
            {"rt_cd": "0", "output": [order(), order()]},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                session = FakeGetSession([FakeResponse(payload)])
                with self.assertRaises(ReconciliationError):
                    client(session).read_open_orders()
                self.assertEqual(len(session.calls), 1)
        session = FakeGetSession([ConnectionError("offline")])
        with self.assertRaises(ReconciliationError):
            client(session).read_open_orders()
        self.assertEqual(len(session.calls), 1)

    def test_dates_must_be_plain_yyyymmdd_ordered_and_within_31_days_before_transport(self) -> None:
        session = FakeGetSession([])
        for start, end in [("2026-07-01", "20260718"), ("20260719", "20260718"), ("20260601", "20260718")]:
            with self.subTest(start=start):
                with self.assertRaises(ValueError):
                    client(session).read_daily_order_fills(start, end)
        self.assertEqual(session.calls, [])

    def test_continuation_is_bounded_and_missing_cursor_fails_closed(self) -> None:
        page = {"rt_cd": "0", "output": [order()], "ctx_area_fk100": "next", "ctx_area_nk100": "next"}
        session = FakeGetSession([FakeResponse(page) for _ in range(5)])
        with self.assertRaises(ReconciliationError):
            client(session).read_open_orders()
        self.assertEqual(len(session.calls), 5)
        session = FakeGetSession([FakeResponse({"rt_cd": "0", "output": [], "ctx_area_fk100": "next"})])
        with self.assertRaises(ReconciliationError):
            client(session).read_open_orders()
        self.assertEqual(len(session.calls), 1)

    def test_assessment_never_clears_from_acknowledgement_or_partial_or_unknown_data(self) -> None:
        session = FakeGetSession([
            FakeResponse({"rt_cd": "0", "output1": [], "output2": {}}),
            FakeResponse({"rt_cd": "0", "output": [order()]}),
            FakeResponse({"rt_cd": "0", "output1": [fill(filled="1")], "output2": {}}),
        ])
        c = client(session)
        snapshot = c.read_snapshot("20260718", "20260718")
        reference = BrokerOrderReference("001", "100", "005930", Side.BUY, 3)
        self.assertEqual(assess_ambiguous_halt(snapshot, reference), AmbiguousHaltAssessment.UNRESOLVED)
        contradiction = BrokerOrderReference("001", "100", "005930", Side.SELL, 3)
        self.assertEqual(assess_ambiguous_halt(snapshot, contradiction), AmbiguousHaltAssessment.CONTRADICTION)

    def test_exact_matching_full_fill_is_non_authoritative_and_never_clears_halt(self) -> None:
        session = FakeGetSession([
            FakeResponse({"rt_cd": "0", "output1": [], "output2": {}}),
            FakeResponse({"rt_cd": "0", "output": [order()]}),
            FakeResponse({"rt_cd": "0", "output1": [fill()], "output2": {}}),
        ])
        snapshot = client(session).read_snapshot("20260718", "20260718")
        reference = BrokerOrderReference("001", "100", "005930", Side.BUY, 3)
        self.assertEqual(assess_ambiguous_halt(snapshot, reference), AmbiguousHaltAssessment.UNRESOLVED)
        object.__setattr__(snapshot, "digest", "0" * 64)
        self.assertEqual(assess_ambiguous_halt(snapshot, reference), AmbiguousHaltAssessment.UNRESOLVED)

    def test_snapshot_records_non_authoritative_sequential_query_provenance_without_raw_account(self) -> None:
        session = FakeGetSession([
            FakeResponse({"rt_cd": "0", "output1": [], "output2": {}}),
            FakeResponse({"rt_cd": "0", "output": [order()]}),
            FakeResponse({"rt_cd": "0", "output1": [fill()], "output2": {}}),
        ])
        snapshot = client(session).read_snapshot("20260701", "20260718")

        self.assertTrue(snapshot.non_atomic_observation)
        self.assertFalse(snapshot.authorizes_halt_release)
        self.assertEqual(snapshot.account_binding_scope, "LOCAL_INJECTED_ACCOUNT_HASH_UNVERIFIED")
        self.assertEqual(snapshot.requested_fill_date_range, ("20260701", "20260718"))
        self.assertEqual(
            [(item.source, item.page_count, item.pagination_complete) for item in snapshot.source_observations],
            [("balance", 1, True), ("open_orders", 1, True), ("daily_order_fills", 1, True)],
        )
        self.assertEqual(len({item.observed_at for item in snapshot.source_observations}), 1)
        self.assertNotIn("12345678-01", repr(snapshot))

    def test_digest_valid_stale_or_foreign_or_forged_exact_full_fill_never_clears_halt(self) -> None:
        session = FakeGetSession([
            FakeResponse({"rt_cd": "0", "output1": [], "output2": {}}),
            FakeResponse({"rt_cd": "0", "output": []}),
            FakeResponse({"rt_cd": "0", "output1": [fill()], "output2": {}}),
        ])
        snapshot = client(session).read_snapshot("20260718", "20260718")
        foreign_session = FakeGetSession([
            FakeResponse({"rt_cd": "0", "output1": [], "output2": {}}),
            FakeResponse({"rt_cd": "0", "output": []}),
            FakeResponse({"rt_cd": "0", "output1": [fill()], "output2": {}}),
        ])
        foreign_snapshot = KisProductionReconciliationClient(
            credentials=KisCredentials("app-key", "app-secret"), access_token="injected-token",
            account_number="87654321-01", session=foreign_session,
            clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        ).read_snapshot("20260718", "20260718")
        reference = BrokerOrderReference("001", "100", "005930", Side.BUY, 3)
        for label, forged in (
            ("stale", replace(snapshot, observed_at=datetime(2000, 1, 1, tzinfo=timezone.utc))),
            ("altered_account_binding", replace(snapshot, account_binding_hash="f" * 64)),
            ("digest_valid_forgery", replace(snapshot, account_binding_hash="0" * 64, fills=snapshot.fills * 2)),
            ("cross_account_exact_full_fill", foreign_snapshot),
        ):
            with self.subTest(label=label):
                canonical = reconciliation._snapshot_canonical(
                    forged.observed_at, forged.account_binding_hash, forged.holdings,
                    forged.open_orders, forged.fills, forged.source_observations,
                    forged.requested_fill_date_range, forged.account_binding_scope,
                    forged.non_atomic_observation, forged.authorizes_halt_release,
                )
                forged = replace(forged, digest=hashlib.sha256(canonical.encode()).hexdigest())
                self.assertTrue(reconciliation._valid_snapshot(forged))
                self.assertNotEqual(assess_ambiguous_halt(forged, reference), AmbiguousHaltAssessment.CLEAR_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
