"""Paper-trading CLI a Hermes routine drives each session (no real money/order/network).

Two-step, mirroring the forward CLI:
- ``render``: build the point-in-time brief (showing held positions and whether the
  kill switch blocks entries) and print the prompt for the agent to reason over.
- ``run``: take the agent's JSON reply, guardrail it against the recovered account,
  and run one durable paper session at the execution date.
Plus ``report`` (cumulative performance), ``halt`` / ``resume`` (kill switch).

Costs use the documented doc-04 §6 base scenario; the account and all fills are
simulated and persisted to local files. Nothing here submits a real order.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
import sys
from typing import Optional, Sequence

from ..backtest_data import SnapshotBacktestData, load_snapshot
from ..backtest.engine import ExecutionCostConfig, Side
from ..llm.brief import build_brief
from ..llm.decision import parse_decision_set
from ..llm.guardrail import GuardrailConfig, PortfolioContext, apply_guardrails
from ..llm.prompt import parse_agent_response, render_brief_prompt
from .kill_switch import engage_kill_switch, is_kill_switch_engaged, release_kill_switch
from .ledger import load_latest_account
from .runner import paper_report, run_paper_session
from .session import PaperSessionResult

_KST = timezone(timedelta(hours=9))
_DEFAULT_WINDOW = 120
_DEFAULT_MAX_GAP_UP_PCT = Decimal("0.05")


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


def _load_data(snapshot_path) -> SnapshotBacktestData:
    return SnapshotBacktestData(load_snapshot(snapshot_path))


def _held_symbols(session_dir) -> frozenset[str]:
    account = load_latest_account(session_dir)
    return frozenset(p.symbol for p in account.positions) if account is not None else frozenset()


def render_paper_prompt(
    *, snapshot_path, signal_date: date, symbols: Sequence[str],
    session_dir, kill_switch_path, window: int = _DEFAULT_WINDOW,
) -> str:
    """Render the agent prompt, reflecting current holdings and the kill switch."""
    brief = build_brief(_load_data(snapshot_path), signal_date=signal_date, symbols=symbols, window=window)
    portfolio = PortfolioContext(_held_symbols(session_dir), is_kill_switch_engaged(kill_switch_path))
    return render_brief_prompt(brief, portfolio=portfolio)


def run_paper_day(
    *, snapshot_path, signal_date: date, execution_date: date, symbols: Sequence[str],
    agent_reply: str, eligible: frozenset[str], session_dir, kill_switch_path,
    initial_cash: Decimal, max_gap_up_pct: Optional[Decimal] = _DEFAULT_MAX_GAP_UP_PCT,
    window: int = _DEFAULT_WINDOW,
) -> PaperSessionResult:
    """Guardrail the agent's reply against the recovered account and run one session."""
    data = _load_data(snapshot_path)
    brief = build_brief(data, signal_date=signal_date, symbols=symbols, window=window)
    decisions = parse_decision_set(
        parse_agent_response(agent_reply),
        known_symbols=brief.known_symbols, known_evidence_ids=brief.known_evidence_ids,
    )
    # Guardrail sees current holdings; the kill switch is enforced by the runner.
    plan = apply_guardrails(
        decisions,
        portfolio=PortfolioContext(_held_symbols(session_dir), new_entries_blocked=False),
        config=GuardrailConfig(eligible_symbols=eligible),
    )
    reference_close_by_symbol = {
        symbol: bar.close for symbol, bar in data.get_bars(signal_date).items() if bar is not None
    }
    return run_paper_session(
        session_dir=session_dir, kill_switch_path=kill_switch_path, initial_cash=initial_cash,
        decisions=plan.admitted, session_bars=data.get_bars(execution_date),
        reference_close_by_symbol=reference_close_by_symbol, costs=default_costs(),
        trade_date=execution_date, max_gap_up_pct=max_gap_up_pct,
    )


def _symbols(value: str) -> tuple[str, ...]:
    parsed = tuple(part.strip() for part in value.split(",") if part.strip())
    if not parsed:
        raise argparse.ArgumentTypeError("expected a comma-separated symbol list")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swing_v2.paper.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="print the agent prompt for a signal date")
    for arg in ("--snapshot", "--session-dir", "--kill-switch"):
        render.add_argument(arg, required=True)
    render.add_argument("--signal-date", required=True, type=date.fromisoformat)
    render.add_argument("--symbols", required=True, type=_symbols)

    run = sub.add_parser("run", help="run one paper session from the agent's reply")
    for arg in ("--snapshot", "--session-dir", "--kill-switch", "--reply-file", "--model-id"):
        run.add_argument(arg, required=(arg != "--model-id"))
    run.add_argument("--signal-date", required=True, type=date.fromisoformat)
    run.add_argument("--execution-date", required=True, type=date.fromisoformat)
    run.add_argument("--symbols", required=True, type=_symbols)
    run.add_argument("--eligible", required=True, type=_symbols)
    run.add_argument("--initial-cash", required=True, type=Decimal)

    report = sub.add_parser("report", help="print the cumulative paper report")
    report.add_argument("--session-dir", required=True)

    halt = sub.add_parser("halt", help="engage the kill switch (block new entries)")
    halt.add_argument("--kill-switch", required=True)
    halt.add_argument("--reason", required=True)

    resume = sub.add_parser("resume", help="release the kill switch")
    resume.add_argument("--kill-switch", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "render":
        sys.stdout.write(render_paper_prompt(
            snapshot_path=args.snapshot, signal_date=args.signal_date, symbols=args.symbols,
            session_dir=args.session_dir, kill_switch_path=args.kill_switch,
        ))
        return 0
    if args.command == "run":
        reply = sys.stdin.read() if args.reply_file == "-" else Path(args.reply_file).read_text(encoding="utf-8")
        result = run_paper_day(
            snapshot_path=args.snapshot, signal_date=args.signal_date, execution_date=args.execution_date,
            symbols=args.symbols, agent_reply=reply, eligible=frozenset(args.eligible),
            session_dir=args.session_dir, kill_switch_path=args.kill_switch, initial_cash=args.initial_cash,
        )
        sys.stdout.write(
            f"execution_date={result.trade_date} fills={[f.symbol for f in result.fills]} "
            f"unfilled={[(u.symbol, u.reason) for u in result.unfilled]} "
            f"cash={result.account.cash} nav={result.nav} realized_pnl={result.realized_pnl}\n"
        )
        return 0
    if args.command == "report":
        report = paper_report(args.session_dir)
        sys.stdout.write(
            f"sessions={report.session_count} fills={report.fill_count} "
            f"start_nav={report.starting_nav} end_nav={report.ending_nav} "
            f"total_return={report.total_return} max_drawdown={report.max_drawdown} "
            f"realized_pnl={report.total_realized_pnl} costs={report.total_costs} "
            f"win/loss={report.winning_sessions}/{report.losing_sessions}\n"
        )
        return 0
    if args.command == "halt":
        engage_kill_switch(args.kill_switch, reason=args.reason, engaged_at=datetime.now(_KST))
        sys.stdout.write("kill switch engaged\n")
        return 0
    release_kill_switch(args.kill_switch)
    sys.stdout.write("kill switch released\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
