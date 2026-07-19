"""Public daily-price data adapter for first-pass KRX research."""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pandas as pd

from .contracts import DailyBar


class FinanceDataReaderAdapter:
    """Normalize FinanceDataReader OHLCV output into the local data contract.

    FinanceDataReader does not return an exchange-reported trading-value field,
    so this adapter records a close-price-times-volume liquidity proxy. It must
    not be treated as the official KRX intraday transaction value.
    """

    def __init__(self, data_reader: Callable[[str, str, str], pd.DataFrame]) -> None:
        self._data_reader = data_reader

    def load_daily_bars(
        self,
        *,
        symbol: str,
        asset_type: str,
        start: date,
        end: date,
    ) -> tuple[DailyBar, ...]:
        frame = self._data_reader(symbol, start.isoformat(), end.isoformat())
        required_columns = {"Open", "High", "Low", "Close", "Volume"}
        missing_columns = required_columns.difference(frame.columns)
        if missing_columns:
            raise ValueError(f"public data is missing required columns: {sorted(missing_columns)}")

        bars = []
        for timestamp, row in frame.iterrows():
            close = Decimal(str(row["Close"]))
            volume = int(row["Volume"])
            bars.append(
                DailyBar(
                    trade_date=pd.Timestamp(timestamp).date(),
                    symbol=symbol,
                    asset_type=asset_type,
                    open=Decimal(str(row["Open"])),
                    high=Decimal(str(row["High"])),
                    low=Decimal(str(row["Low"])),
                    close=close,
                    volume=volume,
                    trading_value=close * volume,
                    is_tradable=volume > 0,
                )
            )
        return tuple(bars)
