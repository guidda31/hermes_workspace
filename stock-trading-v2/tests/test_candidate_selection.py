import unittest
from decimal import Decimal

from swing_v2.backtest import Candidate, select_entry_candidates


D = Decimal


def candidate(symbol: str, *, eligible: bool = True) -> Candidate:
    return Candidate(symbol, eligible, D("0.10"), D("0.10"))


class CandidateSelectionTests(unittest.TestCase):
    def test_selects_eligible_candidates_by_breakout_then_momentum(self) -> None:
        candidates = (
            Candidate("MOMENTUM", True, D("0.10"), D("0.30")),
            Candidate("BREAKOUT", True, D("0.20"), D("0.01")),
            Candidate("INELIGIBLE", False, D("0.99"), D("0.99")),
        )

        result = select_entry_candidates(
            candidates=candidates,
            active_symbols={"HELD"},
            pending_entry_symbols=set(),
            max_positions=5,
        )

        self.assertEqual(
            tuple(item.symbol for item in result.selected),
            ("BREAKOUT", "MOMENTUM"),
        )
        self.assertEqual(result.available_slots, 4)

    def test_uses_symbol_ascending_as_the_final_tiebreaker(self) -> None:
        result = select_entry_candidates(
            candidates=(candidate("ZETA"), candidate("ALPHA")),
            active_symbols=set(),
            pending_entry_symbols=set(),
            max_positions=2,
        )

        self.assertEqual(tuple(item.symbol for item in result.selected), ("ALPHA", "ZETA"))

    def test_snapshot_symbols_are_excluded_from_selection(self) -> None:
        result = select_entry_candidates(
            candidates=(candidate("ELIGIBLE"), candidate("HELD"), candidate("PENDING")),
            active_symbols={"HELD"},
            pending_entry_symbols={"PENDING"},
            max_positions=5,
        )

        self.assertEqual(tuple(item.symbol for item in result.selected), ("ELIGIBLE",))
        self.assertEqual(result.available_slots, 3)

    def test_pending_entry_reserves_a_slot(self) -> None:
        result = select_entry_candidates(
            candidates=(candidate("AAA"), candidate("BBB")),
            active_symbols={"HELD"},
            pending_entry_symbols={"PENDING_1", "PENDING_2", "PENDING_3"},
            max_positions=5,
        )

        self.assertEqual(result.available_slots, 1)
        self.assertEqual(tuple(item.symbol for item in result.selected), ("AAA",))

    def test_active_and_pending_snapshot_capacity_limits_selection(self) -> None:
        result = select_entry_candidates(
            candidates=(candidate("AAA"), candidate("BBB")),
            active_symbols={"HELD_1", "HELD_2"},
            pending_entry_symbols={"PENDING_1", "PENDING_2"},
            max_positions=5,
        )

        self.assertEqual(result.available_slots, 1)
        self.assertEqual(tuple(item.symbol for item in result.selected), ("AAA",))

    def test_rejects_duplicate_candidate_symbols(self) -> None:
        with self.assertRaises(ValueError):
            select_entry_candidates(
                candidates=(candidate("AAA"), candidate("AAA")),
                active_symbols=set(),
                pending_entry_symbols=set(),
                max_positions=2,
            )

    def test_rejects_over_capacity_portfolio_snapshot(self) -> None:
        with self.assertRaises(ValueError):
            select_entry_candidates(
                candidates=(candidate("AAA"),),
                active_symbols={"HELD_1", "HELD_2"},
                pending_entry_symbols={"PENDING"},
                max_positions=2,
            )

    def test_rejects_overlapping_snapshot_symbols(self) -> None:
        with self.assertRaises(ValueError):
            select_entry_candidates(
                candidates=(candidate("AAA"),),
                active_symbols={"SAME"},
                pending_entry_symbols={"SAME"},
                max_positions=2,
            )

    def test_rejects_invalid_candidate_symbols_and_eligible_flag(self) -> None:
        for symbol, eligible in (("", True), (None, True), ("AAA", 1), ("AAA", "true")):
            with self.subTest(symbol=symbol, eligible=eligible):
                with self.assertRaises(ValueError):
                    Candidate(symbol, eligible, D("0.10"), D("0.10"))

    def test_rejects_invalid_snapshot_symbols(self) -> None:
        for keyword, symbols in (("active_symbols", {""}), ("pending_entry_symbols", {None})):
            with self.subTest(keyword=keyword, symbols=symbols):
                arguments = {
                    "candidates": (candidate("AAA"),),
                    "active_symbols": set(),
                    "pending_entry_symbols": set(),
                    "max_positions": 1,
                }
                arguments[keyword] = symbols
                with self.assertRaises(ValueError):
                    select_entry_candidates(**arguments)

    def test_rejects_nonpositive_or_nonfinite_candidate_strengths(self) -> None:
        for field in ("breakout_strength", "momentum_60"):
            for value in (D("0"), D("-0.01"), D("NaN"), D("Infinity"), D("-Infinity")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        Candidate(
                            "AAA",
                            True,
                            value if field == "breakout_strength" else D("0.10"),
                            value if field == "momentum_60" else D("0.10"),
                        )

    def test_rejects_negative_or_noninteger_max_positions(self) -> None:
        for value in (-1, D("NaN"), True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    select_entry_candidates(
                        candidates=(candidate("AAA"),),
                        active_symbols=set(),
                        pending_entry_symbols=set(),
                        max_positions=value,
                    )


if __name__ == "__main__":
    unittest.main()
