import unittest
from datetime import date, timedelta
from decimal import Decimal

from swing_v2.backtest import assess_close_time_candidates
from swing_v2.contracts import DailyBar


D = Decimal
SIGNAL_DATE = date(2026, 7, 17)


def make_bars(
    symbol: str,
    asset_type: str,
    closes: list[Decimal],
    *,
    final_date: date = SIGNAL_DATE,
    trading_value: Decimal = D("1000000000"),
    tradable: bool = True,
) -> tuple[DailyBar, ...]:
    start = final_date - timedelta(days=len(closes) - 1)
    return tuple(
        DailyBar(
            trade_date=start + timedelta(days=index),
            symbol=symbol,
            asset_type=asset_type,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1,
            trading_value=trading_value,
            is_tradable=tradable,
        )
        for index, close in enumerate(closes)
    )


class CloseTimeCandidateAssessmentTests(unittest.TestCase):
    def test_risk_on_is_applied_uniformly_and_every_universe_symbol_is_sorted(self) -> None:
        histories = {
            "ZETA": make_bars("ZETA", "STOCK", [D("1000")] * 60 + [D("1200")]),
            "ALPHA": make_bars("ALPHA", "STOCK", [D("1000")] * 60 + [D("1200")]),
        }

        assessments = assess_close_time_candidates(
            signal_date=SIGNAL_DATE,
            market_closes=[D("100")] * 200,
            asset_types={"ZETA": "STOCK", "ALPHA": "STOCK"},
            asset_histories=histories,
            universe_symbols={"ZETA", "ALPHA"},
        )

        self.assertEqual(tuple(item.symbol for item in assessments), ("ALPHA", "ZETA"))
        self.assertTrue(all(not item.risk_on for item in assessments))
        self.assertTrue(all(not item.candidate.eligible for item in assessments))
        self.assertTrue(all("RISK_OFF" in item.rejection_reasons for item in assessments))

    def test_fully_eligible_candidate_has_exact_close_time_scores(self) -> None:
        bars = make_bars("AAA", "STOCK", [D("1000")] * 40 + [D("1100")] * 20 + [D("1200")])

        assessment = assess_close_time_candidates(
            signal_date=SIGNAL_DATE,
            market_closes=[D("100")] * 199 + [D("101")],
            asset_types={"AAA": "STOCK"},
            asset_histories={"AAA": bars},
            universe_symbols={"AAA"},
        )[0]

        self.assertTrue(assessment.candidate.eligible)
        self.assertTrue(assessment.risk_on)
        self.assertTrue(assessment.liquidity)
        self.assertTrue(assessment.momentum)
        self.assertEqual(assessment.rejection_reasons, ())
        self.assertEqual(assessment.breakout_strength, D("1200") / D("1100") - 1)
        self.assertEqual(assessment.momentum_60, D("1200") / D("1000") - 1)
        self.assertEqual(assessment.candidate.breakout_strength, assessment.breakout_strength)
        self.assertEqual(assessment.candidate.momentum_60, assessment.momentum_60)

    def test_liquidity_failure_is_ineligible_with_an_explicit_reason(self) -> None:
        bars = make_bars(
            "AAA", "STOCK", [D("1000")] * 40 + [D("1100")] * 20 + [D("1200")],
            trading_value=D("999999999"),
        )

        assessment = assess_close_time_candidates(
            signal_date=SIGNAL_DATE, market_closes=[D("100")] * 199 + [D("101")],
            asset_types={"AAA": "STOCK"}, asset_histories={"AAA": bars}, universe_symbols={"AAA"},
        )[0]

        self.assertFalse(assessment.candidate.eligible)
        self.assertFalse(assessment.liquidity)
        self.assertTrue(assessment.momentum)
        self.assertIn("LIQUIDITY_REJECT", assessment.rejection_reasons)

    def test_momentum_failure_is_ineligible_with_an_explicit_reason(self) -> None:
        bars = make_bars("AAA", "STOCK", [D("1100")] * 61)

        assessment = assess_close_time_candidates(
            signal_date=SIGNAL_DATE, market_closes=[D("100")] * 199 + [D("101")],
            asset_types={"AAA": "STOCK"}, asset_histories={"AAA": bars}, universe_symbols={"AAA"},
        )[0]

        self.assertFalse(assessment.candidate.eligible)
        self.assertTrue(assessment.liquidity)
        self.assertFalse(assessment.momentum)
        self.assertEqual(assessment.breakout_strength, D("0"))
        self.assertEqual(assessment.momentum_60, D("0"))
        self.assertIn("MOMENTUM_REJECT", assessment.rejection_reasons)
        self.assertIn("NON_POSITIVE_SCORE", assessment.rejection_reasons)

    def test_ending_after_signal_date_is_a_data_quality_reject_without_lookahead(self) -> None:
        bars = make_bars("AAA", "STOCK", [D("1100")] * 61, final_date=SIGNAL_DATE + timedelta(days=1))

        assessment = assess_close_time_candidates(
            signal_date=SIGNAL_DATE, market_closes=[D("100")] * 200,
            asset_types={"AAA": "STOCK"}, asset_histories={"AAA": bars}, universe_symbols={"AAA"},
        )[0]

        self.assertFalse(assessment.candidate.eligible)
        self.assertIn("DATA_QUALITY_REJECT", assessment.rejection_reasons)
        self.assertIsNone(assessment.breakout_strength)

    def test_duplicate_dates_and_identity_mismatches_are_data_quality_rejects(self) -> None:
        bars = make_bars("AAA", "STOCK", [D("1100")] * 61)
        duplicate_dates = bars[:20] + (bars[19],) + bars[20:]
        cases = (
            ("duplicate", {"AAA": "STOCK"}, duplicate_dates),
            ("symbol", {"AAA": "STOCK"}, make_bars("OTHER", "STOCK", [D("1100")] * 61)),
            ("asset_type", {"AAA": "ETF"}, bars),
            ("malformed_type", {"AAA": 1}, bars),
        )

        for label, asset_types, history in cases:
            with self.subTest(label=label):
                assessment = assess_close_time_candidates(
                    signal_date=SIGNAL_DATE, market_closes=[D("100")] * 200,
                    asset_types=asset_types, asset_histories={"AAA": history}, universe_symbols={"AAA"},
                )[0]
                self.assertFalse(assessment.candidate.eligible)
                self.assertIn("DATA_QUALITY_REJECT", assessment.rejection_reasons)

    def test_insufficient_history_is_an_explicit_noneligible_result_with_neutral_candidate_scores(self) -> None:
        bars = make_bars("AAA", "STOCK", [D("1100")] * 20)

        assessment = assess_close_time_candidates(
            signal_date=SIGNAL_DATE, market_closes=[D("100")] * 199 + [D("101")],
            asset_types={"AAA": "STOCK"}, asset_histories={"AAA": bars}, universe_symbols={"AAA"},
        )[0]

        self.assertFalse(assessment.candidate.eligible)
        self.assertIsNone(assessment.breakout_strength)
        self.assertIsNone(assessment.momentum_60)
        self.assertGreater(assessment.candidate.breakout_strength, D("0"))
        self.assertGreater(assessment.candidate.momentum_60, D("0"))
        self.assertIn("INSUFFICIENT_HISTORY", assessment.rejection_reasons)

    def test_missing_or_malformed_history_mapping_is_reported_not_raised(self) -> None:
        for label, asset_types, histories in (
            ("missing", {}, {}),
            ("not_mapping", None, None),
            ("non_bar_history", {"AAA": "STOCK"}, {"AAA": ("not-a-bar",)}),
        ):
            with self.subTest(label=label):
                assessment = assess_close_time_candidates(
                    signal_date=SIGNAL_DATE,
                    market_closes=[D("100")] * 200,
                    asset_types=asset_types,
                    asset_histories=histories,
                    universe_symbols={"AAA"},
                )[0]
                self.assertFalse(assessment.candidate.eligible)
                self.assertIn("DATA_QUALITY_REJECT", assessment.rejection_reasons)
                self.assertIn("RISK_OFF", assessment.rejection_reasons)


if __name__ == "__main__":
    unittest.main()
