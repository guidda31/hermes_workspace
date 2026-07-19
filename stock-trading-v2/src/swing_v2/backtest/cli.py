"""Run the backtest engine from a snapshot and emit metrics + doc-04 §7 ledgers.

To exercise the engine on a plain OHLCV snapshot this builds a **RESEARCH, non-PIT,
non-survivorship-corrected** universe metadata: every snapshot symbol is treated as an
eligible STOCK/ETF effective from the first calendar date. That is deliberately NOT a
point-in-time classification (it applies today's status to the past and ignores
delistings), so results are for baseline hypothesis exploration only (doc-04 §9) and
must never be read as a tradeable expected return.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
from pathlib import Path
import sys
from typing import Optional

from ..backtest_data import SnapshotBacktestData, load_snapshot
from ..universe_metadata import (
    AssetType,
    EtfExposure,
    MetadataProvenance,
    UniverseMetadataRecord,
    UniverseMetadataSnapshot,
)
from .backtest_engine import BacktestConfig, BacktestResult, BacktestRiskConfig, BacktestRunner
from .engine import ExecutionCostConfig, Side
from .ledgers import write_backtest_ledgers, write_run_summary
from .metrics import BacktestMetrics, build_backtest_metrics

_RESEARCH_SOURCE = "RESEARCH_NON_PIT"


def _tick_round(price: Decimal, side: Side) -> Decimal:
    return price.quantize(Decimal("1"), rounding=ROUND_CEILING if side is Side.BUY else ROUND_FLOOR)


def default_costs() -> ExecutionCostConfig:
    """The doc-04 §6 base cost scenario (hypothesis values), 1-won tick rounding."""
    return ExecutionCostConfig(
        buy_slippage_bps=Decimal("10"), sell_slippage_bps=Decimal("10"),
        buy_commission_bps=Decimal("1.5"), sell_commission_bps=Decimal("1.5"),
        sell_tax_bps_by_asset_type={"STOCK": Decimal("20"), "ETF": Decimal("0")},
        fixed_fee_per_order=Decimal("0"), tick_rounder=_tick_round,
    )


def stress_costs() -> ExecutionCostConfig:
    """A harsher scenario: 3x slippage and 2x commission over the base (doc-04 §6)."""
    return ExecutionCostConfig(
        buy_slippage_bps=Decimal("30"), sell_slippage_bps=Decimal("30"),
        buy_commission_bps=Decimal("3"), sell_commission_bps=Decimal("3"),
        sell_tax_bps_by_asset_type={"STOCK": Decimal("20"), "ETF": Decimal("0")},
        fixed_fee_per_order=Decimal("0"), tick_rounder=_tick_round,
    )


def default_risk() -> BacktestRiskConfig:
    return BacktestRiskConfig(
        risk_per_position=Decimal("0.01"), max_positions=5,
        max_position_notional_pct=Decimal("0.20"), initial_stop_pct=Decimal("0.05"),
        max_daily_loss_pct=Decimal("0.03"), max_gap_up_pct=Decimal("0.05"),
    )


def build_research_metadata(asset_types: Mapping[str, str], *, as_of: date) -> UniverseMetadataSnapshot:
    """Build a RESEARCH (non-PIT) metadata making every symbol eligible from ``as_of``."""
    if not isinstance(asset_types, Mapping) or not asset_types:
        raise ValueError("asset_types must be a nonempty mapping")
    if type(as_of) is not date:
        raise ValueError("as_of must be a plain date")
    digest = hashlib.sha256("|".join(sorted(asset_types)).encode("utf-8")).hexdigest()
    provenance = MetadataProvenance(source=_RESEARCH_SOURCE, version="v0", content_hash=f"sha256:{digest}", as_of=as_of)
    records = []
    for symbol in sorted(asset_types):
        asset_type = AssetType(asset_types[symbol])
        exposure = EtfExposure.DOMESTIC_INDEX_OR_SECTOR if asset_type is AssetType.ETF else None
        records.append(UniverseMetadataRecord(
            symbol=symbol, asset_type=asset_type, effective_from=as_of, effective_to=None,
            flags=frozenset(), etf_exposure=exposure, provenance=provenance,
        ))
    return UniverseMetadataSnapshot(tuple(records))


def run_backtest_from_snapshot(
    *,
    snapshot_path,
    initial_cash: Decimal,
    risk: Optional[BacktestRiskConfig] = None,
    costs: Optional[ExecutionCostConfig] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    output_dir=None,
    annualization_days: int = 252,
) -> tuple[BacktestResult, BacktestMetrics]:
    """Run the engine over a snapshot with RESEARCH metadata; return result + metrics.

    ``costs`` and ``start_date``/``end_date`` may be overridden for cost-stress and
    walk-forward scenarios; a restricted window still uses the full prior history for
    its indicators (no lookahead), only its own dates generate trades.
    """
    snapshot = load_snapshot(snapshot_path)
    data = SnapshotBacktestData(snapshot)
    calendar = tuple(snapshot.trade_calendar)
    if not calendar:
        raise ValueError("snapshot has an empty trade calendar")
    window_start = calendar[0] if start_date is None else start_date
    window_end = calendar[-1] if end_date is None else end_date
    if type(window_start) is not date or type(window_end) is not date or window_start > window_end:
        raise ValueError("start_date/end_date must be plain ordered dates")
    asset_types = dict(snapshot.asset_types)
    config = BacktestConfig(
        start_date=window_start, end_date=window_end, universe=tuple(sorted(asset_types)),
        market_symbol=snapshot.market_symbol, initial_cash=initial_cash,
        costs=costs if costs is not None else default_costs(),
        risk=risk if risk is not None else default_risk(),
        universe_metadata=build_research_metadata(asset_types, as_of=calendar[0]),
    )
    result = BacktestRunner().run(config, data)
    metrics = build_backtest_metrics(result, initial_cash=initial_cash, annualization_days=annualization_days)
    if output_dir is not None:
        write_backtest_ledgers(result, output_dir)
        write_run_summary(result, output_dir, config_summary=_config_summary(config))
    return result, metrics


def _config_summary(config: BacktestConfig) -> dict:
    risk = config.risk
    return {
        "start_date": config.start_date.isoformat(), "end_date": config.end_date.isoformat(),
        "universe": list(config.universe), "market_symbol": config.market_symbol,
        "initial_cash": str(config.initial_cash),
        "metadata_note": "RESEARCH_NON_PIT: today's classification applied to the past; not tradeable",
        "risk": {
            "risk_per_position": str(risk.risk_per_position), "max_positions": risk.max_positions,
            "max_position_notional_pct": str(risk.max_position_notional_pct),
            "initial_stop_pct": str(risk.initial_stop_pct), "max_daily_loss_pct": str(risk.max_daily_loss_pct),
            "max_gap_up_pct": str(risk.max_gap_up_pct),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swing_v2.backtest.cli", description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--initial-cash", required=True, type=Decimal)
    parser.add_argument("--output-dir", default=None)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _, m = run_backtest_from_snapshot(
        snapshot_path=args.snapshot, initial_cash=args.initial_cash, output_dir=args.output_dir,
    )
    sys.stdout.write(
        "RESEARCH backtest (non-PIT, baseline only):\n"
        f"  total_return={m.total_return}  cagr={m.cagr}  sharpe={m.annualized_sharpe}\n"
        f"  max_drawdown={m.max_drawdown} ({m.max_drawdown_peak_date}->{m.max_drawdown_trough_date})\n"
        f"  round_trips={m.closed_round_trips} win_rate={m.win_rate} profit_factor={m.profit_factor}\n"
        f"  avg_win={m.average_win} avg_loss={m.average_loss} avg_hold={m.average_holding_sessions}\n"
        f"  fills={m.total_fills} costs={m.total_costs} stale_days={m.stale_mark_days}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
