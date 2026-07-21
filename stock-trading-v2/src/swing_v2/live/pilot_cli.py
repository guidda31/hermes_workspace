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
from .daily_loss import DEFAULT_DAILY_LOSS_LEDGER, realized_loss_for, record_realized_trade
from .decision_order import (
    admitted_buy_decisions,
    latest_record_path,
    load_record,
    sized_quantity,
    snapshot_close,
)
from .gate import LIVE_OPERATOR_CONFIRMATION
from .intent import Side
from .kill_switch import (
    DEFAULT_LIVE_KILL_SWITCH,
    LiveTradingHalted,
    engage_kill_switch,
    read_kill_switch,
    release_kill_switch,
    require_not_halted,
)
from .pilot import (
    PILOT_MAX_ORDER_NOTIONAL,
    PILOT_MAX_POSITIONS,
    build_pilot_order,
    describe_pilot_plan,
    submit_pilot_order,
)
from .pilot_reconcile import reconcile_via_client

_KST = timezone(timedelta(hours=9))


def _kill_switch_path(args) -> str:
    return getattr(args, "kill_switch", None) or os.getenv("KIS_KILL_SWITCH") or DEFAULT_LIVE_KILL_SWITCH


def _daily_loss_ledger(args) -> str:
    return getattr(args, "daily_loss_ledger", None) or os.getenv("KIS_DAILY_LOSS_LEDGER") or DEFAULT_DAILY_LOSS_LEDGER


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
    sub.add_argument("--daily-loss-ledger", default=None,
                     help=f"realized-loss ledger dir (default {DEFAULT_DAILY_LOSS_LEDGER})")
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
    submit.add_argument("--kill-switch", default=None, help=f"halt marker path (default {DEFAULT_LIVE_KILL_SWITCH})")
    halt = sub.add_parser("halt", help="engage the kill switch: block all live submissions")
    halt.add_argument("--reason", required=True)
    halt.add_argument("--kill-switch", default=None)
    resume = sub.add_parser("resume", help="release the kill switch (manual re-enable)")
    resume.add_argument("--kill-switch", default=None)
    loss = sub.add_parser("record-loss", help="record a closing sell's realized P&L into today's ledger")
    loss.add_argument("--symbol", required=True)
    loss.add_argument("--qty", required=True, type=int)
    loss.add_argument("--sell-price", required=True, type=Decimal)
    loss.add_argument("--avg-cost", required=True, type=Decimal)
    loss.add_argument("--day", default=None, help="YYYY-MM-DD; defaults to today (KST)")
    loss.add_argument("--daily-loss-ledger", default=None)
    fromdec = sub.add_parser("from-decision",
                             help="build the order from the AI's forward BUY decision (AI picks the stock)")
    fromdec.add_argument("--record", default=None, help="forward record path (default: latest)")
    fromdec.add_argument("--records-dir", default="data/forward-records")
    fromdec.add_argument("--symbol", default=None, help="choose one when the AI picked several BUYs")
    fromdec.add_argument("--snapshot", default=None, help="price snapshot (default: forward-<signal_date>.json)")
    fromdec.add_argument("--limit-price", type=Decimal, default=None, help="override; else the snapshot close")
    fromdec.add_argument("--equity", type=Decimal, default=None, help="override; else the live balance")
    fromdec.add_argument("--open-positions", type=int, default=0)
    fromdec.add_argument("--max-notional", type=Decimal, default=PILOT_MAX_ORDER_NOTIONAL)
    fromdec.add_argument("--max-positions", type=int, default=PILOT_MAX_POSITIONS)
    fromdec.add_argument("--daily-loss-ledger", default=None)
    fromdec.add_argument("--account-no", default=None)
    fromdec.add_argument("--arm", action="store_true", help="required to actually place the order")
    fromdec.add_argument("--operator-confirm", default="", help=f'must equal "{LIVE_OPERATOR_CONFIRMATION}"')
    fromdec.add_argument("--audit-dir", default="data/live-audit")
    fromdec.add_argument("--kill-switch", default=None)
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

def _run_halt(args) -> int:
    path = _kill_switch_path(args)
    engage_kill_switch(path, reason=args.reason, engaged_at=datetime.now(_KST))
    sys.stdout.write(f"kill switch ENGAGED at {path} — live submissions are now blocked.\n")
    return 0


def _run_resume(args) -> int:
    path = _kill_switch_path(args)
    release_kill_switch(path)
    sys.stdout.write(f"kill switch released at {path} — live submissions allowed again.\n")
    return 0


def _run_record_loss(args) -> int:
    ledger = _daily_loss_ledger(args)
    day = args.day or datetime.now(_KST).date().isoformat()
    realized = record_realized_trade(
        ledger, day=day, symbol=args.symbol, quantity=args.qty,
        sell_price=args.sell_price, avg_cost=args.avg_cost, recorded_at=datetime.now(_KST),
    )
    total_loss = realized_loss_for(ledger, day)
    sys.stdout.write(
        f"recorded {args.symbol} qty={args.qty}: realized P&L {realized} "
        f"({'LOSS' if realized < 0 else 'gain'}); today's cumulative loss = {total_loss}\n"
    )
    return 0


def _run_order_command(args, account_no: str) -> int:
    marker = read_kill_switch(_kill_switch_path(args))
    if marker is not None:
        sys.stdout.write(f"kill switch: ENGAGED ({marker.get('reason')}) — submit will be blocked\n")
    signal_day = args.signal_date or datetime.now(_KST).date()
    try:
        daily_loss = realized_loss_for(_daily_loss_ledger(args), signal_day.isoformat())
    except ValueError as exc:  # corrupt ledger -> fail closed, block trading
        sys.stderr.write(f"REFUSING (nothing sent): {exc}\n")
        return 2
    if daily_loss > 0:
        sys.stdout.write(f"today's realized loss: {daily_loss} (feeds the daily-loss circuit breaker)\n")
    state = _resolve_state(args, account_no)
    try:
        plan = build_pilot_order(
            symbol=args.symbol, side=Side(args.side), quantity=args.qty, limit_price=args.limit_price,
            signal_date=signal_day, daily_loss=daily_loss,
            equity=state.equity, open_positions=state.open_positions,
            max_order_notional=args.max_notional, max_positions=args.max_positions,
        )
    except ValueError as exc:
        sys.stderr.write(f"pretrade REJECTED (nothing sent): {exc}\n")
        return 2
    return _present_and_submit(args, plan, account_no, submit_requested=(args.command == "submit"))


def _present_and_submit(args, plan, account_no: str, *, submit_requested: bool) -> int:
    """Print the plan; if a submit is requested, run the gates then place + reconcile."""
    sys.stdout.write(describe_pilot_plan(plan, account_number=account_no))
    if not submit_requested:
        return 0

    # independent gates before any network call
    kill_switch = _kill_switch_path(args)
    try:
        require_not_halted(kill_switch)  # fail-closed: engaged/corrupt switch blocks everything
    except LiveTradingHalted as exc:
        sys.stderr.write(f"\nREFUSING to submit: {exc}\n(release with `resume --kill-switch {kill_switch}`)\n")
        return 2
    if not getattr(args, "arm", False):
        sys.stderr.write("\nREFUSING to submit: pass --arm to place this real order.\n")
        return 2
    if getattr(args, "operator_confirm", "") != LIVE_OPERATOR_CONFIRMATION:
        sys.stderr.write(f'\nREFUSING to submit: --operator-confirm must equal "{LIVE_OPERATOR_CONFIRMATION}".\n')
        return 2

    import requests
    from .audit import IntentAuditWriter
    from .production_execution import KisProductionTradingClient

    credentials = _credentials()
    token = _access_token(credentials)
    client = KisProductionTradingClient(
        credentials=credentials, access_token=token, account_number=account_no,
        session=requests.Session(), audit_writer=IntentAuditWriter(getattr(args, "audit_dir", "data/live-audit")),
    )
    sys.stdout.write("\n*** ARMED — placing a REAL order on a REAL account ***\n")
    ack = submit_pilot_order(plan, client=client, operator_confirmation=args.operator_confirm,
                             kill_switch_path=kill_switch)
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


def _run_from_decision(args, account_no: str) -> int:
    """Build the order from the AI's forward-observation BUY decision (AI picks the stock)."""
    records_dir = args.records_dir
    record_path = args.record or latest_record_path(records_dir)
    if record_path is None:
        sys.stderr.write(f"no forward records in {records_dir}; run the AI decision first\n")
        return 2
    record = load_record(record_path)
    signal_day = date.fromisoformat(record["signal_date"])
    buys = admitted_buy_decisions(record)
    if not buys:
        sys.stdout.write(f"AI made no admitted BUY on {record['signal_date']} (e.g. all HOLD) — nothing to order.\n")
        return 0

    symbols = [d["symbol"] for d in buys]
    if args.symbol is not None:
        chosen = next((d for d in buys if d["symbol"] == args.symbol), None)
        if chosen is None:
            sys.stderr.write(f"--symbol {args.symbol} is not an AI BUY pick; choose from {symbols}\n")
            return 2
    elif len(buys) == 1:
        chosen = buys[0]
    else:
        sys.stderr.write(f"AI picked several BUYs {symbols}; pass --symbol to choose one\n")
        return 2

    snapshot = args.snapshot or f"data/snapshots/forward-{record['signal_date']}.json"
    try:
        limit_price = args.limit_price if args.limit_price is not None else snapshot_close(snapshot, chosen["symbol"])
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"could not resolve a limit price ({exc}); pass --limit-price\n")
        return 2

    try:
        daily_loss = realized_loss_for(_daily_loss_ledger(args), signal_day.isoformat())
    except ValueError as exc:
        sys.stderr.write(f"REFUSING (nothing sent): {exc}\n")
        return 2
    state = _resolve_state(args, account_no)

    quantity, clamped = sized_quantity(
        target_weight=Decimal(str(chosen["target_weight"])), equity=state.equity,
        limit_price=limit_price, max_order_notional=args.max_notional)
    sys.stdout.write(
        f"AI pick from {Path(record_path).name}: BUY {chosen['symbol']} "
        f"target_weight={chosen['target_weight']} conviction={chosen.get('conviction')}\n"
        f"sized qty={quantity} @ {limit_price}{' (CLAMPED to pilot cap)' if clamped else ''}\n"
    )
    if quantity < 1:
        sys.stderr.write("sized quantity is below one share at this price/cap; nothing to order\n")
        return 2

    try:
        plan = build_pilot_order(
            symbol=chosen["symbol"], side=Side.BUY, quantity=quantity, limit_price=limit_price,
            signal_date=signal_day, daily_loss=daily_loss, equity=state.equity,
            open_positions=state.open_positions, max_order_notional=args.max_notional,
            max_positions=args.max_positions,
        )
    except ValueError as exc:
        sys.stderr.write(f"pretrade REJECTED (nothing sent): {exc}\n")
        return 2
    return _present_and_submit(args, plan, account_no, submit_requested=bool(args.arm))


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

    # kill-switch commands need no account
    if args.command == "halt":
        return _run_halt(args)
    if args.command == "resume":
        return _run_resume(args)
    if args.command == "record-loss":
        return _run_record_loss(args)

    account_no = _normalize_account(args.account_no or os.getenv("KIS_ACCOUNT_NO", ""))
    if not account_no:
        sys.stderr.write("no account: pass --account-no or set KIS_ACCOUNT_NO in .env\n")
        return 2

    if args.command == "reconcile":
        return _run_reconcile(args, account_no)
    if args.command == "from-decision":
        return _run_from_decision(args, account_no)
    return _run_order_command(args, account_no)


if __name__ == "__main__":
    raise SystemExit(main())
