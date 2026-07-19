"""Autonomous two-step forward-observation glue a Hermes routine can drive.

Step 1 ``render``: build the point-in-time brief from a local snapshot and print the
prompt for the agent to reason over.
Step 2 ``record``: given the agent's JSON reply, rebuild the *identical* brief
(deterministic from snapshot+date), validate/guardrail the decisions, and durably
write the immutable signal audit.

This module calls no LLM and submits no order. The agent's reasoning happens between
the two steps, in the Hermes runtime — not here. Disclosure/news providers are not
wired in v0 (price-only), leaving hooks for DART/news later.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Optional, Sequence

from ..backtest_data import SnapshotBacktestData, load_snapshot
from .forward_eval import ForwardObservationReport, evaluate_forward_observations
from .forward_runner import run_forward_signal
from .guardrail import GuardrailConfig, PortfolioContext
from .prompt import parse_agent_response, render_brief_prompt
from .providers import dart_disclosure_provider_or_none
from .signal_audit import load_signal_audit
from .brief import build_brief

_KST = timezone(timedelta(hours=9))
_DEFAULT_WINDOW = 120
_DEFAULT_FORWARD_SESSIONS = 5


def _load_data(snapshot_path: str | Path) -> SnapshotBacktestData:
    return SnapshotBacktestData(load_snapshot(snapshot_path))


def render_from_snapshot(
    snapshot_path: str | Path,
    signal_date: date,
    symbols: Sequence[str],
    *,
    held: frozenset[str] = frozenset(),
    new_entries_blocked: bool = False,
    window: int = _DEFAULT_WINDOW,
    disclosure_provider=None,
    news_provider=None,
) -> str:
    """Build the brief and render the agent prompt.

    When a ``disclosure_provider`` (e.g. DART) is supplied, point-in-time disclosures
    appear in the brief so the agent can reason over them, not just price. With no
    provider the brief stays price-only.
    """
    brief = build_brief(
        _load_data(snapshot_path), signal_date=signal_date, symbols=symbols, window=window,
        disclosure_provider=disclosure_provider, news_provider=news_provider,
    )
    return render_brief_prompt(brief, portfolio=PortfolioContext(held, new_entries_blocked))


def record_from_snapshot(
    snapshot_path: str | Path,
    signal_date: date,
    symbols: Sequence[str],
    agent_reply: str,
    *,
    eligible: frozenset[str],
    model_id: str,
    decided_at: datetime,
    held: frozenset[str] = frozenset(),
    new_entries_blocked: bool = False,
    output_path: Optional[str | Path] = None,
    window: int = _DEFAULT_WINDOW,
    disclosure_provider=None,
    news_provider=None,
) -> dict:
    """Rebuild the identical brief, apply the agent reply, and record the signal.

    The same providers used at ``render`` time must be passed here so the recorded
    brief (and its evidence ids) is identical to what the agent saw.
    """
    if type(agent_reply) is not str or not agent_reply.strip():
        raise ValueError("agent_reply must be a nonempty str")
    return run_forward_signal(
        _load_data(snapshot_path),
        signal_date=signal_date,
        symbols=symbols,
        guardrail_config=GuardrailConfig(eligible_symbols=eligible),
        portfolio=PortfolioContext(held, new_entries_blocked),
        model_id=model_id,
        decided_at=decided_at,
        decide=lambda brief: parse_agent_response(agent_reply),
        window=window,
        output_path=output_path,
        disclosure_provider=disclosure_provider,
        news_provider=news_provider,
    )


def score_accumulated_observations(
    *,
    records_dir: str | Path,
    snapshot_path: str | Path,
    forward_sessions: int = _DEFAULT_FORWARD_SESSIONS,
) -> ForwardObservationReport:
    """Score every accumulated signal audit against realized outcomes in the snapshot.

    Reads the integrity-verified audit records from ``records_dir`` and evaluates their
    admitted picks over the snapshot's realized bars. Records whose forward window has
    not elapsed in the snapshot are skipped, so this re-runs cleanly as data accrues.
    """
    snapshot = load_snapshot(snapshot_path)
    data = SnapshotBacktestData(snapshot)
    paths = sorted(Path(records_dir).glob("*.json"))
    if not paths:
        raise ValueError(f"no signal-audit records found in {records_dir}")
    records = [load_signal_audit(path) for path in paths]

    def bar_lookup(symbol, day):
        if symbol == snapshot.market_symbol:
            return data.get_market_index_bar(day)
        return data.get_bars(day).get(symbol)

    return evaluate_forward_observations(
        records, bar_lookup=bar_lookup, calendar=list(snapshot.trade_calendar),
        market_symbol=snapshot.market_symbol, forward_sessions=forward_sessions,
    )


def _symbols(value: str) -> tuple[str, ...]:
    parsed = tuple(part.strip() for part in value.split(",") if part.strip())
    if not parsed:
        raise argparse.ArgumentTypeError("expected a comma-separated symbol list")
    return parsed


def _frozen_symbols(value: str) -> frozenset[str]:
    return frozenset(_symbols(value)) if value.strip() else frozenset()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swing_v2.llm.forward_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--snapshot", required=True)
    common.add_argument("--signal-date", required=True, type=date.fromisoformat)
    common.add_argument("--symbols", required=True, type=_symbols)
    common.add_argument("--held", default="", type=_frozen_symbols)
    common.add_argument("--new-entries-blocked", action="store_true")
    common.add_argument("--window", default=_DEFAULT_WINDOW, type=int)
    common.add_argument("--corp-code-cache", default="data/dart-corp-codes.json",
                        help="local {symbol: corp_code} cache for DART disclosures")

    render = sub.add_parser("render", parents=[common], help="print the agent prompt")

    record = sub.add_parser("record", parents=[common], help="record the agent reply as a signal audit")
    record.add_argument("--reply-file", required=True, help="file with the agent's JSON reply ('-' for stdin)")
    record.add_argument("--eligible", required=True, type=_frozen_symbols)
    record.add_argument("--model-id", required=True)
    record.add_argument("--output", default=None)

    score = sub.add_parser("score", help="score accumulated signal audits vs realized outcomes")
    score.add_argument("--records-dir", required=True)
    score.add_argument("--snapshot", required=True)
    score.add_argument("--forward-sessions", type=int, default=_DEFAULT_FORWARD_SESSIONS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "render":
        provider = dart_disclosure_provider_or_none(symbols=args.symbols, cache_path=args.corp_code_cache)
        sys.stdout.write(render_from_snapshot(
            args.snapshot, args.signal_date, args.symbols,
            held=args.held, new_entries_blocked=args.new_entries_blocked, window=args.window,
            disclosure_provider=provider,
        ))
        return 0
    if args.command == "score":
        report = score_accumulated_observations(
            records_dir=args.records_dir, snapshot_path=args.snapshot,
            forward_sessions=args.forward_sessions,
        )
        sys.stdout.write(
            f"scored={report.scored_count}/{report.signal_count} hit_rate={report.hit_rate} "
            f"pick_return={report.mean_pick_return} market_return={report.mean_market_return} "
            f"edge={report.edge}\n"
        )
        return 0
    reply = sys.stdin.read() if args.reply_file == "-" else Path(args.reply_file).read_text(encoding="utf-8")
    provider = dart_disclosure_provider_or_none(symbols=args.symbols, cache_path=args.corp_code_cache)
    record = record_from_snapshot(
        args.snapshot, args.signal_date, args.symbols, reply,
        eligible=args.eligible, model_id=args.model_id,
        decided_at=datetime.now(_KST), held=args.held,
        new_entries_blocked=args.new_entries_blocked, output_path=args.output, window=args.window,
        disclosure_provider=provider,
    )
    sys.stdout.write(
        f"signal_date={record['signal_date']} admitted={record['admitted_symbols']} "
        f"rejected={[r['symbol'] for r in record['rejected']]}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
