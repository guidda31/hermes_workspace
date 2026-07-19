import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from decimal import Decimal

from swing_v2.backtest import PortfolioState, PortfolioValuation, Position, mark_to_market
from swing_v2.contracts import DailyBar


D = Decimal
VALUATION_DATE = date(2026, 2, 2)


def position(
    symbol: str,
    *,
    asset_type: str = "STOCK",
    entry_price: str = "100",
    quantity: int = 10,
    status: str = "OPEN",
) -> Position:
    return Position(
        position_id=f"position-{symbol}", symbol=symbol, asset_type=asset_type,
        entry_order_id=f"entry-order-{symbol}", entry_fill_id=f"entry-fill-{symbol}",
        entry_price=D(entry_price), initial_stop_price=D("90"), quantity=quantity,
        exit_order_id="exit-order" if status == "CLOSED" else None,
        exit_fill_id="exit-fill" if status == "CLOSED" else None,
        exit_price=D("99") if status == "CLOSED" else None,
        exit_reason="EXIT" if status == "CLOSED" else None,
        status=status, age_sessions=1,
    )


def bar(
    symbol: str,
    *,
    close: str = "100",
    asset_type: str = "STOCK",
    trade_date: date = VALUATION_DATE,
    is_tradable: bool = True,
    volume: int = 100,
    trading_value: str = "10000",
) -> DailyBar:
    price = D(close)
    return DailyBar(
        trade_date=trade_date, symbol=symbol, asset_type=asset_type,
        open=price, high=price, low=price, close=price, volume=volume,
        trading_value=D(trading_value), is_tradable=is_tradable,
    )


def state(*positions: Position) -> PortfolioState:
    return PortfolioState(cash=D("123.45"), positions=positions, orders=(), fills=())


class PortfolioValuationTests(unittest.TestCase):
    def test_marks_multiple_open_positions_and_excludes_closed_position(self) -> None:
        portfolio = state(
            position("WIN", entry_price="100", quantity=3),
            position("LOSS", entry_price="50", quantity=4),
            position("CLOSED", entry_price="1", quantity=99, status="CLOSED"),
        )

        result = mark_to_market(
            state=portfolio,
            bars_by_symbol={"WIN": bar("WIN", close="110"), "LOSS": bar("LOSS", close="45")},
            valuation_date=VALUATION_DATE,
        )

        self.assertIsInstance(result, PortfolioValuation)
        self.assertEqual(result.date, VALUATION_DATE)
        self.assertEqual(result.cash, D("123.45"))
        self.assertEqual(result.open_market_value, D("510"))
        self.assertEqual(result.nav, D("633.45"))
        self.assertEqual(result.unrealized_pnl_by_symbol, {"WIN": D("30"), "LOSS": D("-20")})
        self.assertEqual(result.unrealized_pnl_total, D("10"))
        with self.assertRaises(FrozenInstanceError):
            result.nav = D("0")  # type: ignore[misc]

    def test_closed_position_needs_no_bar(self) -> None:
        result = mark_to_market(
            state=state(position("CLOSED", status="CLOSED")),
            bars_by_symbol={}, valuation_date=VALUATION_DATE,
        )

        self.assertEqual(result.open_market_value, D("0"))
        self.assertEqual(result.nav, D("123.45"))
        self.assertEqual(result.unrealized_pnl_by_symbol, {})
        self.assertEqual(result.unrealized_pnl_total, D("0"))

    def test_stale_marks_missing_or_untradable_bar_using_fallback(self) -> None:
        portfolio = state(position("OPEN", entry_price="100", quantity=10))
        stale_bar_sets = (
            ("missing", {}),
            ("none", {"OPEN": None}),
            ("untradable", {"OPEN": bar("OPEN", is_tradable=False)}),
            ("zero volume", {"OPEN": bar("OPEN", volume=0)}),
            ("zero trading value", {"OPEN": bar("OPEN", trading_value="0")}),
        )
        for name, bars in stale_bar_sets:
            with self.subTest(name=name):
                result = mark_to_market(
                    state=portfolio, bars_by_symbol=bars, valuation_date=VALUATION_DATE,
                    fallback_close_by_symbol={"OPEN": D("95")},
                )
                self.assertEqual(result.stale_mark_count, 1)
                self.assertEqual(result.stale_symbols, ("OPEN",))
                self.assertEqual(result.open_market_value, D("950"))  # 10 shares * 95 fallback
                self.assertEqual(result.unrealized_pnl_by_symbol, {"OPEN": D("-50")})

    def test_stale_mark_falls_back_to_entry_price_without_a_fallback_close(self) -> None:
        portfolio = state(position("OPEN", entry_price="100", quantity=10))
        result = mark_to_market(
            state=portfolio, bars_by_symbol={"OPEN": None}, valuation_date=VALUATION_DATE,
        )
        self.assertEqual(result.stale_mark_count, 1)
        self.assertEqual(result.open_market_value, D("1000"))  # 10 * entry_price 100
        self.assertEqual(result.unrealized_pnl_by_symbol, {"OPEN": D("0")})

    def test_valid_bar_is_not_stale(self) -> None:
        portfolio = state(position("OPEN", quantity=10))
        result = mark_to_market(
            state=portfolio, bars_by_symbol={"OPEN": bar("OPEN", close="110")},
            valuation_date=VALUATION_DATE, fallback_close_by_symbol={"OPEN": D("95")},
        )
        self.assertEqual(result.stale_mark_count, 0)
        self.assertEqual(result.stale_symbols, ())
        self.assertEqual(result.open_market_value, D("1100"))  # marks at the live close, not fallback

    def test_wrong_trade_date_bar_still_raises(self) -> None:
        portfolio = state(position("OPEN"))
        with self.assertRaises(ValueError):
            mark_to_market(
                state=portfolio,
                bars_by_symbol={"OPEN": bar("OPEN", trade_date=date(2026, 2, 1))},
                valuation_date=VALUATION_DATE,
            )

    def test_rejects_bar_symbol_or_asset_identity_mismatch(self) -> None:
        portfolio = state(position("OPEN"))
        for name, invalid_bar in (
            ("symbol", bar("OTHER")),
            ("asset", bar("OPEN", asset_type="ETF")),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    mark_to_market(
                        state=portfolio, bars_by_symbol={"OPEN": invalid_bar}, valuation_date=VALUATION_DATE
                    )

    def test_rejects_invalid_state_date_and_open_position_identity(self) -> None:
        portfolio = state(position("OPEN"))
        valid_bars = {"OPEN": bar("OPEN")}
        for name, invalid_state, invalid_date in (
            ("not state", object(), VALUATION_DATE),
            ("datetime valuation date", portfolio, datetime(2026, 2, 2)),
            ("missing valuation date", portfolio, None),
            ("invalid quantity", state(replace(position("BAD"), quantity=0)), VALUATION_DATE),
            ("invalid entry price", state(replace(position("BAD"), entry_price=D("NaN"))), VALUATION_DATE),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    mark_to_market(
                        state=invalid_state, bars_by_symbol=valid_bars, valuation_date=invalid_date  # type: ignore[arg-type]
                    )

    def test_rejects_duplicate_open_position_identity_in_tampered_state(self) -> None:
        original = position("OPEN")
        duplicate = replace(original, position_id="another-position")
        tampered = object.__new__(PortfolioState)
        object.__setattr__(tampered, "cash", D("123.45"))
        object.__setattr__(tampered, "positions", (original, duplicate))
        object.__setattr__(tampered, "orders", ())
        object.__setattr__(tampered, "fills", ())

        with self.assertRaises(ValueError):
            mark_to_market(
                state=tampered, bars_by_symbol={"OPEN": bar("OPEN")}, valuation_date=VALUATION_DATE
            )


if __name__ == "__main__":
    unittest.main()
