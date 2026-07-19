"""Read-only Korea Investment & Securities OpenAPI client."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable, Mapping, Protocol

import requests

from .contracts import DailyBar


_TOKEN_URL = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
_INQUIRE_BALANCE_URL = (
    "https://openapi.koreainvestment.com:9443/"
    "uapi/domestic-stock/v1/trading/inquire-balance"
)
_INQUIRE_DAILY_ITEM_CHART_PRICE_URL = (
    "https://openapi.koreainvestment.com:9443/"
    "uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
)
_DAILY_ITEM_CHART_PRICE_TR_ID = "FHKST03010100"
_DAILY_ITEM_CHART_PRICE_PAGE_SIZE = 100
_INQUIRE_DAILY_INDEX_CHART_PRICE_URL = (
    "https://openapi.koreainvestment.com:9443/"
    "uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
)
# Official KIS [v1_국내주식-021] domestic daily index-chart request.
_DAILY_INDEX_CHART_PRICE_TR_ID = "FHKUP03500100"
_DAILY_INDEX_CHART_PRICE_PAGE_SIZE = 100
KOSPI_INDEX_CODE = "0001"
KOSPI_MARKET_SYMBOL = "KOSPI"


@dataclass(frozen=True)
class KisCredentials:
    """Application credentials used to request a KIS access token."""

    app_key: str
    app_secret: str


class _HttpSession(Protocol):
    def post(self, url: str, *, json: dict[str, str]) -> Any: ...

    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, str]
    ) -> Any: ...


class PageRequestLimiter(Protocol):
    """Allows a caller to bound and pace every KIS daily-price HTTP page."""

    def before_page_request(self) -> None: ...


class PageRequestBudget:
    """A shared, injected request budget for KIS daily-price pagination."""

    def __init__(self, *, max_requests: int, delay_seconds: float, sleep: Callable[[float], None]) -> None:
        if type(max_requests) is not int or max_requests <= 0:
            raise ValueError("max_requests must be a positive int")
        if type(delay_seconds) not in (int, float) or delay_seconds <= 0:
            raise ValueError("delay_seconds must be nonzero and positive")
        if not callable(sleep):
            raise ValueError("sleep must be callable")
        self._max_requests = max_requests
        self._delay_seconds = delay_seconds
        self._sleep = sleep
        self._requests_made = 0

    def before_page_request(self) -> None:
        if self._requests_made >= self._max_requests:
            raise RuntimeError("KIS daily-price page request cap reached")
        if self._requests_made:
            self._sleep(self._delay_seconds)
        self._requests_made += 1


class KisClient:
    """KIS authentication plus explicitly read-only account and market data APIs."""

    def __init__(self, *, credentials: KisCredentials, session: _HttpSession | None = None) -> None:
        self._credentials = credentials
        self._session = session or requests.Session()

    def issue_access_token(self) -> str:
        """Request and return a KIS OAuth access token."""
        return self._issue_access_token_with_expiry()[0]

    def get_access_token(
        self,
        *,
        cache_path: Path | None = None,
        expiry_skew: timedelta = timedelta(minutes=5),
        now: Callable[[], datetime] | None = None,
    ) -> str:
        """Return a valid cached OAuth token or issue and securely cache one.

        Passing ``None`` for ``cache_path`` retains the uncached behavior.
        """
        if not isinstance(expiry_skew, timedelta) or expiry_skew < timedelta():
            raise ValueError("expiry_skew must be a nonnegative timedelta")
        clock = now or (lambda: datetime.now(timezone.utc))
        current_time = _utc_datetime(clock())
        if cache_path is None:
            return self.issue_access_token()
        cached_token = _read_cached_token(Path(cache_path), current_time, expiry_skew)
        if cached_token is not None:
            return cached_token
        access_token, expires_at = self._issue_access_token_with_expiry(now=current_time)
        if expires_at is not None:
            _write_token_cache(Path(cache_path), access_token, current_time, expires_at)
        return access_token

    def _issue_access_token_with_expiry(self, *, now: datetime | None = None) -> tuple[str, datetime | None]:
        response = self._session.post(
            _TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "appkey": self._credentials.app_key,
                "appsecret": self._credentials.app_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("KIS token response must be a JSON object")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("KIS token response is missing access_token")
        expires_in = payload.get("expires_in")
        if type(expires_in) not in (int, float) or expires_in <= 0:
            return access_token, None
        issued_at = _utc_datetime(now or datetime.now(timezone.utc))
        return access_token, issued_at + timedelta(seconds=expires_in)

    def inquire_balance(self, access_token: str, account_number: str) -> dict[str, Any]:
        """Return the read-only KIS balance response for an account."""
        cano, separator, account_product_code = account_number.partition("-")
        if (
            separator != "-"
            or not cano.isdigit()
            or len(cano) != 8
            or not account_product_code.isdigit()
            or len(account_product_code) != 2
        ):
            raise ValueError("account_number must have the form CANO-ACNT_PRDT_CD")

        response = self._session.get(
            _INQUIRE_BALANCE_URL,
            headers={
                "authorization": f"Bearer {access_token}",
                "appkey": self._credentials.app_key,
                "appsecret": self._credentials.app_secret,
                "tr_id": "TTTC8434R",
                "custtype": "P",
            },
            params={
                "CANO": cano, "ACNT_PRDT_CD": account_product_code,
                "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "01",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("KIS balance response must be a JSON object")
        return payload

    def load_domestic_daily_bars(
        self, access_token: str, symbol: str, asset_type: str, start: date, end: date, *,
        page_request_limiter: PageRequestLimiter | None = None,
    ) -> tuple[DailyBar, ...]:
        """Load adjusted KRX stock/ETF bars in an explicit inclusive date range.

        The official daily chart API returns at most 100 newest-first rows per
        call. This read-only method moves the end-date cursor backward, filters
        its explicit bounds, deduplicates by date, and returns ascending bars.
        """
        _validate_daily_request(access_token, symbol, asset_type, start, end)
        cursor = end
        bars_by_date: dict[date, DailyBar] = {}
        while True:
            if page_request_limiter is not None:
                page_request_limiter.before_page_request()
            response = self._session.get(
                _INQUIRE_DAILY_ITEM_CHART_PRICE_URL,
                headers={
                    "authorization": f"Bearer {access_token}",
                    "appkey": self._credentials.app_key,
                    "appsecret": self._credentials.app_secret,
                    "tr_id": _DAILY_ITEM_CHART_PRICE_TR_ID,
                },
                params={
                    "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": cursor.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
                },
            )
            response.raise_for_status()
            page = _daily_chart_rows(response.json(), symbol, asset_type)
            if not page:
                break
            for bar in page:
                if start <= bar.trade_date <= end:
                    bars_by_date[bar.trade_date] = bar
            oldest = min(bar.trade_date for bar in page)
            if len(page) < _DAILY_ITEM_CHART_PRICE_PAGE_SIZE or oldest <= start:
                break
            next_cursor = oldest - timedelta(days=1)
            if next_cursor >= cursor:
                raise ValueError("KIS daily-price pagination cursor did not move backwards")
            cursor = next_cursor
        return tuple(bars_by_date[day] for day in sorted(bars_by_date))

    def load_kospi_daily_bars(
        self, access_token: str, start: date, end: date, *,
        page_request_limiter: PageRequestLimiter | None = None,
    ) -> tuple[DailyBar, ...]:
        """Load KOSPI (official KIS industry code ``0001``) daily index bars.

        Uses only KIS's read-only domestic daily-index chart endpoint
        (``FHKUP03500100``).  Responses are newest-first, so the cursor moves
        backward and every transport GET consumes the optional shared limiter.
        KOSPI is market data only, never a PIT universe classification.
        """
        _validate_daily_request(access_token, KOSPI_MARKET_SYMBOL, "INDEX", start, end)
        cursor = end
        bars_by_date: dict[date, DailyBar] = {}
        while True:
            if page_request_limiter is not None:
                page_request_limiter.before_page_request()
            response = self._session.get(
                _INQUIRE_DAILY_INDEX_CHART_PRICE_URL,
                headers={
                    "authorization": f"Bearer {access_token}",
                    "appkey": self._credentials.app_key,
                    "appsecret": self._credentials.app_secret,
                    "tr_id": _DAILY_INDEX_CHART_PRICE_TR_ID,
                },
                params={
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_INPUT_ISCD": KOSPI_INDEX_CODE,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": cursor.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                },
            )
            response.raise_for_status()
            page = _daily_index_chart_rows(response.json())
            if not page:
                break
            for bar in page:
                if start <= bar.trade_date <= end:
                    bars_by_date[bar.trade_date] = bar
            oldest = min(bar.trade_date for bar in page)
            if oldest <= start:
                break
            next_cursor = oldest - timedelta(days=1)
            if next_cursor >= cursor:
                raise ValueError("KIS daily-index pagination cursor did not move backwards")
            cursor = next_cursor
        return tuple(bars_by_date[day] for day in sorted(bars_by_date))


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("token cache clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _read_cached_token(path: Path, now: datetime, expiry_skew: timedelta) -> str | None:
    """Return a valid token only from an owner-private regular cache file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            payload = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if not isinstance(payload, Mapping):
        return None
    access_token = payload.get("access_token")
    issued_at = _parse_cache_time(payload.get("issued_at"))
    expires_at = _parse_cache_time(payload.get("expires_at"))
    if (
        type(access_token) is not str
        or not access_token
        or issued_at is None
        or expires_at is None
        or issued_at > expires_at
        or expires_at - expiry_skew <= now
    ):
        return None
    return access_token


def _parse_cache_time(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _write_token_cache(path: Path, access_token: str, issued_at: datetime, expires_at: datetime) -> None:
    """Atomically persist an owner-only cache; callers never log its payload."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "access_token": access_token,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary_path = stream.name
            os.chmod(temporary_path, 0o600)
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _validate_daily_request(access_token: object, symbol: object, asset_type: object, start: object, end: object) -> None:
    if type(access_token) is not str or not access_token:
        raise ValueError("access_token must be a nonempty plain str")
    if type(symbol) is not str or not symbol:
        raise ValueError("symbol must be a nonempty plain str")
    if type(asset_type) is not str or not asset_type:
        raise ValueError("asset_type must be a nonempty plain str")
    if type(start) is not date or type(end) is not date:
        raise ValueError("start and end must be plain dates")
    if start > end:
        raise ValueError("start must not be after end")


def _daily_chart_rows(payload: object, symbol: str, asset_type: str) -> tuple[DailyBar, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError("KIS daily-price response must be a JSON object")
    if payload.get("rt_cd") != "0":
        raise ValueError(f"KIS daily-price request failed ({payload.get('msg_cd', 'unknown')}): {payload.get('msg1', 'KIS rejected daily-price request')}")
    raw_rows = payload.get("output2")
    if not isinstance(raw_rows, list):
        raise ValueError("KIS daily-price response output2 must be an array")
    bars: list[DailyBar] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("KIS daily-price output2 rows must be objects")
        try:
            bars.append(DailyBar(
                trade_date=datetime.strptime(_required_string(raw, "stck_bsop_date"), "%Y%m%d").date(),
                symbol=symbol, asset_type=asset_type,
                open=Decimal(_required_string(raw, "stck_oprc")), high=Decimal(_required_string(raw, "stck_hgpr")),
                low=Decimal(_required_string(raw, "stck_lwpr")), close=Decimal(_required_string(raw, "stck_clpr")),
                volume=int(_required_string(raw, "acml_vol")), trading_value=Decimal(_required_string(raw, "acml_tr_pbmn")),
                is_tradable=True,
            ))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("KIS daily-price output2 contains an invalid OHLCV row") from exc
    return tuple(bars)


def _daily_index_chart_rows(payload: object) -> tuple[DailyBar, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError("KIS daily-index response must be a JSON object")
    if payload.get("rt_cd") != "0":
        raise ValueError(f"KIS daily-index request failed ({payload.get('msg_cd', 'unknown')}): {payload.get('msg1', 'KIS rejected daily-index request')}")
    raw_rows = payload.get("output2")
    if not isinstance(raw_rows, list):
        raise ValueError("KIS daily-index response output2 must be an array")
    bars: list[DailyBar] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("KIS daily-index output2 rows must be objects")
        try:
            bars.append(DailyBar(
                trade_date=datetime.strptime(_required_string(raw, "stck_bsop_date"), "%Y%m%d").date(),
                symbol=KOSPI_MARKET_SYMBOL, asset_type="INDEX",
                open=Decimal(_required_string(raw, "bstp_nmix_oprc")),
                high=Decimal(_required_string(raw, "bstp_nmix_hgpr")),
                low=Decimal(_required_string(raw, "bstp_nmix_lwpr")),
                close=Decimal(_required_string(raw, "bstp_nmix_prpr")),
                volume=int(_required_string(raw, "acml_vol")),
                trading_value=Decimal(_required_string(raw, "acml_tr_pbmn")),
                is_tradable=True,
            ))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("KIS daily-index output2 contains an invalid OHLCV row") from exc
    return tuple(bars)


def _required_string(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if type(value) is not str or not value:
        raise ValueError(f"KIS daily-price row is missing {field}")
    return value
