"""Market-regime signals calculated from completed daily close series."""

from collections.abc import Sequence
from decimal import Decimal

from .contracts import DailyBar


def is_risk_on(
    closes: Sequence[Decimal], *, long_window: int = 200, short_window: int = 50
) -> bool:
    """Return whether the latest close satisfies the short/long-window trend filter.

    The long/short windows are doc-04 §3.1 hypothesis values (default 200/50); they
    are parameters, not constants, so the regime warm-up and sensitivity can be
    validated rather than fixed. A shorter long_window trades sooner but reacts more
    to noise. Fewer than ``long_window`` closes is conservatively risk-off.
    """
    if isinstance(long_window, bool) or type(long_window) is not int or long_window < 2:
        raise ValueError("long_window must be an int >= 2")
    if isinstance(short_window, bool) or type(short_window) is not int or not 0 < short_window <= long_window:
        raise ValueError("short_window must be an int in (0, long_window]")
    if len(closes) < long_window:
        return False

    moving_average_long = sum(closes[-long_window:]) / long_window
    moving_average_short = sum(closes[-short_window:]) / short_window
    return closes[-1] > moving_average_long and moving_average_short > moving_average_long


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
