"""Tests for the small-live-pilot orchestration glue.

The dangerous pieces (transport, gate, pretrade risk, audit) already exist and are
guarded; pilot.py only wires them under TIGHTENED caps and a human arm step. These
tests use an injected fake HTTP session — no network, no real order is ever placed.
"""

import contextlib
import io
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from swing_v2.kis import KisCredentials
from swing_v2.live.audit import IntentAuditWriter
from swing_v2.live.intent import Side
from swing_v2.live.pilot import (
    PILOT_MAX_ORDER_NOTIONAL,
    build_pilot_order,
    describe_pilot_plan,
    submit_pilot_order,
)
from swing_v2.live import pilot_cli
from swing_v2.live.production_execution import KisProductionTradingClient
from swing_v2.live.gate import LIVE_OPERATOR_CONFIRMATION

_ACCOUNT = "12345678-01"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """Records posts; returns a canned KIS acknowledgement. Never touches the network."""

    def __init__(self, payload):
        self._payload = payload
        self.posts = []

    def post(self, url, *, headers, json):
        self.posts.append((url, headers, json))
        return _FakeResponse(self._payload)


def _tiny_buy(**overrides):
    kwargs = dict(
        symbol="086790", side=Side.BUY, quantity=1, limit_price=Decimal("50000"),
        signal_date=date(2026, 7, 21), equity=Decimal("10000000"), open_positions=0,
    )
    kwargs.update(overrides)
    return build_pilot_order(**kwargs)


class BuildPilotOrderTests(unittest.TestCase):
    def test_builds_validated_tiny_order(self):
        plan = _tiny_buy()
        self.assertEqual(plan.intent.symbol, "086790")
        self.assertEqual(plan.intent.quantity, 1)
        self.assertEqual(plan.intent.notional, Decimal("50000"))
        # pilot caps are far below the standing limits
        self.assertEqual(plan.limits.max_positions, 1)
        self.assertEqual(plan.limits.max_order_notional, PILOT_MAX_ORDER_NOTIONAL)

    def test_rejects_order_above_pilot_notional_cap(self):
        # 5 shares * 50,000 = 250,000 > the 100,000 pilot cap
        with self.assertRaises(ValueError):
            _tiny_buy(quantity=5)

    def test_rejects_when_a_position_is_already_open(self):
        with self.assertRaises(ValueError):
            _tiny_buy(open_positions=1)  # pilot allows at most one position

    def test_custom_cap_can_lower_but_still_enforced(self):
        with self.assertRaises(ValueError):
            _tiny_buy(limit_price=Decimal("60000"), max_order_notional=Decimal("50000"))


class DescribePilotPlanTests(unittest.TestCase):
    def test_describes_exact_wire_and_tr_id_without_submitting(self):
        text = describe_pilot_plan(_tiny_buy(), account_number=_ACCOUNT)
        self.assertIn("TTTC0012U", text)          # production cash-buy TR id
        self.assertIn("086790", text)             # PDNO
        self.assertIn("DRY-RUN", text)            # makes clear nothing was sent
        self.assertNotIn("submitted", text.lower())

    def test_sell_shows_sell_tr_id(self):
        plan = build_pilot_order(symbol="086790", side=Side.SELL, quantity=1,
                                 limit_price=Decimal("50000"), signal_date=date(2026, 7, 21),
                                 equity=Decimal("10000000"), open_positions=0)
        self.assertIn("TTTC0011U", describe_pilot_plan(plan, account_number=_ACCOUNT))


class SubmitPilotOrderTests(unittest.TestCase):
    _ACK = {"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000000123"}}

    def _client(self, session):
        return KisProductionTradingClient(
            credentials=KisCredentials(app_key="k", app_secret="s"),
            access_token="tok", account_number=_ACCOUNT,
            session=session, audit_writer=IntentAuditWriter(tempfile.mkdtemp()),
        )

    def test_submits_exactly_one_order_when_armed(self):
        session = _FakeSession(self._ACK)
        ack = submit_pilot_order(_tiny_buy(), client=self._client(session),
                                 operator_confirmation=LIVE_OPERATOR_CONFIRMATION)
        self.assertEqual(ack.order_number, "0000000123")
        self.assertEqual(len(session.posts), 1)
        self.assertIn("order-cash", session.posts[0][0])

    def test_wrong_operator_confirmation_submits_nothing(self):
        session = _FakeSession(self._ACK)
        with self.assertRaises(ValueError):
            submit_pilot_order(_tiny_buy(), client=self._client(session),
                               operator_confirmation="not-the-phrase")
        self.assertEqual(session.posts, [])


class CliTests(unittest.TestCase):
    _BASE = ["--symbol", "086790", "--side", "BUY", "--qty", "1",
             "--limit-price", "50000", "--equity", "10000000", "--account-no", "12345678-01"]

    def test_parser_has_the_three_subcommands(self):
        parser = pilot_cli.build_parser()
        self.assertEqual(parser.parse_args(["preflight", *self._BASE]).command, "preflight")
        self.assertEqual(parser.parse_args(["submit", *self._BASE, "--arm"]).command, "submit")
        recon = parser.parse_args(["reconcile", "--symbol", "086790", "--order-number", "0000000123"])
        self.assertEqual(recon.command, "reconcile")
        self.assertEqual(recon.order_number, "0000000123")

    def test_preflight_prints_dry_run_and_places_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pilot_cli.main(["preflight", *self._BASE])
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertIn("TTTC0012U", out.getvalue())

    def test_submit_without_arm_refuses(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = pilot_cli.main(["submit", *self._BASE])
        self.assertEqual(rc, 2)
        self.assertIn("--arm", err.getvalue())

    def test_submit_with_wrong_confirmation_refuses(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = pilot_cli.main(["submit", *self._BASE, "--arm", "--operator-confirm", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("operator-confirm", err.getvalue())


if __name__ == "__main__":
    unittest.main()
