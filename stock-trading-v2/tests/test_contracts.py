import unittest
from datetime import date
from decimal import Decimal

from swing_v2.contracts import DailyBar


class DailyBarContractTests(unittest.TestCase):
    def test_accepts_a_complete_tradable_krx_daily_bar(self) -> None:
        bar = DailyBar.from_mapping(
            {
                "trade_date": "2026-01-02",
                "symbol": "005930",
                "asset_type": "STOCK",
                "open": "1000",
                "high": "1100",
                "low": "990",
                "close": "1050",
                "volume": "100000",
                "trading_value": "105000000",
                "is_tradable": True,
            }
        )

        self.assertEqual(bar.trade_date, date(2026, 1, 2))
        self.assertEqual(bar.symbol, "005930")
        self.assertEqual(bar.close, Decimal("1050"))

    def test_parses_false_string_as_not_tradable(self) -> None:
        bar = DailyBar.from_mapping(
            {
                "trade_date": "2026-01-02",
                "symbol": "005930",
                "asset_type": "STOCK",
                "open": "1000",
                "high": "1100",
                "low": "990",
                "close": "1050",
                "volume": "100000",
                "trading_value": "105000000",
                "is_tradable": "false",
            }
        )

        self.assertFalse(bar.is_tradable)

    def test_rejects_non_finite_price_or_trading_value(self) -> None:
        raw = {
            "trade_date": "2026-01-02",
            "symbol": "005930",
            "asset_type": "STOCK",
            "open": "1000",
            "high": "Infinity",
            "low": "990",
            "close": "1050",
            "volume": "100000",
            "trading_value": "105000000",
            "is_tradable": True,
        }

        with self.assertRaises(ValueError):
            DailyBar.from_mapping(raw)

    def test_rejects_a_bar_with_an_invalid_price_range(self) -> None:
        with self.assertRaises(ValueError):
            DailyBar.from_mapping(
                {
                    "trade_date": "2026-01-02",
                    "symbol": "005930",
                    "asset_type": "STOCK",
                    "open": "1000",
                    "high": "900",
                    "low": "990",
                    "close": "1050",
                    "volume": "100000",
                    "trading_value": "105000000",
                    "is_tradable": True,
                }
            )


if __name__ == "__main__":
    unittest.main()
