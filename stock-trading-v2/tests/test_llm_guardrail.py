"""Tests for the hard guardrails applied to Hermes' proposed decisions.

Principle: Hermes only *proposes*; the code *enforces*. A proposal that violates the
deny-by-default universe, the new-entry block, the max-position count, or the single
position weight cap is rejected (with a reason) rather than acted on. No sizing to
shares, no orders — this is the signal-only admission stage.
"""

import unittest
from decimal import Decimal

from swing_v2.llm.decision import DecisionAction, SymbolDecision
from swing_v2.llm.guardrail import (
    GuardrailConfig,
    PortfolioContext,
    apply_guardrails,
)


ELIGIBLE = frozenset({"005930", "000660", "035420", "005380", "051910", "068270"})


def _decision(symbol, action, conviction="0.7", weight="0.1"):
    return SymbolDecision(
        symbol=symbol,
        action=DecisionAction[action],
        conviction=Decimal(conviction),
        target_weight=Decimal(weight),
        rationale="test",
        cited_evidence=(),
    )


def _config(**kw):
    return GuardrailConfig(eligible_symbols=ELIGIBLE, **kw)


class GuardrailTests(unittest.TestCase):
    def test_valid_buy_into_empty_portfolio_is_admitted(self):
        plan = apply_guardrails(
            (_decision("005930", "BUY"),),
            portfolio=PortfolioContext(held_symbols=frozenset(), new_entries_blocked=False),
            config=_config(),
        )
        self.assertEqual([d.symbol for d in plan.admitted], ["005930"])
        self.assertEqual(plan.rejected, ())

    def test_buy_outside_eligible_universe_is_rejected(self):
        plan = apply_guardrails(
            (_decision("999999", "BUY"),),
            portfolio=PortfolioContext(held_symbols=frozenset(), new_entries_blocked=False),
            config=_config(),
        )
        self.assertEqual(plan.admitted, ())
        self.assertEqual(len(plan.rejected), 1)
        self.assertEqual(plan.rejected[0].symbol, "999999")

    def test_buy_blocked_when_new_entries_blocked(self):
        plan = apply_guardrails(
            (_decision("005930", "BUY"),),
            portfolio=PortfolioContext(held_symbols=frozenset(), new_entries_blocked=True),
            config=_config(),
        )
        self.assertEqual(plan.admitted, ())
        self.assertEqual(plan.rejected[0].action, DecisionAction.BUY)

    def test_sell_always_allowed_even_when_new_entries_blocked(self):
        plan = apply_guardrails(
            (SymbolDecision("005930", DecisionAction.SELL, Decimal("0.9"), Decimal("0"), "exit", ()),),
            portfolio=PortfolioContext(held_symbols=frozenset({"005930"}), new_entries_blocked=True),
            config=_config(),
        )
        self.assertEqual([d.symbol for d in plan.admitted], ["005930"])

    def test_sell_of_unheld_symbol_is_rejected(self):
        plan = apply_guardrails(
            (SymbolDecision("005930", DecisionAction.SELL, Decimal("0.9"), Decimal("0"), "exit", ()),),
            portfolio=PortfolioContext(held_symbols=frozenset(), new_entries_blocked=False),
            config=_config(),
        )
        self.assertEqual(plan.admitted, ())

    def test_hold_of_unheld_symbol_is_rejected(self):
        plan = apply_guardrails(
            (_decision("005930", "HOLD", weight="0.1"),),
            portfolio=PortfolioContext(held_symbols=frozenset(), new_entries_blocked=False),
            config=_config(),
        )
        self.assertEqual(plan.admitted, ())

    def test_buy_of_already_held_symbol_is_rejected(self):
        plan = apply_guardrails(
            (_decision("005930", "BUY"),),
            portfolio=PortfolioContext(held_symbols=frozenset({"005930"}), new_entries_blocked=False),
            config=_config(),
        )
        self.assertEqual(plan.admitted, ())

    def test_single_weight_above_cap_is_rejected(self):
        plan = apply_guardrails(
            (_decision("005930", "BUY", weight="0.25"),),
            portfolio=PortfolioContext(held_symbols=frozenset(), new_entries_blocked=False),
            config=_config(max_single_weight=Decimal("0.20")),
        )
        self.assertEqual(plan.admitted, ())

    def test_max_positions_drops_lowest_conviction_buys(self):
        # 3 slots free (max 5, 2 held). 4 buys proposed -> lowest conviction dropped.
        decisions = (
            _decision("035420", "BUY", conviction="0.9"),
            _decision("005380", "BUY", conviction="0.8"),
            _decision("051910", "BUY", conviction="0.3"),
            _decision("068270", "BUY", conviction="0.6"),
        )
        plan = apply_guardrails(
            decisions,
            portfolio=PortfolioContext(held_symbols=frozenset({"005930", "000660"}), new_entries_blocked=False),
            config=_config(max_positions=5),
        )
        admitted = {d.symbol for d in plan.admitted}
        self.assertEqual(admitted, {"035420", "005380", "068270"})  # 0.3 conviction dropped
        self.assertEqual([r.symbol for r in plan.rejected], ["051910"])

    def test_sells_free_slots_for_buys(self):
        # 5 held, one SELL frees a slot for one BUY.
        decisions = (
            SymbolDecision("005930", DecisionAction.SELL, Decimal("0.9"), Decimal("0"), "exit", ()),
            _decision("068270", "BUY", conviction="0.7"),
        )
        plan = apply_guardrails(
            decisions,
            portfolio=PortfolioContext(
                held_symbols=frozenset({"005930", "000660", "035420", "005380", "051910"}),
                new_entries_blocked=False,
            ),
            config=_config(max_positions=5),
        )
        admitted = {d.symbol for d in plan.admitted}
        self.assertEqual(admitted, {"005930", "068270"})

    def test_admitted_plan_has_no_order_or_share_fields(self):
        plan = apply_guardrails(
            (_decision("005930", "BUY"),),
            portfolio=PortfolioContext(held_symbols=frozenset(), new_entries_blocked=False),
            config=_config(),
        )
        forbidden = {"quantity", "shares", "order_id", "fill_id", "price", "cash"}
        self.assertEqual(forbidden & set(vars(plan).keys()), set())


if __name__ == "__main__":
    unittest.main()
