"""Tests for the AI-decision -> live-order bridge."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from swing_v2.live.decision_order import (
    admitted_buy_decisions,
    latest_record_path,
    load_record,
    sized_quantity,
    snapshot_close,
)


def _record(**over):
    rec = {
        "signal_date": "2026-07-20",
        "admitted_symbols": ["105560", "086790"],
        "decisions": [
            {"action": "BUY", "symbol": "105560", "target_weight": "0.18", "conviction": "0.7"},
            {"action": "BUY", "symbol": "086790", "target_weight": "0.18", "conviction": "0.72"},
            {"action": "HOLD", "symbol": "000660", "target_weight": "0", "conviction": "0.5"},
            {"action": "BUY", "symbol": "999999", "target_weight": "0.1"},  # not admitted
        ],
    }
    rec.update(over)
    return rec


class RecordTests(unittest.TestCase):
    def test_admitted_buys_only(self):
        buys = admitted_buy_decisions(_record())
        self.assertEqual([d["symbol"] for d in buys], ["105560", "086790"])  # HOLD + non-admitted excluded

    def test_all_hold_yields_no_buys(self):
        rec = _record(decisions=[{"action": "HOLD", "symbol": "105560", "target_weight": "0.18"}])
        self.assertEqual(admitted_buy_decisions(rec), ())

    def test_latest_record_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "signal-2026-07-20.json").write_text(json.dumps(_record()), encoding="utf-8")
            (Path(d) / "signal-2026-07-21.json").write_text(
                json.dumps(_record(signal_date="2026-07-21")), encoding="utf-8")
            latest = latest_record_path(d)
            self.assertTrue(str(latest).endswith("signal-2026-07-21.json"))
            self.assertEqual(load_record(latest)["signal_date"], "2026-07-21")

    def test_load_rejects_non_record(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "signal-x.json"
            p.write_text(json.dumps({"foo": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_record(p)


class SnapshotCloseTests(unittest.TestCase):
    def test_reads_latest_close(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snap.json"
            p.write_text(json.dumps({"histories": {"086790": [
                {"trade_date": "2026-07-20", "close": "130000"},
                {"trade_date": "2026-07-21", "close": "133400"}]}}), encoding="utf-8")
            self.assertEqual(snapshot_close(p, "086790"), Decimal("133400"))

    def test_missing_symbol_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snap.json"
            p.write_text(json.dumps({"histories": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                snapshot_close(p, "086790")


class SizedQuantityTests(unittest.TestCase):
    def test_target_weight_sizing(self):
        # 0.18 * 10,000,000 / 50,000 = 36 shares; cap 10,000,000 allows plenty -> not clamped
        qty, clamped = sized_quantity(target_weight=Decimal("0.18"), equity=Decimal("10000000"),
                                      limit_price=Decimal("50000"), max_order_notional=Decimal("10000000"))
        self.assertEqual(qty, 36)
        self.assertFalse(clamped)

    def test_clamped_to_pilot_cap(self):
        # weight wants 36 shares (1.8M) but pilot cap 100,000 / 50,000 = 2 shares
        qty, clamped = sized_quantity(target_weight=Decimal("0.18"), equity=Decimal("10000000"),
                                      limit_price=Decimal("50000"), max_order_notional=Decimal("100000"))
        self.assertEqual(qty, 2)
        self.assertTrue(clamped)

    def test_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            sized_quantity(target_weight=Decimal("0"), equity=Decimal("1"),
                           limit_price=Decimal("1"), max_order_notional=Decimal("1"))


if __name__ == "__main__":
    unittest.main()
