"""Unit tests for the read-only KIS balance -> account-state + pretrade-snapshot reader."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from swing_v2.live.account_state import (
    AccountState,
    HeldPosition,
    build_pretrade_snapshot,
    parse_account_state,
    read_account_state,
)
from swing_v2.live.intent import LiveOrderIntent, OrderMode, Side
from swing_v2.live.risk import PretradeLimits, validate_pretrade


def _payload() -> dict:
    """A representative KIS domestic inquire-balance payload with a skipped zero-qty row."""
    return {
        "output1": [
            {
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "hldg_qty": "10",
                "pchs_avg_pric": "71000",
                "evlu_amt": "720000",
            },
            {
                "pdno": "000660",
                "prdt_name": "SK하이닉스",
                "hldg_qty": "0",
                "pchs_avg_pric": "180000",
                "evlu_amt": "0",
            },
        ],
        "output2": [
            {
                "dnca_tot_amt": "5000000",
                "tot_evlu_amt": "5720000",
                "nass_amt": "5720000",
                "evlu_amt_smtl_amt": "720000",
                "pchs_amt_smtl_amt": "710000",
                "evlu_pfls_smtl_amt": "10000",
            }
        ],
    }


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, str]] = []

    def inquire_balance(self, access_token: str, account_number: str) -> dict:
        self.calls.append((access_token, account_number))
        return self._payload


class ParseAccountStateTest(unittest.TestCase):
    def test_extracts_equity_cash_and_nonzero_holdings(self) -> None:
        state = parse_account_state(_payload())

        self.assertIsInstance(state, AccountState)
        self.assertEqual(state.equity, Decimal("5720000"))
        self.assertEqual(state.cash, Decimal("5000000"))
        self.assertEqual(state.open_positions, 1)
        self.assertEqual(
            state.holdings,
            (HeldPosition(symbol="005930", quantity=10, avg_price=Decimal("71000")),),
        )

    def test_empty_holdings_is_valid_zero_positions(self) -> None:
        payload = _payload()
        payload["output1"] = []
        state = parse_account_state(payload)
        self.assertEqual(state.open_positions, 0)
        self.assertEqual(state.holdings, ())
        self.assertEqual(state.equity, Decimal("5720000"))

    def test_skips_zero_quantity_rows(self) -> None:
        state = parse_account_state(_payload())
        self.assertNotIn("000660", tuple(h.symbol for h in state.holdings))

    def test_rejects_empty_output2(self) -> None:
        payload = _payload()
        payload["output2"] = []
        with self.assertRaisesRegex(ValueError, "output2"):
            parse_account_state(payload)

    def test_rejects_missing_nass_amt(self) -> None:
        payload = _payload()
        del payload["output2"][0]["nass_amt"]
        with self.assertRaisesRegex(ValueError, "nass_amt"):
            parse_account_state(payload)

    def test_rejects_non_object_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "mapping"):
            parse_account_state(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_rejects_non_positive_equity(self) -> None:
        payload = _payload()
        payload["output2"][0]["nass_amt"] = "0"
        with self.assertRaisesRegex(ValueError, "positive"):
            parse_account_state(payload)

    def test_rejects_malformed_holding_quantity(self) -> None:
        payload = _payload()
        payload["output1"][0]["hldg_qty"] = "ten"
        with self.assertRaisesRegex(ValueError, "hldg_qty"):
            parse_account_state(payload)


class ReadAccountStateTest(unittest.TestCase):
    def test_reads_via_injected_fake_client(self) -> None:
        client = _FakeClient(_payload())
        state = read_account_state(client, "token-abc", "12345678-01")

        self.assertEqual(client.calls, [("token-abc", "12345678-01")])
        self.assertEqual(state.equity, Decimal("5720000"))
        self.assertEqual(state.open_positions, 1)


class BuildPretradeSnapshotTest(unittest.TestCase):
    def test_uses_current_open_position_count(self) -> None:
        # Convention (matches order_bridge + validate_pretrade's >= check): pass the
        # CURRENT open count, not a pre-incremented one. _payload holds 1 position.
        state = parse_account_state(_payload())
        snapshot = build_pretrade_snapshot(state, proposed_position_risk=Decimal("1000"))

        self.assertEqual(snapshot.planned_or_open_positions, 1)
        self.assertEqual(snapshot.equity, Decimal("5720000"))
        self.assertEqual(snapshot.daily_loss, Decimal("0"))
        self.assertEqual(snapshot.proposed_position_risk, Decimal("1000"))

    def test_empty_account_yields_zero_planned_positions(self) -> None:
        payload = _payload()
        payload["output1"] = []
        snapshot = build_pretrade_snapshot(parse_account_state(payload), proposed_position_risk=Decimal("1000"))
        self.assertEqual(snapshot.planned_or_open_positions, 0)

    def test_snapshot_passes_validate_pretrade_within_limits(self) -> None:
        state = parse_account_state(_payload())
        # equity 5,720,000 -> per-position risk cap at 1% = 57,200.
        snapshot = build_pretrade_snapshot(state, proposed_position_risk=Decimal("50000"))
        intent = LiveOrderIntent(
            "swing-v2", "1", date(2026, 7, 18), "005930", "STOCK",
            Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT,
        )
        # Does not raise.
        validate_pretrade(intent, snapshot)

    def test_snapshot_fails_validate_pretrade_when_position_risk_exceeded(self) -> None:
        state = parse_account_state(_payload())
        snapshot = build_pretrade_snapshot(state, proposed_position_risk=Decimal("100000"))
        intent = LiveOrderIntent(
            "swing-v2", "1", date(2026, 7, 18), "005930", "STOCK",
            Side.BUY, 1, Decimal("71000"), OrderMode.LIMIT,
        )
        with self.assertRaisesRegex(ValueError, "position risk"):
            validate_pretrade(intent, snapshot, limits=PretradeLimits())

    def test_rejects_negative_proposed_position_risk(self) -> None:
        state = parse_account_state(_payload())
        with self.assertRaisesRegex(ValueError, "proposed_position_risk"):
            build_pretrade_snapshot(state, proposed_position_risk=Decimal("-1"))

    def test_rejects_non_account_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "AccountState"):
            build_pretrade_snapshot(object(), proposed_position_risk=Decimal("1"))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
