"""Market-regime signals calculated from completed daily close series."""

from collections.abc import Sequence
from decimal import Decimal

from .contracts import DailyBar


def is_risk_on(closes: Sequence[Decimal]) -> bool:
    """Return whether the latest close satisfies the 50/200-day trend filter."""
    if len(closes) < 200:
        return False

    moving_average_200 = sum(closes[-200:]) / 200
    moving_average_50 = sum(closes[-50:]) / 50
    return closes[-1] > moving_average_200 and moving_average_50 > moving_average_200


def is_momentum_breakout(closes: Sequence[Decimal]) -> bool:
    """Return whether the latest close satisfies the initial momentum-breakout filter."""
    if len(closes) < 61:
        return False

    latest_close = closes[-1]
    moving_average_20 = sum(closes[-20:]) / 20
    moving_average_60 = sum(closes[-60:]) / 60
    return (
        latest_close > moving_average_20
        and moving_average_20 > moving_average_60
        and latest_close > max(closes[-21:-1])
        and latest_close > closes[-61]
    )


def passes_liquidity_filter(bars: Sequence[DailyBar]) -> bool:
    """Return whether a symbol has enough recent daily-bar history."""
    recent_bars = bars[-20:]
    return (
        len(recent_bars) == 20
        and all(bar.is_tradable for bar in recent_bars)
        and recent_bars[-1].close >= Decimal("1000")
        and sum(bar.trading_value for bar in recent_bars) / len(recent_bars) >= Decimal("1000000000")
    )


def should_open_position(
    market_closes: Sequence[Decimal], asset_bars: Sequence[DailyBar]
) -> bool:
    """Return whether all market, liquidity, and momentum entry filters pass."""
    return (
        is_risk_on(market_closes)
        and passes_liquidity_filter(asset_bars)
        and is_momentum_breakout([bar.close for bar in asset_bars])
    )
