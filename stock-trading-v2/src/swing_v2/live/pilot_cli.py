"""CLI for the small live KRX pilot.

Two subcommands over the same validated plan:
  preflight  — build + validate + print the EXACT order that would be sent. Offline,
               places nothing. This is the default you run first, every time.
  submit     — actually place the order via the existing gated production submitter.
               Refuses unless you pass BOTH --arm AND the exact --operator-confirm phrase;
               loads .env for real KIS credentials and issues a real token.

Usage (from stock-trading-v2/):
  PYTHONPATH=src .venv/bin/python -m swing_v2.live.pilot_cli preflight \
      --symbol 086790 --side BUY --qty 1 --limit-price 50000 --equity 10000000

  # only after reviewing the preflight and deciding to risk real money:
  PYTHONPATH=src .venv/bin/python -m swing_v2.live.pilot_cli submit \
      --symbol 086790 --side BUY --qty 1 --limit-price 50000 --equity 10000000 \
      --arm --operator-confirm KIS_LIVE_TRADING_OPERATOR_CONFIRMED
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional, Sequence

from .gate import LIVE_OPERATOR_CONFIRMATION
from .intent import Side
from .pilot import PILOT_MAX_ORDER_NOTIONAL, build_pilot_order, describe_pilot_plan, submit_pilot_order

_KST = timezone(timedelta(hours=9))


def _normalize_account(raw: str) -> str:
    """Accept CANO-ACNT_PRDT_CD, or 10 bare digits, and return the dashed form."""
    account = raw.strip()
    if "-" not in account and len(account) == 10 and account.isdigit():
        return f"{account[:8]}-{account[8:]}"
    return account


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--symbol", required=True)
    sub.add_argument("--side", required=True, choices=[s.value for s in Side])
    sub.add_argument("--qty", required=True, type=int)
    sub.add_argument("--limit-price", required=True, type=Decimal)
    sub.add_argument("--equity", required=True, type=Decimal, help="account equity for risk sizing")
    sub.add_argument("--open-positions", type=int, default=0, help="positions already held (pilot allows <1)")
    sub.add_argument("--as-of", dest="signal_date", type=date.fromisoformat, default=None)
    sub.add_argument("--max-notional", type=Decimal, default=PILOT_MAX_ORDER_NOTIONAL,
                     help=f"pilot per-order cap (default {PILOT_MAX_ORDER_NOTIONAL})")
    sub.add_argument("--account-no", default=None, help="defaults to KIS_ACCOUNT_NO in .env")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swing_v2.live.pilot_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    _add_common(sub.add_parser("preflight", help="build + print the order; submit nothing (offline)"))
    submit = sub.add_parser("submit", help="place the order via the gated production submitter")
    _add_common(submit)
    submit.add_argument("--arm", action="store_true", help="required to actually place the order")
    submit.add_argument("--operator-confirm", default="", help=f'must equal "{LIVE_OPERATOR_CONFIRMATION}"')
    submit.add_argument("--audit-dir", default="data/live-audit")
    return parser


def _plan(args):
    return build_pilot_order(
        symbol=args.symbol, side=Side(args.side), quantity=args.qty, limit_price=args.limit_price,
        signal_date=args.signal_date or datetime.now(_KST).date(),
        equity=args.equity, open_positions=args.open_positions, max_order_notional=args.max_notional,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except ImportError:
        pass
    args = build_parser().parse_args(argv)
    account_no = _normalize_account(args.account_no or os.getenv("KIS_ACCOUNT_NO", ""))
    if not account_no:
        sys.stderr.write("no account: pass --account-no or set KIS_ACCOUNT_NO in .env\n")
        return 2

    plan = _plan(args)  # builds + validates; raises on any breach
    sys.stdout.write(describe_pilot_plan(plan, account_number=account_no))

    if args.command == "preflight":
        return 0

    # submit path — two independent human gates before any network call
    if not args.arm:
        sys.stderr.write("\nREFUSING to submit: pass --arm to place this real order.\n")
        return 2
    if args.operator_confirm != LIVE_OPERATOR_CONFIRMATION:
        sys.stderr.write(f'\nREFUSING to submit: --operator-confirm must equal "{LIVE_OPERATOR_CONFIRMATION}".\n')
        return 2

    import requests
    from swing_v2.kis import KisClient, KisCredentials
    from .audit import IntentAuditWriter
    from .production_execution import KisProductionTradingClient

    app_key, app_secret = os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        sys.stderr.write("KIS_APP_KEY / KIS_APP_SECRET must be set in .env\n")
        return 2
    credentials = KisCredentials(app_key=app_key, app_secret=app_secret)
    token_cache = os.getenv("KIS_TOKEN_CACHE")
    access_token = KisClient(credentials=credentials).get_access_token(
        cache_path=Path(token_cache) if token_cache else None)

    client = KisProductionTradingClient(
        credentials=credentials, access_token=access_token, account_number=account_no,
        session=requests.Session(), audit_writer=IntentAuditWriter(args.audit_dir),
    )
    sys.stdout.write("\n*** ARMED — placing a REAL order on a REAL account ***\n")
    ack = submit_pilot_order(plan, client=client, operator_confirmation=args.operator_confirm)
    sys.stdout.write(f"ACCEPTED by KIS: org={ack.forwarding_order_organization} order_no={ack.order_number}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
