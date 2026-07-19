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
from .forward_runner import run_forward_signal
from .guardrail import GuardrailConfig, PortfolioContext
from .prompt import parse_agent_response, render_brief_prompt
from .brief import build_brief

_KST = timezone(timedelta(hours=9))
_DEFAULT_WINDOW = 120


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
) -> str:
    """Build the brief and render the agent prompt (price-only in v0)."""
    brief = build_brief(_load_data(snapshot_path), signal_date=signal_date, symbols=symbols, window=window)
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
) -> dict:
    """Rebuild the identical brief, apply the agent reply, and record the signal."""
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

    render = sub.add_parser("render", parents=[common], help="print the agent prompt")

    record = sub.add_parser("record", parents=[common], help="record the agent reply as a signal audit")
    record.add_argument("--reply-file", required=True, help="file with the agent's JSON reply ('-' for stdin)")
    record.add_argument("--eligible", required=True, type=_frozen_symbols)
    record.add_argument("--model-id", required=True)
    record.add_argument("--output", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "render":
        sys.stdout.write(render_from_snapshot(
            args.snapshot, args.signal_date, args.symbols,
            held=args.held, new_entries_blocked=args.new_entries_blocked, window=args.window,
        ))
        return 0
    reply = sys.stdin.read() if args.reply_file == "-" else Path(args.reply_file).read_text(encoding="utf-8")
    record = record_from_snapshot(
        args.snapshot, args.signal_date, args.symbols, reply,
        eligible=args.eligible, model_id=args.model_id,
        decided_at=datetime.now(_KST), held=args.held,
        new_entries_blocked=args.new_entries_blocked, output_path=args.output, window=args.window,
    )
    sys.stdout.write(
        f"signal_date={record['signal_date']} admitted={record['admitted_symbols']} "
        f"rejected={[r['symbol'] for r in record['rejected']]}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
