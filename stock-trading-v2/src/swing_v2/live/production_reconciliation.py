"""Production KIS read-only reconciliation contract.

Phase 1 is a data-reader and parsing boundary, not authority to release an
ambiguous-order halt.  A future privileged reconciler must obtain atomic broker
queries and prove account, position, cash, order, and fill state before an
explicit operator action.  This module has no halt mutation and no submit,
amend, cancel, token, or environment-loading capability.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from typing import Any, Callable, Mapping, Protocol

from swing_v2.kis import KisCredentials
from .intent import Side

_BASE_URL = "https://openapi.koreainvestment.com:9443"
_OPEN_ORDERS_URL = _BASE_URL + "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
_DAILY_FILLS_URL = _BASE_URL + "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
_BALANCE_URL = _BASE_URL + "/uapi/domestic-stock/v1/trading/inquire-balance"
_MAX_PAGES = 5


class _GetSession(Protocol):
    def get(self, url: str, *, headers: dict[str, str], params: dict[str, str]) -> Any: ...


class ReconciliationError(RuntimeError):
    """A broker read cannot be safely interpreted; callers must fail closed."""


def _plain(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{name} must be a nonempty plain str")
    return value


def _account_parts(value: object) -> tuple[str, str]:
    account = _plain(value, "account_number")
    cano, sep, product = account.partition("-")
    if sep != "-" or len(cano) != 8 or len(product) != 2 or not cano.isdigit() or not product.isdigit():
        raise ValueError("account_number must have the form CANO-ACNT_PRDT_CD")
    return cano, product


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not str or not value or not value.isascii() or not value.isdigit():
        raise ReconciliationError(f"{name} must be a nonnegative decimal integer")
    parsed = int(value)
    if positive and parsed == 0:
        raise ReconciliationError(f"{name} must be positive")
    return parsed


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if type(value) is not str or not value:
        raise ReconciliationError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ReconciliationError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        raise ReconciliationError(f"{name} is invalid")
    return parsed


def _field(record: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in record:
            return record[name]
    raise ReconciliationError(f"missing KIS field {names[0]}")


def _side(value: object) -> Side:
    if value == "01":
        return Side.SELL
    if value == "02":
        return Side.BUY
    raise ReconciliationError("KIS side is not explicitly BUY or SELL")


def _date(value: object, name: str) -> date:
    if type(value) is not str or len(value) != 8 or not value.isdigit():
        raise ValueError(f"{name} must be a plain YYYYMMDD date")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid YYYYMMDD date") from exc


@dataclass(frozen=True)
class OpenOrder:
    forwarding_order_organization: str
    order_number: str
    symbol: str
    side: Side
    quantity: int
    limit_price: Decimal
    filled_quantity: int

    def __post_init__(self) -> None:
        _plain(self.forwarding_order_organization, "forwarding_order_organization")
        _plain(self.order_number, "order_number")
        _plain(self.symbol, "symbol")
        if type(self.side) is not Side or type(self.quantity) is not int or self.quantity <= 0 or type(self.filled_quantity) is not int or not 0 <= self.filled_quantity <= self.quantity:
            raise ValueError("invalid open order")
        if type(self.limit_price) is not Decimal or not self.limit_price.is_finite() or self.limit_price < 0:
            raise ValueError("invalid open order price")


@dataclass(frozen=True)
class OrderFill:
    forwarding_order_organization: str
    order_number: str
    symbol: str
    side: Side
    quantity: int
    filled_quantity: int
    average_fill_price: Decimal


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: int
    average_purchase_price: Decimal


@dataclass(frozen=True)
class BrokerOrderReference:
    forwarding_order_organization: str
    order_number: str
    symbol: str
    side: Side
    quantity: int

    def __post_init__(self) -> None:
        _plain(self.forwarding_order_organization, "forwarding_order_organization")
        _plain(self.order_number, "order_number")
        _plain(self.symbol, "symbol")
        if type(self.side) is not Side or type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("invalid broker order reference")


@dataclass(frozen=True)
class ReconciliationSnapshot:
    observed_at: datetime
    account_binding_hash: str
    holdings: tuple[Holding, ...]
    open_orders: tuple[OpenOrder, ...]
    fills: tuple[OrderFill, ...]
    source_observations: tuple[QueryProvenance, ...]
    requested_fill_date_range: tuple[str, str] | None
    account_binding_scope: str
    non_atomic_observation: bool
    authorizes_halt_release: bool
    digest: str


@dataclass(frozen=True)
class QueryProvenance:
    """Metadata only; it records a completed GET sequence, not broker authority."""

    source: str
    observed_at: datetime
    page_count: int
    pagination_complete: bool


class AmbiguousHaltAssessment(str, Enum):
    CLEAR_EVIDENCE = "CLEAR_EVIDENCE"
    UNRESOLVED = "UNRESOLVED"
    CONTRADICTION = "CONTRADICTION"


class KisProductionReconciliationClient:
    """Injected-production-only transport with precisely three GET reader methods."""

    def __init__(self, *, credentials: KisCredentials, access_token: str, account_number: str,
                 session: _GetSession, clock: Callable[[], datetime] | None = None) -> None:
        if type(credentials) is not KisCredentials:
            raise ValueError("credentials must be exact KisCredentials")
        _plain(access_token, "access_token")
        _account_parts(account_number)
        if session is None or not callable(getattr(session, "get", None)):
            raise ValueError("session must provide get")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self._credentials, self._access_token = credentials, access_token
        self._account_number, self._session = account_number, session
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _headers(self, tr_id: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self._access_token}", "appkey": self._credentials.app_key,
                "appsecret": self._credentials.app_secret, "tr_id": tr_id, "custtype": "P"}

    def _read_pages(self, *, url: str, tr_id: str, params: dict[str, str], list_name: str,
                    object_name: str | None = None) -> tuple[list[Mapping[str, object]], Mapping[str, object] | None, int]:
        rows: list[Mapping[str, object]] = []
        summary: Mapping[str, object] | None = None
        cursor_fk = cursor_nk = ""
        for page in range(_MAX_PAGES):
            page_params = dict(params, CTX_AREA_FK100=cursor_fk, CTX_AREA_NK100=cursor_nk)
            try:
                response = self._session.get(url, headers=self._headers(tr_id), params=page_params)
                response.raise_for_status()
                payload = response.json()
            except BaseException as exc:
                raise ReconciliationError("KIS read transport/HTTP/JSON failure") from exc
            if type(payload) is not dict or payload.get("rt_cd") != "0":
                raise ReconciliationError("KIS read response rejected or malformed")
            page_rows = payload.get(list_name)
            if type(page_rows) is not list or not all(type(item) is dict for item in page_rows):
                raise ReconciliationError(f"KIS successful response has malformed {list_name}")
            rows.extend(page_rows)
            if object_name is not None:
                candidate = payload.get(object_name)
                if type(candidate) is not dict:
                    raise ReconciliationError(f"KIS successful response has malformed {object_name}")
                summary = candidate
            fk, nk = payload.get("ctx_area_fk100", ""), payload.get("ctx_area_nk100", "")
            raw_headers = getattr(response, "headers", None)
            tr_cont = raw_headers.get("tr_cont", "") if isinstance(raw_headers, Mapping) else ""
            if type(tr_cont) is not str or tr_cont not in {"", "M", "F"}:
                raise ReconciliationError("KIS continuation header is malformed")
            continuation = tr_cont in {"M", "F"} or fk != "" or nk != ""
            if continuation:
                if type(fk) is not str or not fk or type(nk) is not str or not nk:
                    raise ReconciliationError("KIS continuation cursor is malformed")
                if page == _MAX_PAGES - 1:
                    raise ReconciliationError("KIS reconciliation page cap reached")
                cursor_fk, cursor_nk = fk, nk
                continue
            return rows, summary, page + 1
        raise ReconciliationError("KIS reconciliation page cap reached")

    def read_open_orders(self) -> tuple[OpenOrder, ...]:
        return self._read_open_orders()[0]

    def _read_open_orders(self) -> tuple[tuple[OpenOrder, ...], int]:
        cano, product = _account_parts(self._account_number)
        rows, _, pages = self._read_pages(url=_OPEN_ORDERS_URL, tr_id="TTTC0084R", list_name="output", params={
            "CANO": cano, "ACNT_PRDT_CD": product, "INQR_DVSN_1": "0", "INQR_DVSN_2": "0", "EXCG_ID_DVSN_CD": "KRX"})
        parsed = tuple(_open_order(row) for row in rows)
        _unique(((item.forwarding_order_organization, item.order_number) for item in parsed), "open order")
        return parsed, pages

    def read_daily_order_fills(self, start_date: str, end_date: str) -> tuple[OrderFill, ...]:
        start, end = _date(start_date, "start_date"), _date(end_date, "end_date")
        if start > end or (end - start).days > 30:
            raise ValueError("date range must be ordered and at most 31 days")
        return self._read_daily_order_fills(start_date, end_date)[0]

    def _read_daily_order_fills(self, start_date: str, end_date: str) -> tuple[tuple[OrderFill, ...], int]:
        cano, product = _account_parts(self._account_number)
        rows, _, pages = self._read_pages(url=_DAILY_FILLS_URL, tr_id="TTTC0081R", list_name="output1", object_name="output2", params={
            "CANO": cano, "ACNT_PRDT_CD": product, "INQR_STRT_DT": start_date, "INQR_END_DT": end_date,
            "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00",
            "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "", "EXCG_ID_DVSN_CD": "KRX"})
        parsed = tuple(_fill(row) for row in rows)
        _unique(((item.forwarding_order_organization, item.order_number) for item in parsed), "fill")
        return parsed, pages

    def read_balance(self) -> ReconciliationSnapshot:
        holdings, pages = self._read_holdings()
        observation = self._observation("balance", pages)
        return self._snapshot(holdings, (), (), (observation,), None)

    def read_snapshot(self, start_date: str, end_date: str) -> ReconciliationSnapshot:
        # Validate before any GET, then retain per-source sequence metadata.  This
        # is deliberately non-atomic and never confers halt-release authority.
        start, end = _date(start_date, "start_date"), _date(end_date, "end_date")
        if start > end or (end - start).days > 30:
            raise ValueError("date range must be ordered and at most 31 days")
        holdings, holding_pages = self._read_holdings()
        holding_observation = self._observation("balance", holding_pages)
        orders, order_pages = self._read_open_orders()
        order_observation = self._observation("open_orders", order_pages)
        fills, fill_pages = self._read_daily_order_fills(start_date, end_date)
        fill_observation = self._observation("daily_order_fills", fill_pages)
        return self._snapshot(holdings, orders, fills, (holding_observation, order_observation, fill_observation), (start_date, end_date))

    def _read_holdings(self) -> tuple[tuple[Holding, ...], int]:
        cano, product = _account_parts(self._account_number)
        rows, _, pages = self._read_pages(url=_BALANCE_URL, tr_id="TTTC8434R", list_name="output1", object_name="output2", params={
            "CANO": cano, "ACNT_PRDT_CD": product, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00"})
        parsed = tuple(Holding(_plain(_field(row, "PDNO"), "symbol"), _integer(_field(row, "HLDG_QTY"), "holding quantity"), _decimal(_field(row, "PCHS_AVG_PRIC"), "average purchase price")) for row in rows)
        _unique((item.symbol for item in parsed), "holding")
        return parsed, pages

    def _observation(self, source: str, page_count: int) -> QueryProvenance:
        observed_at = self._clock()
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise ReconciliationError("clock must return an aware datetime")
        return QueryProvenance(source, observed_at, page_count, True)

    def _snapshot(self, holdings: tuple[Holding, ...], orders: tuple[OpenOrder, ...], fills: tuple[OrderFill, ...],
                  source_observations: tuple[QueryProvenance, ...], requested_fill_date_range: tuple[str, str] | None) -> ReconciliationSnapshot:
        observed_at = source_observations[-1].observed_at
        binding = hashlib.sha256(self._account_number.encode("ascii")).hexdigest()
        scope = "LOCAL_INJECTED_ACCOUNT_HASH_UNVERIFIED"
        non_atomic = True
        authorizes_halt_release = False
        canonical = _snapshot_canonical(observed_at, binding, holdings, orders, fills, source_observations,
                                        requested_fill_date_range, scope, non_atomic, authorizes_halt_release)
        return ReconciliationSnapshot(observed_at, binding, holdings, orders, fills, source_observations,
                                      requested_fill_date_range, scope, non_atomic, authorizes_halt_release,
                                      hashlib.sha256(canonical.encode()).hexdigest())


def _open_order(row: Mapping[str, object]) -> OpenOrder:
    return OpenOrder(_plain(_field(row, "KRX_FWDG_ORD_ORGNO", "ORD_GNO_BRNO"), "forwarding_order_organization"), _plain(_field(row, "ODNO"), "order_number"), _plain(_field(row, "PDNO"), "symbol"), _side(_field(row, "SLL_BUY_DVSN_CD")), _integer(_field(row, "ORD_QTY"), "order quantity", positive=True), _decimal(_field(row, "ORD_UNPR"), "order price"), _integer(_field(row, "TOT_CCLD_QTY"), "filled quantity"))


def _fill(row: Mapping[str, object]) -> OrderFill:
    quantity = _integer(_field(row, "ORD_QTY"), "order quantity", positive=True)
    filled = _integer(_field(row, "TOT_CCLD_QTY"), "filled quantity")
    if filled > quantity:
        raise ReconciliationError("filled quantity exceeds order quantity")
    return OrderFill(_plain(_field(row, "KRX_FWDG_ORD_ORGNO", "ORD_GNO_BRNO"), "forwarding_order_organization"), _plain(_field(row, "ODNO"), "order_number"), _plain(_field(row, "PDNO"), "symbol"), _side(_field(row, "SLL_BUY_DVSN_CD")), quantity, filled, _decimal(_field(row, "AVG_PRVS", "AVG_CCLD_UNPR"), "average fill price"))


def _unique(values: object, label: str) -> None:
    seen: set[object] = set()
    for value in values:  # type: ignore[union-attr]
        if value in seen:
            raise ReconciliationError(f"duplicate {label} broker identity")
        seen.add(value)


def _json_value(value: OpenOrder | OrderFill | Holding) -> dict[str, object]:
    result = asdict(value)
    return {key: item.value if isinstance(item, Enum) else format(item, "f") if isinstance(item, Decimal) else item for key, item in result.items()}


def _snapshot_canonical(observed_at: datetime, binding: str, holdings: tuple[Holding, ...],
                        orders: tuple[OpenOrder, ...], fills: tuple[OrderFill, ...],
                        source_observations: tuple[QueryProvenance, ...],
                        requested_fill_date_range: tuple[str, str] | None,
                        account_binding_scope: str, non_atomic_observation: bool,
                        authorizes_halt_release: bool) -> str:
    return json.dumps({"observed_at": observed_at.isoformat(), "account_binding_hash": binding,
        "holdings": [_json_value(item) for item in holdings], "open_orders": [_json_value(item) for item in orders],
        "fills": [_json_value(item) for item in fills],
        "source_observations": [{"source": item.source, "observed_at": item.observed_at.isoformat(),
                                 "page_count": item.page_count, "pagination_complete": item.pagination_complete}
                                for item in source_observations],
        "requested_fill_date_range": requested_fill_date_range,
        "account_binding_scope": account_binding_scope,
        "non_atomic_observation": non_atomic_observation,
        "authorizes_halt_release": authorizes_halt_release}, sort_keys=True, separators=(",", ":"))


def _valid_snapshot(snapshot: ReconciliationSnapshot) -> bool:
    try:
        if type(snapshot.observed_at) is not datetime or snapshot.observed_at.tzinfo is None:
            return False
        if type(snapshot.account_binding_hash) is not str or len(snapshot.account_binding_hash) != 64:
            return False
        if type(snapshot.holdings) is not tuple or not all(type(item) is Holding for item in snapshot.holdings):
            return False
        if type(snapshot.open_orders) is not tuple or not all(type(item) is OpenOrder for item in snapshot.open_orders):
            return False
        if type(snapshot.fills) is not tuple or not all(type(item) is OrderFill for item in snapshot.fills):
            return False
        if type(snapshot.source_observations) is not tuple or not snapshot.source_observations:
            return False
        expected_sources = ("balance",) if len(snapshot.source_observations) == 1 else ("balance", "open_orders", "daily_order_fills")
        if tuple(item.source for item in snapshot.source_observations if type(item) is QueryProvenance) != expected_sources:
            return False
        for item in snapshot.source_observations:
            if (type(item) is not QueryProvenance or type(item.observed_at) is not datetime or item.observed_at.tzinfo is None
                    or type(item.page_count) is not int or item.page_count < 1 or item.pagination_complete is not True):
                return False
        if snapshot.requested_fill_date_range is None:
            if len(snapshot.source_observations) != 1:
                return False
        elif (type(snapshot.requested_fill_date_range) is not tuple or len(snapshot.requested_fill_date_range) != 2
              or any(type(value) is not str for value in snapshot.requested_fill_date_range)):
            return False
        else:
            start, end = snapshot.requested_fill_date_range
            if _date(start, "start_date") > _date(end, "end_date"):
                return False
        if (snapshot.account_binding_scope != "LOCAL_INJECTED_ACCOUNT_HASH_UNVERIFIED"
                or snapshot.non_atomic_observation is not True or snapshot.authorizes_halt_release is not False):
            return False
        if type(snapshot.digest) is not str or len(snapshot.digest) != 64:
            return False
        canonical = _snapshot_canonical(snapshot.observed_at, snapshot.account_binding_hash, snapshot.holdings, snapshot.open_orders,
                                        snapshot.fills, snapshot.source_observations, snapshot.requested_fill_date_range,
                                        snapshot.account_binding_scope, snapshot.non_atomic_observation, snapshot.authorizes_halt_release)
        return hashlib.sha256(canonical.encode()).hexdigest() == snapshot.digest
    except BaseException:
        return False


def assess_ambiguous_halt(snapshot: ReconciliationSnapshot, broker_reference: BrokerOrderReference) -> AmbiguousHaltAssessment:
    """Pure, conservative evidence classification; it never changes any halt marker."""
    if type(snapshot) is not ReconciliationSnapshot or type(broker_reference) is not BrokerOrderReference:
        return AmbiguousHaltAssessment.UNRESOLVED
    if not _valid_snapshot(snapshot):
        return AmbiguousHaltAssessment.UNRESOLVED
    identity = (broker_reference.forwarding_order_organization, broker_reference.order_number)
    orders = [item for item in snapshot.open_orders if (item.forwarding_order_organization, item.order_number) == identity]
    fills = [item for item in snapshot.fills if (item.forwarding_order_organization, item.order_number) == identity]
    matching = orders + fills
    if not matching:
        return AmbiguousHaltAssessment.UNRESOLVED
    if any(item.symbol != broker_reference.symbol or item.side is not broker_reference.side or item.quantity != broker_reference.quantity for item in matching):
        return AmbiguousHaltAssessment.CONTRADICTION
    # Phase 1 observations are not independently authorized evidence.  Even an
    # exact full fill is only a local, non-atomic read and cannot release a halt.
    return AmbiguousHaltAssessment.UNRESOLVED
