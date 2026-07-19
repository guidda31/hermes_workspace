"""Pure close-time mark-to-market valuation for immutable portfolio snapshots."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from swing_v2.contracts import DailyBar

from .engine import Fill, Order, Position
from .portfolio_state import PortfolioState


@dataclass(frozen=True)
class PortfolioValuation:
    """A close-time NAV snapshot for the OPEN positions in a portfolio.

    ``unrealized_pnl`` deliberately compares close with ``entry_price`` only.
    Entry commissions and fees were already deducted from ``cash`` when the BUY
    fill was applied, so subtracting them again here would double count them.
    """

    date: date
    cash: Decimal
    open_market_value: Decimal
    nav: Decimal
    unrealized_pnl_by_symbol: Mapping[str, Decimal]
    unrealized_pnl_total: Decimal

    def __post_init__(self) -> None:
        if type(self.date) is not date:
            raise ValueError("valuation date must be a plain date")
        for name, value in (
            ("cash", self.cash),
            ("open_market_value", self.open_market_value),
            ("nav", self.nav),
            ("unrealized_pnl_total", self.unrealized_pnl_total),
        ):
            _require_finite_decimal(value, name)
        if not isinstance(self.unrealized_pnl_by_symbol, Mapping):
            raise ValueError("unrealized_pnl_by_symbol must be a mapping")
        pnl_by_symbol = dict(self.unrealized_pnl_by_symbol)
        if not all(isinstance(symbol, str) and symbol for symbol in pnl_by_symbol):
            raise ValueError("unrealized_pnl_by_symbol keys must be nonempty strings")
        for value in pnl_by_symbol.values():
            _require_finite_decimal(value, "unrealized PnL")
        if self.nav != self.cash + self.open_market_value:
            raise ValueError("nav must equal cash plus open_market_value")
        if self.unrealized_pnl_total != sum(pnl_by_symbol.values(), Decimal("0")):
            raise ValueError("unrealized_pnl_total must equal the per-symbol total")
        object.__setattr__(self, "unrealized_pnl_by_symbol", MappingProxyType(pnl_by_symbol))


def mark_to_market(
    *,
    state: PortfolioState,
    bars_by_symbol: Mapping[str, DailyBar | None],
    valuation_date: date,
) -> PortfolioValuation:
    """Value every OPEN position at a valid bar's close on ``valuation_date``.

    A valuation is intentionally all-or-nothing: each OPEN position requires a
    same-day, tradable bar with positive close/open/volume/trading value.  CLOSED
    positions are excluded and therefore require no bar.
    """
    _validate_inputs(state, bars_by_symbol, valuation_date)

    market_value = Decimal("0")
    pnl_by_symbol: dict[str, Decimal] = {}
    for position in state.positions:
        if position.status != "OPEN":
            continue
        bar = bars_by_symbol[position.symbol]
        assert bar is not None
        value = Decimal(position.quantity) * bar.close
        pnl = (bar.close - position.entry_price) * Decimal(position.quantity)
        market_value += value
        pnl_by_symbol[position.symbol] = pnl

    return PortfolioValuation(
        date=valuation_date,
        cash=state.cash,
        open_market_value=market_value,
        nav=state.cash + market_value,
        unrealized_pnl_by_symbol=pnl_by_symbol,
        unrealized_pnl_total=sum(pnl_by_symbol.values(), Decimal("0")),
    )


def _validate_inputs(
    state: object, bars_by_symbol: object, valuation_date: object
) -> None:
    if type(valuation_date) is not date:
        raise ValueError("valuation_date must be a plain date")
    _validate_state(state)
    if not isinstance(bars_by_symbol, Mapping):
        raise ValueError("bars_by_symbol must be a mapping")
    for symbol, bar in bars_by_symbol.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("bars_by_symbol keys must be nonempty strings")
        if bar is not None and not isinstance(bar, DailyBar):
            raise ValueError("bars_by_symbol values must be DailyBar or None")

    assert isinstance(state, PortfolioState)
    for position in state.positions:
        if position.status != "OPEN":
            continue
        if position.symbol not in bars_by_symbol or bars_by_symbol[position.symbol] is None:
            raise ValueError("a valuation bar is required for every OPEN position")
        bar = bars_by_symbol[position.symbol]
        assert isinstance(bar, DailyBar)
        if (bar.symbol, bar.asset_type) != (position.symbol, position.asset_type):
            raise ValueError("valuation bar identity must match its OPEN position")
        if bar.trade_date != valuation_date:
            raise ValueError("valuation bar trade_date must equal valuation_date")
        if (
            not bar.is_tradable
            or not all(value.is_finite() and value > 0 for value in (bar.open, bar.close, bar.trading_value))
            or isinstance(bar.volume, bool)
            or not isinstance(bar.volume, int)
            or bar.volume <= 0
        ):
            raise ValueError("valuation bar must be tradable with positive close, open, volume, and trading value")


def _validate_state(state: object) -> None:
    if not isinstance(state, PortfolioState):
        raise ValueError("state must be a PortfolioState")
    _require_finite_decimal(state.cash, "state cash")
    if state.cash < 0:
        raise ValueError("state cash must be non-negative")
    for values, item_type, name in (
        (state.positions, Position, "positions"),
        (state.orders, Order, "orders"),
        (state.fills, Fill, "fills"),
    ):
        if not isinstance(values, tuple) or not all(isinstance(value, item_type) for value in values):
            raise ValueError(f"state {name} must be a tuple of {item_type.__name__} values")

    position_ids = tuple(position.position_id for position in state.positions)
    if any(not isinstance(position_id, str) or not position_id for position_id in position_ids):
        raise ValueError("state position_ids must be nonempty strings")
    if len(position_ids) != len(set(position_ids)):
        raise ValueError("state positions must have unique position_ids")

    open_symbols: set[str] = set()
    for position in state.positions:
        if position.status not in {"OPEN", "CLOSED"}:
            raise ValueError("position status must be OPEN or CLOSED")
        if position.status != "OPEN":
            continue
        if not isinstance(position.symbol, str) or not position.symbol:
            raise ValueError("OPEN position symbol must be a nonempty string")
        if not isinstance(position.asset_type, str) or not position.asset_type:
            raise ValueError("OPEN position asset_type must be a nonempty string")
        if position.symbol in open_symbols:
            raise ValueError("OPEN positions must have unique symbols")
        open_symbols.add(position.symbol)
        _require_finite_decimal(position.entry_price, "OPEN position entry_price")
        if position.entry_price <= 0:
            raise ValueError("OPEN position entry_price must be positive")
        if isinstance(position.quantity, bool) or not isinstance(position.quantity, int) or position.quantity <= 0:
            raise ValueError("OPEN position quantity must be a positive integer")


def _require_finite_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
