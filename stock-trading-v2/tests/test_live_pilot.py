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
from pathlib import Path

from swing_v2.kis import KisCredentials
from swing_v2.live.audit import IntentAuditWriter
from swing_v2.live.intent import Side
from swing_v2.live.pilot import (
    PILOT_MAX_ORDER_NOTIONAL,
    build_pilot_exit,
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

    def test_exit_is_not_blocked_by_entry_caps(self):
        # A full exit whose notional dwarfs the pilot BUY cap still validates (exits reduce risk).
        plan = build_pilot_exit(symbol="006840", quantity=4, limit_price=Decimal("500000"),
                                signal_date=date(2026, 7, 21), equity=Decimal("815535"), open_positions=1)
        self.assertIs(plan.intent.side, Side.SELL)
        self.assertEqual(plan.intent.quantity, 4)
        self.assertEqual(plan.notional, Decimal("2000000"))  # far above the 100k BUY cap, yet allowed


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

    def _unengaged_switch(self):
        return str(Path(tempfile.mkdtemp()) / "live-kill-switch.json")  # absent -> not halted

    def test_submits_exactly_one_order_when_armed(self):
        session = _FakeSession(self._ACK)
        ack = submit_pilot_order(_tiny_buy(), client=self._client(session),
                                 operator_confirmation=LIVE_OPERATOR_CONFIRMATION,
                                 kill_switch_path=self._unengaged_switch())
        self.assertEqual(ack.order_number, "0000000123")
        self.assertEqual(len(session.posts), 1)
        self.assertIn("order-cash", session.posts[0][0])

    def test_wrong_operator_confirmation_submits_nothing(self):
        session = _FakeSession(self._ACK)
        with self.assertRaises(ValueError):
            submit_pilot_order(_tiny_buy(), client=self._client(session),
                               operator_confirmation="not-the-phrase",
                               kill_switch_path=self._unengaged_switch())
        self.assertEqual(session.posts, [])

    def test_engaged_kill_switch_blocks_submission(self):
        from datetime import datetime, timezone
        from swing_v2.live.kill_switch import LiveTradingHalted, engage_kill_switch
        path = self._unengaged_switch()
        engage_kill_switch(path, reason="manual test halt", engaged_at=datetime.now(timezone.utc))
        session = _FakeSession(self._ACK)
        with self.assertRaises(LiveTradingHalted):
            submit_pilot_order(_tiny_buy(), client=self._client(session),
                               operator_confirmation=LIVE_OPERATOR_CONFIRMATION, kill_switch_path=path)
        self.assertEqual(session.posts, [])  # fail-closed: no order despite correct confirmation


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

    def _decision_dirs(self, decisions, admitted, close="50000"):
        import json
        rec_dir = tempfile.mkdtemp()
        snap_dir = tempfile.mkdtemp()
        Path(rec_dir, "signal-2026-07-20.json").write_text(json.dumps({
            "signal_date": "2026-07-20", "admitted_symbols": admitted, "decisions": decisions}), encoding="utf-8")
        Path(snap_dir, "forward-2026-07-20.json").write_text(
            json.dumps({"histories": {d["symbol"]: [{"trade_date": "2026-07-20", "close": close}]
                                      for d in decisions}}),
            encoding="utf-8")
        return rec_dir, snap_dir

    def test_from_decision_previews_ai_pick(self):
        rec, snap = self._decision_dirs(
            [{"action": "BUY", "symbol": "086790", "target_weight": "0.18", "conviction": "0.72"}], ["086790"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pilot_cli.main(["from-decision", "--records-dir", rec,
                                 "--snapshot", str(Path(snap, "forward-2026-07-20.json")),
                                 "--equity", "10000000", "--account-no", "12345678-01"])
        self.assertEqual(rc, 0)
        self.assertIn("AI pick", out.getvalue())
        self.assertIn("086790", out.getvalue())
        self.assertIn("DRY-RUN", out.getvalue())  # no --arm -> preview only

    def test_from_decision_sell_full_exit_preview(self):
        import json
        from swing_v2.live import pilot_cli as pc
        from swing_v2.live.account_state import AccountState, HeldPosition
        rec = tempfile.mkdtemp()
        snap = tempfile.mkdtemp()
        Path(rec, "signal-2026-07-20.json").write_text(json.dumps({
            "signal_date": "2026-07-20", "admitted_symbols": ["006840"],
            "decisions": [{"action": "SELL", "symbol": "006840", "target_weight": "0", "conviction": "0.6"}]}),
            encoding="utf-8")
        Path(snap, "forward-2026-07-20.json").write_text(json.dumps({
            "histories": {"006840": [{"trade_date": "2026-07-20", "close": "20000"}]}}), encoding="utf-8")
        # a held position so the exit is sized to it (stub the live account read)
        self.addCleanup(setattr, pc, "_resolve_state", pc._resolve_state)
        pc._resolve_state = lambda args, account_no: AccountState(
            equity=Decimal("815535"), cash=Decimal("791335"), open_positions=1,
            holdings=(HeldPosition("006840", 4, Decimal("22000")),))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pc.main(["from-decision", "--records-dir", rec,
                          "--snapshot", str(Path(snap, "forward-2026-07-20.json")),
                          "--account-no", "12345678-01"])
        self.assertEqual(rc, 0)
        self.assertIn("SELL 006840", out.getvalue())
        self.assertIn("full exit qty=4", out.getvalue())
        self.assertIn("TTTC0011U", out.getvalue())  # sell TR id in the wire preview

    def test_from_decision_all_hold_orders_nothing(self):
        rec, snap = self._decision_dirs(
            [{"action": "HOLD", "symbol": "086790", "target_weight": "0.18"}], ["086790"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pilot_cli.main(["from-decision", "--records-dir", rec, "--equity", "10000000",
                                 "--account-no", "12345678-01"])
        self.assertEqual(rc, 0)
        self.assertIn("no admitted BUY", out.getvalue())

    def test_from_decision_requires_symbol_when_multiple(self):
        rec, snap = self._decision_dirs([
            {"action": "BUY", "symbol": "105560", "target_weight": "0.18"},
            {"action": "BUY", "symbol": "086790", "target_weight": "0.18"}], ["105560", "086790"])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = pilot_cli.main(["from-decision", "--records-dir", rec, "--equity", "10000000",
                                 "--account-no", "12345678-01"])
        self.assertEqual(rc, 2)
        self.assertIn("--symbol", err.getvalue())

    def test_settle_auto_records_realized_loss_from_filled_sell(self):
        from datetime import datetime, timezone
        from decimal import Decimal
        from swing_v2.live import pilot_cli as pc
        from swing_v2.live.daily_loss import realized_loss_for
        from swing_v2.live.pilot_reconcile import STATUS_FILLED, OrderReconciliation
        from swing_v2.live.realized_settlement import record_pending_sell

        pending = str(Path(tempfile.mkdtemp()) / "pending.jsonl")
        ledger = tempfile.mkdtemp()
        record_pending_sell(pending, order_number="0000000123", symbol="005930",
                            quantity=10, avg_cost=Decimal("71000"), at=datetime.now(timezone.utc))
        # stub the broker reader + credentials/token so no network is touched
        self.addCleanup(setattr, pc, "_credentials", pc._credentials)
        self.addCleanup(setattr, pc, "_access_token", pc._access_token)
        self.addCleanup(setattr, pc, "reconcile_via_client", pc.reconcile_via_client)
        import swing_v2.live.production_reconciliation as pr
        self.addCleanup(setattr, pr, "KisProductionReconciliationClient", pr.KisProductionReconciliationClient)
        pc._credentials = lambda: object()
        pc._access_token = lambda creds: "tok"
        pr.KisProductionReconciliationClient = lambda **kw: object()
        pc.reconcile_via_client = lambda client, **kw: OrderReconciliation(
            "0000000123", "005930", 10, 0, STATUS_FILLED, Decimal("69000"))  # sold below cost

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pc.main(["settle", "--pending-sells", pending, "--daily-loss-ledger", ledger,
                          "--date", "20260721", "--account-no", "12345678-01"])
        self.assertEqual(rc, 0)
        self.assertIn("realized -20000", out.getvalue())
        self.assertEqual(realized_loss_for(ledger, "2026-07-21"), Decimal("20000"))  # auto-recorded

    def test_authorize_autonomous_writes_file(self):
        auth = str(Path(tempfile.mkdtemp()) / "auth.json")
        from swing_v2.live.autonomous import AUTONOMOUS_CONFIRMATION, load_authorization
        with contextlib.redirect_stdout(io.StringIO()):
            rc = pilot_cli.main(["authorize-autonomous", "--expires", "2027-01-01", "--max-orders", "2",
                                 "--max-notional", "300000", "--confirm", AUTONOMOUS_CONFIRMATION,
                                 "--auth-file", auth])
        self.assertEqual(rc, 0)
        self.assertTrue(load_authorization(auth).enabled)

    def test_autonomous_without_authorization_refuses(self):
        rec, snap = self._decision_dirs(
            [{"action": "BUY", "symbol": "086790", "target_weight": "0.18", "conviction": "0.72"}], ["086790"])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = pilot_cli.main(["from-decision", "--records-dir", rec,
                                 "--snapshot", str(Path(snap, "forward-2026-07-20.json")),
                                 "--equity", "10000000", "--account-no", "12345678-01",
                                 "--max-notional", "60000", "--autonomous",
                                 "--auth-file", str(Path(tempfile.mkdtemp()) / "none.json")])
        self.assertEqual(rc, 2)
        self.assertIn("autonomous", err.getvalue().lower())

    def test_recorded_daily_loss_trips_the_circuit_breaker(self):
        ledger = tempfile.mkdtemp()
        day = "2026-07-21"
        # equity 10,000,000 -> 3% daily-loss cap = 300,000. Record a 400,000 loss.
        with contextlib.redirect_stdout(io.StringIO()):
            pilot_cli.main(["record-loss", "--symbol", "005930", "--qty", "10",
                            "--sell-price", "60000", "--avg-cost", "100000",  # -400,000
                            "--day", day, "--daily-loss-ledger", ledger])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = pilot_cli.main(["preflight", *self._BASE, "--as-of", day, "--daily-loss-ledger", ledger])
        self.assertEqual(rc, 2)
        self.assertIn("daily loss", err.getvalue())

    def test_halt_then_submit_is_blocked_then_resume(self):
        switch = str(Path(tempfile.mkdtemp()) / "ks.json")
        # halt
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pilot_cli.main(["halt", "--reason", "test", "--kill-switch", switch]), 0)
        # submit is fail-closed blocked even with a correct arm + confirmation
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = pilot_cli.main(["submit", *self._BASE, "--arm",
                                 "--operator-confirm", LIVE_OPERATOR_CONFIRMATION, "--kill-switch", switch])
        self.assertEqual(rc, 2)
        self.assertIn("HALTED", err.getvalue())
        # resume clears it
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pilot_cli.main(["resume", "--kill-switch", switch]), 0)


if __name__ == "__main__":
    unittest.main()
