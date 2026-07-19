"""Tests for the INERT bridge from an admitted BUY decision to a risk-validated intent.

This bridge stops at a validated LiveOrderIntent. Building an intent is NOT placing an
order: there is no submitter, no gate flip, no network here. These tests also assert
the module never reaches into the execution/gate machinery.
"""

import ast
import pathlib
import unittest
from datetime import date
from decimal import Decimal

from swing_v2.live.intent import LiveOrderIntent, OrderMode, Side
from swing_v2.live.risk import PretradeLimits
from swing_v2.llm.decision import DecisionAction, SymbolDecision
from swing_v2.llm.order_bridge import build_inert_intent


def _buy(symbol="005930", weight="0.1", conviction="0.8"):
    return SymbolDecision(symbol, DecisionAction.BUY, Decimal(conviction), Decimal(weight), "why", ())


def _limits(notional="100000000"):
    return PretradeLimits(max_order_notional=Decimal(notional))


class OrderBridgeTests(unittest.TestCase):
    def test_buy_decision_becomes_validated_limit_intent(self):
        intent = build_inert_intent(
            _buy(weight="0.1"),
            limit_price=Decimal("70000"),
            equity=Decimal("10000000"),
            classification="STOCK",
            strategy="llm_v0",
            strategy_version="0",
            signal_date=date(2026, 7, 16),
            planned_or_open_positions=0,
            daily_loss=Decimal("0"),
            limits=_limits(),
        )
        self.assertIsInstance(intent, LiveOrderIntent)
        self.assertIs(intent.side, Side.BUY)
        self.assertIs(intent.order_mode, OrderMode.LIMIT)
        self.assertEqual(intent.symbol, "005930")
        # 10% of 10,000,000 / 70,000 = floor(14.28) = 14
        self.assertEqual(intent.quantity, 14)

    def test_notional_over_cap_is_rejected_by_pretrade(self):
        with self.assertRaises(ValueError):
            build_inert_intent(
                _buy(weight="0.9"),
                limit_price=Decimal("70000"),
                equity=Decimal("10000000"),
                classification="STOCK",
                strategy="llm_v0", strategy_version="0", signal_date=date(2026, 7, 16),
                planned_or_open_positions=0, daily_loss=Decimal("0"),
                limits=_limits(notional="1000000"),  # tiny cap -> 9,000,000 notional rejected
            )

    def test_position_count_at_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            build_inert_intent(
                _buy(),
                limit_price=Decimal("70000"), equity=Decimal("10000000"),
                classification="STOCK", strategy="llm_v0", strategy_version="0",
                signal_date=date(2026, 7, 16),
                planned_or_open_positions=5, daily_loss=Decimal("0"),
                limits=_limits(),
            )

    def test_sub_one_share_quantity_is_rejected(self):
        with self.assertRaises(ValueError):
            build_inert_intent(
                _buy(weight="0.0001"),
                limit_price=Decimal("70000"), equity=Decimal("10000000"),
                classification="STOCK", strategy="llm_v0", strategy_version="0",
                signal_date=date(2026, 7, 16),
                planned_or_open_positions=0, daily_loss=Decimal("0"),
                limits=_limits(),
            )

    def test_sell_and_hold_are_not_supported_by_this_buy_bridge(self):
        for action in (DecisionAction.SELL, DecisionAction.HOLD):
            decision = SymbolDecision("005930", action, Decimal("0.8"), Decimal("0"), "x", ())
            with self.assertRaises(ValueError):
                build_inert_intent(
                    decision, limit_price=Decimal("70000"), equity=Decimal("10000000"),
                    classification="STOCK", strategy="llm_v0", strategy_version="0",
                    signal_date=date(2026, 7, 16),
                    planned_or_open_positions=0, daily_loss=Decimal("0"), limits=_limits(),
                )

    def test_disallowed_classification_is_rejected(self):
        with self.assertRaises(ValueError):
            build_inert_intent(
                _buy(), limit_price=Decimal("70000"), equity=Decimal("10000000"),
                classification="LEVERAGED_ETF", strategy="llm_v0", strategy_version="0",
                signal_date=date(2026, 7, 16),
                planned_or_open_positions=0, daily_loss=Decimal("0"), limits=_limits(),
            )

    def test_module_imports_and_calls_are_free_of_execution_gate_or_network(self):
        # Check real imports/calls via AST, not prose: the safety docstring deliberately
        # names the absent machinery, so a raw substring scan would false-positive.
        source = pathlib.Path("src/swing_v2/llm/order_bridge.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        joined_imports = " ".join(imported)
        for forbidden in ("production_execution", "gate", "requests", "urllib", "socket", "http"):
            self.assertNotIn(forbidden, joined_imports)
        # No submission/network call sites (these patterns never appear in prose).
        for call in (".post(", ".submit(", "urlopen(", ".get("):
            self.assertNotIn(call, source)


if __name__ == "__main__":
    unittest.main()
