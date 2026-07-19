"""Validated input contracts for daily KRX market data."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping


def _parse_boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError(f"is_tradable must be a boolean-like value, got {value!r}")


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    symbol: str
    asset_type: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trading_value: Decimal
    is_tradable: bool

    def __post_init__(self) -> None:
        values = (self.open, self.high, self.low, self.close, self.trading_value)
        if not all(value.is_finite() for value in values):
            raise ValueError("daily bar price and trading value must be finite")
        if self.low > self.high or min(self.open, self.close) < self.low or max(self.open, self.close) > self.high:
            raise ValueError("daily bar prices must satisfy low <= open/close <= high")
        if self.low <= 0 or self.volume < 0 or self.trading_value < 0:
            raise ValueError("daily bar price, volume, and trading value must be non-negative")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "DailyBar":
        return cls(
            trade_date=date.fromisoformat(str(raw["trade_date"])),
            symbol=str(raw["symbol"]),
            asset_type=str(raw["asset_type"]),
            open=Decimal(str(raw["open"])),
            high=Decimal(str(raw["high"])),
            low=Decimal(str(raw["low"])),
            close=Decimal(str(raw["close"])),
            volume=int(str(raw["volume"])),
            trading_value=Decimal(str(raw["trading_value"])),
            is_tradable=_parse_boolean(raw["is_tradable"]),
        )
