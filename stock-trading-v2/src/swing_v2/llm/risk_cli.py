"""Daily risk-watch CLI: scan held/watched symbols for material disclosure/news risk.

Defensive, not alpha. Pulls recent DART disclosures (and Naver news when configured)
for each symbol, screens titles for material negatives, and prints a severity-ranked
alert digest — "did anything bad happen to my names today". No orders, no predictions.

Usage (from stock-trading-v2/, after the KRX close):
    PYTHONPATH=src .venv/bin/python -m swing_v2.llm.risk_cli watch --symbols 005930,000660
    # or default to the saved universe:
    PYTHONPATH=src .venv/bin/python -m swing_v2.llm.risk_cli watch
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Optional

from .providers import dart_disclosure_provider_or_none, news_provider_or_none
from .risk_screen import RiskFlag, Severity, screen_disclosures

_KST = timezone(timedelta(hours=9))
_DEFAULT_DAYS = 14


def watch_risks(
    *,
    symbols: Sequence[str],
    disclosure_provider,
    news_provider,
    as_of: date,
) -> list[RiskFlag]:
    """Screen each symbol's recent disclosures + news for material risk, HIGH first."""
    if type(as_of) is not date:
        raise ValueError("as_of must be a plain date")
    flags: list[RiskFlag] = []
    for symbol in symbols:
        items = list(disclosure_provider(symbol, as_of)) if disclosure_provider is not None else []
        if news_provider is not None:
            items += list(news_provider(symbol, as_of))
        flags.extend(screen_disclosures(symbol, items))
    flags.sort(key=lambda f: (0 if f.severity is Severity.HIGH else 1, f.symbol))
    return flags


def format_digest(flags: Sequence[RiskFlag], *, names: dict, as_of: date) -> str:
    """Render a severity-ranked, human-readable alert digest."""
    lines = [f"Risk watch — {as_of.isoformat()} ({len(flags)} flags)"]
    if not flags:
        lines.append("  no material risk flags (clear)")
        return "\n".join(lines) + "\n"
    for severity in (Severity.HIGH, Severity.MEDIUM):
        group = [f for f in flags if f.severity is severity]
        if not group:
            continue
        lines.append(f"\n[{severity.value}] {len(group)}")
        for f in group:
            name = names.get(f.symbol, "")
            lines.append(f"  {f.symbol} {name:8s} [{f.category}] {f.disclosure_title[:48]}  ({f.evidence_id})")
    return "\n".join(lines) + "\n"


def _load_json(path, default):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swing_v2.llm.risk_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    watch = sub.add_parser("watch", help="print today's risk digest for held/watched symbols")
    watch.add_argument("--symbols", default=None, help="comma-separated; defaults to the saved universe")
    watch.add_argument("--universe", default="data/universe-symbols.json")
    watch.add_argument("--names", default="data/universe-names.json")
    watch.add_argument("--as-of", default=None, type=date.fromisoformat, help="defaults to today (KST)")
    watch.add_argument("--days", type=int, default=_DEFAULT_DAYS)
    watch.add_argument("--corp-code-cache", default="data/dart-corp-codes.json")
    watch.add_argument("--news-name-cache", default="data/universe-names.json")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    args = build_parser().parse_args(argv)
    symbols = (
        tuple(s.strip() for s in args.symbols.split(",") if s.strip())
        if args.symbols else tuple(_load_json(args.universe, []))
    )
    if not symbols:
        sys.stderr.write("no symbols (pass --symbols or provide data/universe-symbols.json)\n")
        return 2
    as_of = args.as_of or datetime.now(_KST).date()
    names = _load_json(args.names, {})
    disclosure_provider = dart_disclosure_provider_or_none(
        symbols=symbols, cache_path=args.corp_code_cache, window_days=args.days)
    news_provider = news_provider_or_none(
        symbols=symbols, name_cache_path=args.news_name_cache, window_days=args.days)
    if disclosure_provider is None and news_provider is None:
        sys.stderr.write("no data source: set OPENDART_API_KEY (and/or NAVER keys) in .env\n")
        return 2
    flags = watch_risks(symbols=symbols, disclosure_provider=disclosure_provider,
                        news_provider=news_provider, as_of=as_of)
    sys.stdout.write(format_digest(flags, names=names, as_of=as_of))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
