"""Tests for the forward-observation evaluation harness.

Recording signals is only half of forward observation; this scores the accumulated
signal records against what actually happened next — the survivorship-free measure of
whether the admitted BUY picks beat the market over the intended horizon. It joins each
record's admitted picks to realized bars (entry at the next session's open, exit at the
close `forward_sessions` later) and reports hit rate, mean pick return, market benchmark,
and the pick-minus-market edge.
"""

import unittest
from datetime import date
from decimal import Decimal

from swing_v2.contracts import DailyBar
from swing_v2.llm.forward_eval import ForwardObservationReport, evaluate_forward_observations


D = Decimal
CAL = [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16), date(2026, 7, 17)]


def _bar(day, symbol, open_, close_):
    o, c = D(open_), D(close_)
    return DailyBar(day, symbol, "STOCK" if symbol != "KOSPI" else "INDEX",
                    o, max(o, c) + D("1"), min(o, c) - D("1"), c, 1_000_000, o * 1_000_000, True)


# Prices: A rises after signal, B falls; market roughly flat-up.
_BARS = {
    ("A", date(2026, 7, 14)): _bar(date(2026, 7, 14), "A", "100", "101"),
    ("A", date(2026, 7, 15)): _bar(date(2026, 7, 15), "A", "101", "110"),
    ("B", date(2026, 7, 14)): _bar(date(2026, 7, 14), "B", "100", "99"),
    ("B", date(2026, 7, 15)): _bar(date(2026, 7, 15), "B", "99", "90"),
    ("KOSPI", date(2026, 7, 14)): _bar(date(2026, 7, 14), "KOSPI", "2500", "2505"),
    ("KOSPI", date(2026, 7, 15)): _bar(date(2026, 7, 15), "KOSPI", "2505", "2510"),
}


def _lookup(symbol, day):
    return _BARS.get((symbol, day))


def _record(signal_date, picks):
    return {
        "signal_date": signal_date.isoformat(),
        "decisions": [
            {"symbol": s, "action": "BUY", "conviction": conv} for s, conv in picks
        ],
        "admitted_symbols": [s for s, _ in picks],
    }


class ForwardEvalTests(unittest.TestCase):
    def test_scores_picks_forward_return_against_market(self):
        # signal on 07-13 -> entry open 07-14, exit close (2 sessions) 07-15.
        records = [_record(date(2026, 7, 13), [("A", "0.8"), ("B", "0.6")])]
        report = evaluate_forward_observations(
            records, bar_lookup=_lookup, calendar=CAL, market_symbol="KOSPI", forward_sessions=2,
        )
        self.assertIsInstance(report, ForwardObservationReport)
        self.assertEqual(report.scored_count, 2)
        # A: 110/100-1 = +0.10 ; B: 90/100-1 = -0.10
        self.assertEqual(report.hit_rate, D("0.5"))
        self.assertEqual(report.mean_pick_return, D("0"))  # (+0.10 + -0.10)/2
        # market: 2510/2500-1 = +0.004
        self.assertEqual(report.mean_market_return, D("0.004"))
        self.assertEqual(report.edge, report.mean_pick_return - report.mean_market_return)

    def test_unobservable_records_are_skipped_not_counted(self):
        # signal on the last calendar date has no forward sessions yet.
        records = [_record(date(2026, 7, 17), [("A", "0.9")])]
        report = evaluate_forward_observations(
            records, bar_lookup=_lookup, calendar=CAL, market_symbol="KOSPI", forward_sessions=2,
        )
        self.assertEqual(report.scored_count, 0)

    def test_missing_forward_bar_is_skipped(self):
        # C has no bars in the lookup.
        records = [_record(date(2026, 7, 13), [("C", "0.7")])]
        report = evaluate_forward_observations(
            records, bar_lookup=_lookup, calendar=CAL, market_symbol="KOSPI", forward_sessions=2,
        )
        self.assertEqual(report.scored_count, 0)

    def test_conviction_calibration_buckets(self):
        records = [_record(date(2026, 7, 13), [("A", "0.9"), ("B", "0.2")])]
        report = evaluate_forward_observations(
            records, bar_lookup=_lookup, calendar=CAL, market_symbol="KOSPI", forward_sessions=2,
        )
        # high-conviction A won, low-conviction B lost -> calibration should reflect it
        buckets = {b.label: b for b in report.by_conviction}
        self.assertTrue(any(b.mean_return > 0 for b in report.by_conviction))
        self.assertTrue(any(b.mean_return < 0 for b in report.by_conviction))

    def test_only_admitted_buys_are_scored(self):
        rec = {
            "signal_date": date(2026, 7, 13).isoformat(),
            "decisions": [
                {"symbol": "A", "action": "BUY", "conviction": "0.8"},
                {"symbol": "B", "action": "HOLD", "conviction": "0.5"},  # not a BUY
            ],
            "admitted_symbols": ["A"],  # B not admitted
        }
        report = evaluate_forward_observations(
            [rec], bar_lookup=_lookup, calendar=CAL, market_symbol="KOSPI", forward_sessions=2,
        )
        self.assertEqual(report.scored_count, 1)
        self.assertEqual(report.outcomes[0].symbol, "A")

    def test_empty_records_raise(self):
        with self.assertRaises(ValueError):
            evaluate_forward_observations([], bar_lookup=_lookup, calendar=CAL, market_symbol="KOSPI")


if __name__ == "__main__":
    unittest.main()
