"""Read-only KIS balance reader: raw payload -> typed account state + pretrade snapshot.

This module NEVER places, amends, or cancels an order; it only parses a KIS
domestic ``inquire-balance`` payload into typed, exact-validated values and builds
a local pretrade risk snapshot for the independent risk gate. It exists so the live
pilot can derive equity and open-position count from the account itself instead of
trusting operator-supplied ``--equity`` / ``--open-positions`` flags.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .risk import AccountRiskSnapshot


def _decimal_field(value: object, field_name: str) -> Decimal:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    return parsed


def _int_field(value: object, field_name: str) -> int:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be an integer string")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer string") from exc
    return parsed


@dataclass(frozen=True)
class HeldPosition:
    symbol: str
    quantity: int
    avg_price: Decimal


@dataclass(frozen=True)
class AccountState:
    equity: Decimal
    cash: Decimal
    open_positions: int
    holdings: tuple[HeldPosition, ...]


def parse_account_state(balance_payload: Mapping) -> AccountState:
    """Parse a raw KIS domestic inquire-balance payload into a typed AccountState.

    Pure and offline: performs no network access. Fails closed with ``ValueError``
    on a non-mapping payload, missing/empty ``output1``/``output2``, a summary that
    is not a mapping, malformed numeric strings, or a non-positive equity.

    ``equity`` is taken from ``output2[0]["nass_amt"]`` (순자산금액, the account net
    asset value used for risk sizing). ``cash`` is ``output2[0]["dnca_tot_amt"]``.
    Holdings with a non-positive ``hldg_qty`` are skipped.
    """
    if not isinstance(balance_payload, Mapping):
        raise ValueError("balance payload must be a mapping")

    output2 = balance_payload.get("output2")
    if not isinstance(output2, list) or not output2:
        raise ValueError("balance payload output2 must be a nonempty list")
    summary = output2[0]
    if not isinstance(summary, Mapping):
        raise ValueError("balance payload output2[0] must be a mapping")

    output1 = balance_payload.get("output1")
    if not isinstance(output1, list):
        raise ValueError("balance payload output1 must be a list")
    # An empty output1 is valid: an account with no holdings has open_positions=0.

    equity = _decimal_field(summary.get("nass_amt"), "nass_amt")
    if equity <= 0:
        raise ValueError("nass_amt (equity) must be a positive finite Decimal")
    cash = _decimal_field(summary.get("dnca_tot_amt"), "dnca_tot_amt")

    holdings: list[HeldPosition] = []
    for row in output1:
        if not isinstance(row, Mapping):
            raise ValueError("balance payload output1 row must be a mapping")
        quantity = _int_field(row.get("hldg_qty"), "hldg_qty")
        if quantity <= 0:
            continue
        symbol = row.get("pdno")
        if type(symbol) is not str or not symbol.strip():
            raise ValueError("pdno must be a nonempty plain str")
        avg_price = _decimal_field(row.get("pchs_avg_pric"), "pchs_avg_pric")
        holdings.append(HeldPosition(symbol=symbol, quantity=quantity, avg_price=avg_price))

    return AccountState(
        equity=equity,
        cash=cash,
        open_positions=len(holdings),
        holdings=tuple(holdings),
    )


def read_account_state(client, access_token: str, account_number: str) -> AccountState:
    """Fetch the read-only balance via ``client.inquire_balance`` and parse it.

    Thin network wrapper around :func:`parse_account_state`. It only reads balance
    data and never submits, amends, or cancels an order.
    """
    payload = client.inquire_balance(access_token, account_number)
    return parse_account_state(payload)


def build_pretrade_snapshot(
    state: AccountState,
    *,
    proposed_position_risk: Decimal,
    daily_loss: Decimal = Decimal("0"),
) -> AccountRiskSnapshot:
    """Build an AccountRiskSnapshot from a parsed AccountState for the risk gate.

    ``planned_or_open_positions`` is the account's CURRENT open-position count
    (``state.open_positions``), matching the convention used elsewhere (order_bridge)
    and the ``planned_or_open_positions >= max_positions`` check in
    :func:`~swing_v2.live.risk.validate_pretrade`: that check already rejects a new
    order once you are at the cap, so the count must NOT be pre-incremented for the
    order being placed (doing so would be off-by-one and reject a first buy at max=1).

    Limitation: the KIS balance endpoint does not expose intraday realized loss, so
    ``daily_loss`` cannot be derived from balance data. It defaults to ``Decimal("0")``,
    which means the daily-loss guard is effectively not enforced from balance alone.
    Callers that track realized daily loss elsewhere must pass it explicitly.
    """
    if type(state) is not AccountState:
        raise ValueError("state must be an exact AccountState")
    for value, name in ((proposed_position_risk, "proposed_position_risk"), (daily_loss, "daily_loss")):
        if type(value) is not Decimal or not value.is_finite() or value < 0:
            raise ValueError(f"{name} must be a nonnegative finite Decimal")

    return AccountRiskSnapshot(
        planned_or_open_positions=state.open_positions,
        equity=state.equity,
        daily_loss=daily_loss,
        proposed_position_risk=proposed_position_risk,
    )
