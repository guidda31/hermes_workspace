import unittest
from datetime import date
from decimal import Decimal

import pandas as pd

from swing_v2.public_data import FinanceDataReaderAdapter


class FinanceDataReaderAdapterTests(unittest.TestCase):
    def test_converts_public_daily_ohlcv_to_daily_bars(self) -> None:
        def fake_reader(symbol: str, start: str, end: str) -> pd.DataFrame:
            self.assertEqual((symbol, start, end), ("005930", "2026-01-02", "2026-01-05"))
            return pd.DataFrame(
                {
                    "Open": [1000],
                    "High": [1100],
                    "Low": [990],
                    "Close": [1050],
                    "Volume": [100000],
                },
                index=pd.to_datetime(["2026-01-02"]),
            )

        adapter = FinanceDataReaderAdapter(data_reader=fake_reader)
        bars = adapter.load_daily_bars(
            symbol="005930",
            asset_type="STOCK",
            start=date(2026, 1, 2),
            end=date(2026, 1, 5),
        )

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "005930")
        self.assertEqual(bars[0].trade_date, date(2026, 1, 2))
        self.assertEqual(bars[0].trading_value, Decimal("105000000"))


if __name__ == "__main__":
    unittest.main()
