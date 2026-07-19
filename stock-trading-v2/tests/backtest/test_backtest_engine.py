import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping, Sequence
from unittest.mock import patch

from swing_v2.contracts import DailyBar
from swing_v2.backtest.engine import (
    BacktestRunner,
    BacktestConfig,
    ExecutionCostConfig,
    Side,
    RunResult,
)

class MockBacktestData:
    def __init__(self, bars_by_date, calendar):
        self.bars_by_date = bars_by_date
        self.calendar = calendar

    def get_bars(self, trade_date: date) -> Mapping[str, DailyBar | None]:
        return self.bars_by_date.get(trade_date, {})

    def get_market_index_bar(self, trade_date: date) -> DailyBar | None:
        return DailyBar(trade_date, "KOSPI", "INDEX", Decimal("2500"), Decimal("2500"), Decimal("2500"), Decimal("2500"), 0, Decimal("0"), True)

    def get_historical_closes(self, symbol: str, end_date: date, window: int) -> Sequence[Decimal]:
        return [Decimal("1000.0")] * window

    def get_historical_bars(self, symbol: str, end_date: date, window: int) -> Sequence[DailyBar]:
        return [
            DailyBar(end_date - timedelta(days=window - i - 1), symbol, "STOCK", Decimal("1000"), Decimal("1000"), Decimal("1000"), Decimal("1000"), 1000, Decimal("1000000"), True)
            for i in range(window)
        ]

    def get_trade_calendar(self, start_date: date, end_date: date) -> Sequence[date]:
        return [d for d in self.calendar if start_date <= d <= end_date]

    def get_asset_type(self, symbol: str) -> str:
        return "STOCK"

class BacktestEngineTests(unittest.TestCase):
    def test_backtest_runner_multi_day_scenario(self):
        d1 = date(2024, 1, 1)
        d2 = date(2024, 1, 2)
        d3 = date(2024, 1, 3)
        d4 = date(2024, 1, 4)
        calendar = [d1, d2, d3, d4]
        symbol = "005930"
        
        # Day 1: Signal generated at close
        bar1 = DailyBar(d1, symbol, "STOCK", Decimal("1000"), Decimal("1100"), Decimal("950"), Decimal("1050"), 1000000, Decimal("1050000000"), True)
        # Day 2: Open at 1050 (Matches Day 1 close to satisfy strict execution engine), Close at 1070
        bar2 = DailyBar(d2, symbol, "STOCK", Decimal("1050"), Decimal("1080"), Decimal("1050"), Decimal("1070"), 1000000, Decimal("1070000000"), True)
        # Day 3: Open at 1070, Close at 900 (Stop loss)
        bar3 = DailyBar(d3, symbol, "STOCK", Decimal("1070"), Decimal("1080"), Decimal("850"), Decimal("900"), 1000000, Decimal("900000000"), True)
        # Day 4: Open at 900 (Matches Day 3 close), Close at 900
        bar4 = DailyBar(d4, symbol, "STOCK", Decimal("900"), Decimal("910"), Decimal("880"), Decimal("900"), 1000000, Decimal("900000000"), True)
        
        bars_by_date = {d1: {symbol: bar1}, d2: {symbol: bar2}, d3: {symbol: bar3}, d4: {symbol: bar4}}
        data = MockBacktestData(bars_by_date, calendar)
        
        costs = ExecutionCostConfig(
            buy_slippage_bps=Decimal("0"), sell_slippage_bps=Decimal("0"),
            buy_commission_bps=Decimal("0"), sell_commission_bps=Decimal("0"),
            sell_tax_bps_by_asset_type={"STOCK": Decimal("0")},
            fixed_fee_per_order=Decimal("0"),
            tick_rounder=lambda p, s: p.quantize(Decimal("1")),
        )
        
        config = BacktestConfig(
            start_date=d1, end_date=d4, universe=(symbol,),
            initial_cash=Decimal("10000000"), costs=costs,
            risk_parameters={
                "risk_per_position": Decimal("0.01"), "max_positions": 5,
                "max_position_notional_pct": Decimal("0.2"), "initial_stop_pct": Decimal("0.05"),
                "max_daily_loss_pct": Decimal("0.03"),
            },
            signal_parameters={}
        )
        
        runner = BacktestRunner()
        
        with patch("swing_v2.backtest.close_time_candidates.is_risk_on", return_value=True), \
             patch("swing_v2.backtest.close_time_candidates.passes_liquidity_filter", return_value=True), \
             patch("swing_v2.backtest.close_time_candidates.is_momentum_breakout", return_value=True), \
             patch("swing_v2.backtest.close_time_candidates._scores", return_value=(Decimal("0.1"), Decimal("0.1"))):
            
            result = runner.run(config, data)
        
        self.assertEqual(len(result.all_day_results), 4)
        
        fills = sorted(result.fills, key=lambda f: f.trade_date)
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[0].side, Side.BUY)
        self.assertEqual(fills[0].trade_date, d2)
        self.assertEqual(fills[0].fill_price, Decimal("1050"))
        
        self.assertEqual(fills[1].side, Side.SELL)
        self.assertEqual(fills[1].trade_date, d4)
        self.assertEqual(fills[1].fill_price, Decimal("900"))
        
        self.assertEqual(len(result.positions), 1)
        pos = result.positions[0]
        self.assertEqual(pos.symbol, symbol)
        self.assertEqual(pos.status, "CLOSED")
        self.assertEqual(pos.exit_reason, "STOP_CLOSE")

    def test_backtest_runner_daily_loss_guard_blocks_entry(self):
        d1 = date(2024, 1, 1)
        d2 = date(2024, 1, 2)
        calendar = [d1, d2]
        symbol = "005930"
        
        bar1 = DailyBar(d1, symbol, "STOCK", Decimal("1000"), Decimal("1100"), Decimal("950"), Decimal("1050"), 1000000, Decimal("1050000000"), True)
        bar2 = DailyBar(d2, symbol, "STOCK", Decimal("1060"), Decimal("1080"), Decimal("1050"), Decimal("1070"), 1000000, Decimal("1070000000"), True)
        
        bars_by_date = {d1: {symbol: bar1}, d2: {symbol: bar2}}
        data = MockBacktestData(bars_by_date, calendar)
        
        costs = ExecutionCostConfig(
            buy_slippage_bps=Decimal("0"), sell_slippage_bps=Decimal("0"),
            buy_commission_bps=Decimal("0"), sell_commission_bps=Decimal("0"),
            sell_tax_bps_by_asset_type={"STOCK": Decimal("0")},
            fixed_fee_per_order=Decimal("0"),
            tick_rounder=lambda p, s: p.quantize(Decimal("1")),
        )
        
        config = BacktestConfig(
            start_date=d1, end_date=d2, universe=(symbol,),
            initial_cash=Decimal("10000000"), costs=costs,
            risk_parameters={
                "risk_per_position": Decimal("0.01"), "max_positions": 5,
                "max_position_notional_pct": Decimal("0.2"), "initial_stop_pct": Decimal("0.05"),
                "max_daily_loss_pct": Decimal("0.01"),
            },
            signal_parameters={}
        )
        
        runner = BacktestRunner()
        
        with patch("swing_v2.backtest.portfolio_day.run_portfolio_day") as mock_run:
            from swing_v2.backtest.portfolio_valuation import PortfolioValuation
            from swing_v2.backtest.portfolio_day import PortfolioDayResult
            from swing_v2.backtest.portfolio_state import PortfolioState
            
            val1 = PortfolioValuation(d1, Decimal("9500000"), Decimal("0"), Decimal("9500000"), {}, Decimal("0"))
            state1 = PortfolioState(Decimal("9500000"), (), (), ())
            res1 = PortfolioDayResult(d1, state1, state1, RunResult(Decimal("9500000"), (), (), ()), RunResult(Decimal("9500000"), (), (), ()), (), val1)
            
            val2 = PortfolioValuation(d2, Decimal("9500000"), Decimal("0"), Decimal("9500000"), {}, Decimal("0"))
            res2 = PortfolioDayResult(d2, state1, state1, RunResult(Decimal("9500000"), (), (), ()), RunResult(Decimal("9500000"), (), (), ()), (), val2)
            
            mock_run.side_effect = [res1, res2]
            
            with patch("swing_v2.backtest.close_time_candidates.is_risk_on", return_value=True), \
                 patch("swing_v2.backtest.close_time_candidates.passes_liquidity_filter", return_value=True), \
                 patch("swing_v2.backtest.close_time_candidates.is_momentum_breakout", return_value=True), \
                 patch("swing_v2.backtest.close_time_candidates._scores", return_value=(Decimal("0.1"), Decimal("0.1"))):
                
                result = runner.run(config, data)
                
        self.assertEqual(len(result.fills), 0)

if __name__ == "__main__":
    unittest.main()
