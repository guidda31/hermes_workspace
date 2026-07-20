"""CLI for the small live KRX pilot.

Subcommands over the same validated plan:
  preflight  — build + validate + print the EXACT order that would be sent, submitting
               nothing. By default it reads the LIVE account (read-only) to source real
               equity + open-position count; pass --equity to stay fully offline.
  submit     — actually place the order via the existing gated production submitter, then
               reconcile the fill. Refuses unless BOTH --arm AND the exact --operator-confirm
               phrase are given.
  reconcile  — read-only: did a given order fill? (open-orders + daily fills)

Usage (from stock-trading-v2/):
  # preflight against the live account (auto equity + open positions):
  PYTHONPATH=src .venv/bin/python -m swing_v2.live.pilot_cli preflight \
      --symbol 086790 --side BUY --qty 1 --limit-price 50000

  # only after reviewing the preflight and deciding to risk real money:
  PYTHONPATH=src .venv/bin/python -m swing_v2.live.pilot_cli submit \
      --symbol 086790 --side BUY --qty 1 --limit-price 50000 \
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

from .account_state import AccountState, read_account_state
from .gate import LIVE_OPERATOR_CONFIRMATION
from .intent import Side
from .pilot import (
    PILOT_MAX_ORDER_NOTIONAL,
    PILOT_MAX_POSITIONS,
    build_pilot_order,
    describe_pilot_plan,
    submit_pilot_order,
)
from .pilot_reconcile import reconcile_via_client

_KST = timezone(timedelta(hours=9))


def _normalize_account(raw: str) -> str:
    """Accept CANO-ACNT_PRDT_CD, or 10 bare digits, and return the dashed form."""
    account = raw.strip()
    if "-" not in account and len(account) == 10 and account.isdigit():
        return f"{account[:8]}-{account[8:]}"
    return account


def _add_order_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--symbol", required=True)
    sub.add_argument("--side", required=True, choices=[s.value for s in Side])
    sub.add_argument("--qty", required=True, type=int)
    sub.add_argument("--limit-price", required=True, type=Decimal)
    sub.add_argument("--equity", type=Decimal, default=None,
                     help="override account equity (skips the live read; stays offline)")
    sub.add_argument("--open-positions", type=int, default=0,
                     help="used only with --equity; live read ignores this")
    sub.add_argument("--as-of", dest="signal_date", type=date.fromisoformat, default=None)
    sub.add_argument("--max-notional", type=Decimal, default=PILOT_MAX_ORDER_NOTIONAL,
                     help=f"pilot per-order cap (default {PILOT_MAX_ORDER_NOTIONAL})")
    sub.add_argument("--max-positions", type=int, default=PILOT_MAX_POSITIONS,
                     help=f"max concurrent positions (default {PILOT_MAX_POSITIONS}); raise to trade alongside existing holdings")
    sub.add_argument("--account-no", default=None, help="defaults to KIS_ACCOUNT_NO in .env")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swing_v2.live.pilot_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    _add_order_args(sub.add_parser("preflight", help="build + print the order; submit nothing"))
    submit = sub.add_parser("submit", help="place the order via the gated production submitter")
    _add_order_args(submit)
    submit.add_argument("--arm", action="store_true", help="required to actually place the order")
    submit.add_argument("--operator-confirm", default="", help=f'must equal "{LIVE_OPERATOR_CONFIRMATION}"')
    submit.add_argument("--audit-dir", default="data/live-audit")
    recon = sub.add_parser("reconcile", help="read-only: did this order fill?")
    recon.add_argument("--symbol", required=True)
    recon.add_argument("--order-number", required=True)
    recon.add_argument("--date", default=None, help="YYYYMMDD; defaults to today (KST)")
    recon.add_argument("--account-no", default=None)
    return parser


# --- shared credential / transport helpers -----------------------------------------

def _credentials():
    from swing_v2.kis import KisCredentials
    app_key, app_secret = os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        raise SystemExit("KIS_APP_KEY / KIS_APP_SECRET must be set in .env")
    return KisCredentials(app_key=app_key, app_secret=app_secret)


def _access_token(credentials):
    from swing_v2.kis import KisClient
    cache = os.getenv("KIS_TOKEN_CACHE")
    return KisClient(credentials=credentials).get_access_token(cache_path=Path(cache) if cache else None)


def _resolve_state(args, account_no: str) -> AccountState:
    """Real equity + open-positions from the live account, unless --equity is given."""
    if args.equity is not None:
        return AccountState(equity=args.equity, cash=Decimal("0"),
                            open_positions=args.open_positions, holdings=())
    from swing_v2.kis import KisClient
    credentials = _credentials()
    token = _access_token(credentials)
    state = read_account_state(KisClient(credentials=credentials), token, account_no)
    sys.stdout.write(
        f"account (live): equity={state.equity} open_positions={state.open_positions} "
        f"holdings={[h.symbol for h in state.holdings]}\n"
    )
    return state


# --- subcommands --------------------------------------------------------------------

def _run_order_command(args, account_no: str) -> int:
    state = _resolve_state(args, account_no)
    try:
        plan = build_pilot_order(
            symbol=args.symbol, side=Side(args.side), quantity=args.qty, limit_price=args.limit_price,
            signal_date=args.signal_date or datetime.now(_KST).date(),
            equity=state.equity, open_positions=state.open_positions,
            max_order_notional=args.max_notional, max_positions=args.max_positions,
        )
    except ValueError as exc:
        sys.stderr.write(f"pretrade REJECTED (nothing sent): {exc}\n")
        return 2
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
    from .audit import IntentAuditWriter
    from .production_execution import KisProductionTradingClient

    credentials = _credentials()
    token = _access_token(credentials)
    client = KisProductionTradingClient(
        credentials=credentials, access_token=token, account_number=account_no,
        session=requests.Session(), audit_writer=IntentAuditWriter(args.audit_dir),
    )
    sys.stdout.write("\n*** ARMED — placing a REAL order on a REAL account ***\n")
    ack = submit_pilot_order(plan, client=client, operator_confirmation=args.operator_confirm)
    sys.stdout.write(f"ACCEPTED by KIS: org={ack.forwarding_order_organization} order_no={ack.order_number}\n")

    # best-effort fill reconciliation (fills can lag; a miss here is not an order failure)
    try:
        today = datetime.now(_KST).strftime("%Y%m%d")
        recon = _reconcile(credentials, token, account_no,
                           order_number=ack.order_number, symbol=plan.intent.symbol, day=today)
        sys.stdout.write(f"reconcile: {recon.status} filled={recon.filled_quantity} open={recon.open_quantity}\n")
    except Exception as exc:  # noqa: BLE001 - reconciliation is advisory, never mask the accepted order
        sys.stdout.write(f"reconcile: unavailable ({type(exc).__name__}); check the account manually\n")
    return 0


def _reconcile(credentials, token, account_no, *, order_number, symbol, day):
    import requests
    from .production_reconciliation import KisProductionReconciliationClient
    client = KisProductionReconciliationClient(
        credentials=credentials, access_token=token, account_number=account_no, session=requests.Session())
    return reconcile_via_client(client, order_number=order_number, symbol=symbol, start=day, end=day)


def _run_reconcile(args, account_no: str) -> int:
    credentials = _credentials()
    token = _access_token(credentials)
    day = args.date or datetime.now(_KST).strftime("%Y%m%d")
    recon = _reconcile(credentials, token, account_no,
                       order_number=args.order_number, symbol=args.symbol, day=day)
    sys.stdout.write(
        f"order {recon.order_number} ({recon.symbol}): {recon.status} "
        f"filled={recon.filled_quantity} open={recon.open_quantity}\n"
    )
    return 0


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

    if args.command == "reconcile":
        return _run_reconcile(args, account_no)
    return _run_order_command(args, account_no)


if __name__ == "__main__":
    raise SystemExit(main())
