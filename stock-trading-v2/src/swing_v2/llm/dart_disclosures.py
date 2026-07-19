"""Point-in-time DART (opendart.fss.or.kr) disclosure evidence provider.

Turns official electronic-disclosure ``list.json`` records into ``EvidenceItem``
values for the brief layer so Hermes can see point-in-time disclosures. The HTTP
transport is INJECTED as a callable (like the repo's other clients): this module
reads no ``.env``, constructs no default network client, and imports no ``requests``.
The injected ``crtfc_key`` is passed only as a request parameter and never logged
or placed on an ``EvidenceItem``. This provider maps ``stock_code`` -> symbol and
returns only items for the requested symbol; the brief builder — not this provider
— enforces the PIT cutoff, so day-granularity KST publication times suffice.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .brief import EvidenceItem, EvidenceProvider

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

# DART reports success as "000" and "no matching data" as "013"; every other
# status is a hard failure we surface fail-closed.
_STATUS_OK = "000"
_STATUS_NO_DATA = "013"

# KRX/DART operate on the KST calendar; ``rcept_dt`` is day-granular, so we anchor
# each disclosure at KST midnight of its receipt date.
_KST = timezone(timedelta(hours=9))

_DEFAULT_WINDOW_DAYS = 60

HttpGet = Callable[[str, Mapping[str, str]], Mapping]


def make_dart_disclosure_provider(
    *,
    http_get: HttpGet,
    api_key: str,
    corp_code_by_symbol: Mapping[str, str],
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> EvidenceProvider:
    """Build an ``EvidenceProvider`` closure over an injected DART transport."""
    if not callable(http_get):
        raise ValueError("http_get must be callable")
    if type(api_key) is not str or not api_key:
        raise ValueError("api_key must be a nonempty plain str")
    if not isinstance(corp_code_by_symbol, Mapping):
        raise ValueError("corp_code_by_symbol must be a mapping")
    if type(window_days) is not int or window_days <= 0:
        raise ValueError("window_days must be a positive int")
    corp_codes = {str(k): str(v) for k, v in corp_code_by_symbol.items()}

    def provider(symbol: str, signal_date: date) -> tuple[EvidenceItem, ...]:
        if type(symbol) is not str or not symbol:
            raise ValueError("symbol must be a nonempty plain str")
        if type(signal_date) is not date:
            raise ValueError("signal_date must be a plain date")
        corp_code = corp_codes.get(symbol)
        if not corp_code:
            raise ValueError(f"no DART corp_code registered for symbol {symbol}")
        bgn_de = signal_date - timedelta(days=window_days)
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de.strftime("%Y%m%d"),
            "end_de": signal_date.strftime("%Y%m%d"),
        }
        payload = http_get(DART_LIST_URL, params)
        return _parse_list(payload, symbol)

    return provider


def _parse_list(payload: object, symbol: str) -> tuple[EvidenceItem, ...]:
    """Map a DART ``list.json`` payload to evidence for ``symbol``, fail-closed."""
    if not isinstance(payload, Mapping):
        raise ValueError("DART response must be a JSON object")
    status = payload.get("status")
    if type(status) is not str or not status:
        raise ValueError("DART response is missing status")
    if status == _STATUS_NO_DATA:
        return ()
    if status != _STATUS_OK:
        raise ValueError(f"DART request failed with status {status}")
    records = payload.get("list")
    if not isinstance(records, list):
        raise ValueError("DART response list must be an array")
    items: list[EvidenceItem] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("DART list rows must be objects")
        stock_code = _required(record, "stock_code")
        rcept_no = _required(record, "rcept_no")
        rcept_dt = _required(record, "rcept_dt")
        report_nm = _required(record, "report_nm")
        if stock_code != symbol:
            continue
        items.append(EvidenceItem(
            evidence_id=f"dart:{stock_code}:{rcept_no}",
            kind="disclosure",
            symbol=stock_code,
            published_at=_kst_midnight(rcept_dt),
            summary=report_nm,
        ))
    return tuple(items)


def _required(record: Mapping[str, object], field: str) -> str:
    value = record.get(field)
    if type(value) is not str or not value.strip():
        raise ValueError(f"DART list row is missing {field}")
    return value


def _kst_midnight(rcept_dt: str) -> datetime:
    try:
        parsed = datetime.strptime(rcept_dt, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("DART rcept_dt must be YYYYMMDD") from exc
    return parsed.replace(tzinfo=_KST)
