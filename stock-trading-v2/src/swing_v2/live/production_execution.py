"""Isolated production KIS cash-limit submitter contract.

This module is deliberately not safe to execute inside a general Python process.
A future runtime must be a separately privileged process, activated only by a
one-shot operator action after fresh market, balance, order reconciliation and a
per-broker allowlist review.  This is a client contract, not an automated trader.
It neither loads environment files nor issues tokens; tests inject a mocked session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
from typing import Any, Mapping, Protocol

from swing_v2.kis import KisCredentials

from .audit import IntentAuditWriter
from .gate import LiveExecutionConfig, require_live_execution_enabled
from .integrity import seal, verify
from .intent import LiveOrderIntent, Side
from .risk import AccountRiskSnapshot, PretradeLimits, validate_pretrade

_PRODUCTION_BASE_URL = "https://openapi.koreainvestment.com:9443"
_ORDER_CASH_URL = _PRODUCTION_BASE_URL + "/uapi/domestic-stock/v1/trading/order-cash"


class _HttpSession(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, str]) -> Any: ...


class AmbiguousBrokerState(RuntimeError):
    """A request may have reached KIS; halt and reconcile before any further action."""


class BrokerRejectedOrder(RuntimeError):
    """KIS rejected a request or supplied an unusable acknowledgement; never retry."""


@dataclass(frozen=True)
class _CapturedCashLimitAction:
    """Validated immutable decision used for every post-audit transport value."""

    intent: LiveOrderIntent
    request: CashLimitOrderRequest
    wire_body_items: tuple[tuple[str, str], ...]
    tr_id: str

    def matches(self, caller_intent: LiveOrderIntent) -> bool:
        try:
            return _capture_cash_limit_action(caller_intent, self.request.account_number).intent == self.intent
        except (TypeError, ValueError):
            return False

    def wire_body(self) -> dict[str, str]:
        return dict(self.wire_body_items)


def _plain_nonempty(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a nonempty plain str")
    return value


def _wire_decimal(value: object, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")
    return format(value.normalize(), "f")


def _account_parts(account_number: object) -> tuple[str, str]:
    account = _plain_nonempty(account_number, "account_number")
    cano, separator, product = account.partition("-")
    if separator != "-" or len(cano) != 8 or len(product) != 2 or not cano.isdigit() or not product.isdigit():
        raise ValueError("account_number must have the form CANO-ACNT_PRDT_CD")
    return cano, product


@dataclass(frozen=True)
class CashLimitOrderRequest:
    """Exact KRX cash-limit values; market/stop/NXT/SOR do not have representations."""

    account_number: str
    symbol: str
    classification: str
    side: Side
    quantity: int
    limit_price: Decimal

    def __post_init__(self) -> None:
        _account_parts(self.account_number)
        _plain_nonempty(self.symbol, "symbol")
        if type(self.classification) is not str or self.classification not in {"STOCK", "DOMESTIC_INDEX_OR_SECTOR"}:
            raise ValueError("classification is not explicitly allowed for KRX cash trading")
        if type(self.side) is not Side:
            raise ValueError("side must be exact BUY or SELL")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("quantity must be a positive plain int")
        _wire_decimal(self.limit_price, "limit_price")

    def wire_body(self) -> dict[str, str]:
        cano, product = _account_parts(self.account_number)
        return {"CANO": cano, "ACNT_PRDT_CD": product, "PDNO": self.symbol,
                "ORD_DVSN": "00", "ORD_QTY": str(self.quantity),
                "ORD_UNPR": _wire_decimal(self.limit_price, "limit_price"),
                "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01", "CNDT_PRIC": "0"}


def _capture_cash_limit_action(intent: LiveOrderIntent, account_number: str) -> _CapturedCashLimitAction:
    """Copy all order-decision primitives before auditing, hooks, or transport."""
    if type(intent) is not LiveOrderIntent:
        raise ValueError("intent must be an exact LiveOrderIntent")
    captured_intent = LiveOrderIntent(
        strategy=intent.strategy, strategy_version=intent.strategy_version,
        signal_date=intent.signal_date, symbol=intent.symbol, classification=intent.classification,
        side=intent.side, quantity=intent.quantity, limit_price=intent.limit_price,
        order_mode=intent.order_mode,
    )
    if type(intent.intent_id) is not str or intent.intent_id != captured_intent.intent_id:
        raise ValueError("intent identity mismatch")
    request = CashLimitOrderRequest(account_number, captured_intent.symbol, captured_intent.classification,
                                    captured_intent.side, captured_intent.quantity, captured_intent.limit_price)
    body = request.wire_body()
    return _CapturedCashLimitAction(
        intent=captured_intent, request=request, wire_body_items=tuple(body.items()),
        tr_id="TTTC0012U" if captured_intent.side is Side.BUY else "TTTC0011U",
    )


@dataclass(frozen=True)
class KisOrderAcknowledgement:
    forwarding_order_organization: str
    order_number: str


@dataclass(frozen=True)
class BrokerOrderReceipt:
    """Broker identifiers from a verified successful order acknowledgement."""

    forwarding_order_organization: str
    order_number: str

    def __post_init__(self) -> None:
        _plain_nonempty(self.forwarding_order_organization, "forwarding_order_organization")
        _plain_nonempty(self.order_number, "order_number")


_AMENDMENT_CONFIRMATION = "KIS_LIVE_AMEND_CANCEL_OPERATOR_CONFIRMED"


def _amendment_config_bytes(config: "LiveAmendmentConfig") -> bytes:
    if type(config.live_amendment_enabled) is not bool:
        raise ValueError("live_amendment_enabled must be a plain bool")
    if config.operator_confirmation is not None and type(config.operator_confirmation) is not str:
        raise ValueError("operator_confirmation must be a plain str or None")
    return json.dumps({"live_amendment_enabled": config.live_amendment_enabled,
                       "operator_confirmation": config.operator_confirmation},
                      sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True)
class LiveAmendmentConfig:
    """Approval independent from order submission; preparation still cannot submit."""

    live_amendment_enabled: bool = False
    operator_confirmation: str | None = None
    _integrity_seal: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_integrity_seal", seal("LiveAmendmentConfig", _amendment_config_bytes(self)))


@dataclass(frozen=True)
class PreparedAmendmentOrCancel:
    """Serialization-only representation: no method here submits it."""

    endpoint: str
    headers: dict[str, str]
    body: dict[str, str]


def prepare_amendment_or_cancel(*, config: LiveAmendmentConfig, account_number: str,
                                original_receipt: BrokerOrderReceipt, quantity: Decimal,
                                limit_price: Decimal, cancel: bool) -> PreparedAmendmentOrCancel:
    """Prepare the documented KIS revision/cancel wire shape without transport access."""
    if type(config) is not LiveAmendmentConfig or not verify("LiveAmendmentConfig", _amendment_config_bytes(config), config._integrity_seal):
        raise ValueError("separate amendment approval is invalid")
    if config.live_amendment_enabled is not True or config.operator_confirmation != _AMENDMENT_CONFIRMATION:
        raise ValueError("separate amendment approval is required")
    if type(original_receipt) is not BrokerOrderReceipt:
        raise ValueError("original_receipt must be an exact broker acknowledgement")
    if type(cancel) is not bool:
        raise ValueError("cancel must be a plain bool")
    cano, product = _account_parts(account_number)
    quantity_wire = _wire_decimal(quantity, "quantity")
    price_wire = _wire_decimal(limit_price, "limit_price")
    return PreparedAmendmentOrCancel(
        endpoint=_PRODUCTION_BASE_URL + "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        headers={"tr_id": "TTTC0013U"},
        body={"CANO": cano, "ACNT_PRDT_CD": product,
              "KRX_FWDG_ORD_ORGNO": original_receipt.forwarding_order_organization,
              "ORGN_ODNO": original_receipt.order_number, "ORD_DVSN": "00",
              "RVSE_CNCL_DVSN_CD": "02" if cancel else "01", "ORD_QTY": quantity_wire,
              "ORD_UNPR": price_wire, "QTY_ALL_ORD_YN": "N", "EXCG_ID_DVSN_CD": "KRX", "CNDT_PRIC": "0"},
    )


class KisProductionTradingClient:
    """Production-only, injected-transport submitter for cash KRX limit orders."""

    def __init__(self, *, credentials: KisCredentials, access_token: str, account_number: str,
                 session: _HttpSession, audit_writer: IntentAuditWriter) -> None:
        if type(credentials) is not KisCredentials:
            raise ValueError("credentials must be exact KisCredentials")
        _plain_nonempty(access_token, "access_token")
        _account_parts(account_number)
        if session is None or not callable(getattr(session, "post", None)):
            raise ValueError("session must provide post")
        if type(audit_writer) is not IntentAuditWriter:
            raise ValueError("audit_writer must be exact IntentAuditWriter")
        self._credentials = credentials
        self._access_token = access_token
        self._account_number = account_number
        self._session = session
        self._audit_writer = audit_writer

    def submit_cash_limit_order(self, *, config: LiveExecutionConfig, intent: LiveOrderIntent,
                                snapshot: AccountRiskSnapshot, limits: PretradeLimits) -> KisOrderAcknowledgement:
        """Audit before exactly one injected POST. Any later failure is a reconciliation halt."""
        require_live_execution_enabled(config)
        with self._audit_writer._account_halt_lock(self._account_number):
            if self._audit_writer._has_ambiguous_halt(self._account_number):
                raise AmbiguousBrokerState("KIS order state is durably halted; reconcile before retry")
            if self._audit_writer._after_first_halt_check is not None:
                self._audit_writer._after_first_halt_check()
            action = _capture_cash_limit_action(intent, self._account_number)
            validate_pretrade(action.intent, snapshot, limits=limits)
            self._audit_writer.write(action.intent)  # persistent write-once idempotency boundary
            if self._audit_writer._has_ambiguous_halt(self._account_number):
                raise AmbiguousBrokerState("KIS order state is durably halted; reconcile before retry")
            headers = {
                "authorization": f"Bearer {self._access_token}", "appkey": self._credentials.app_key,
                "appsecret": self._credentials.app_secret,
                "tr_id": action.tr_id, "custtype": "P",
            }
            try:
                response = self._session.post(_ORDER_CASH_URL, headers=headers, json=action.wire_body())
                response.raise_for_status()
                payload = response.json()
                return _acknowledgement(payload)
            except BaseException as exc:
                try:
                    self._audit_writer._record_ambiguous_halt_locked(self._account_number)
                except BaseException as halt_exc:
                    raise AmbiguousBrokerState("KIS order state is ambiguous and durable halt recording failed; reconcile before retry") from halt_exc
                raise AmbiguousBrokerState("KIS order transport/HTTP/parse state is ambiguous; reconcile before retry") from exc

    def _halt_ambiguous(self) -> None:
        try:
            self._audit_writer._record_ambiguous_halt(self._account_number)
        except BaseException as exc:
            raise AmbiguousBrokerState("KIS order state changed and durable halt recording failed; reconcile before retry") from exc
        raise AmbiguousBrokerState("KIS order state changed after audit; reconcile before retry")


def _acknowledgement(payload: object) -> KisOrderAcknowledgement:
    if not isinstance(payload, Mapping):
        raise BrokerRejectedOrder("KIS order response must be a JSON object")
    if payload.get("rt_cd") != "0":
        raise BrokerRejectedOrder("KIS rejected order request")
    output = payload.get("output")
    if not isinstance(output, Mapping):
        raise BrokerRejectedOrder("KIS successful order response is missing output")
    organization = output.get("KRX_FWDG_ORD_ORGNO")
    order_number = output.get("ODNO")
    if type(organization) is not str or not organization or type(order_number) is not str or not order_number:
        raise BrokerRejectedOrder("KIS successful order response lacks broker identifiers")
    return KisOrderAcknowledgement(organization, order_number)
