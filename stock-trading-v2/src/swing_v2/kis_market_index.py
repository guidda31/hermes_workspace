"""Bounded, immutable KOSPI daily-index acquisition for local backtest snapshots.

KOSPI is a market-risk signal only.  This module intentionally has no universe,
security, ETF, management, or halt classification behavior.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import time
from typing import Protocol

from .contracts import DailyBar
from .kis import KOSPI_INDEX_CODE, KOSPI_MARKET_SYMBOL, PageRequestBudget


SCHEMA_VERSION = 1
SOURCE = "KIS OpenAPI domestic daily index chart (KOSPI code 0001)"
_FILENAME = "KOSPI.json"


class KospiDailyIndexClient(Protocol):
    def load_kospi_daily_bars(
        self, access_token: str, start: date, end: date, *, page_request_limiter: PageRequestBudget
    ) -> Sequence[DailyBar]: ...


@dataclass(frozen=True)
class MarketIndexCollectionResult:
    complete: bool
    path: Path
    sha256: str | None
    observed_start: date | None
    observed_end: date | None
    error: str | None


def collect_kospi_market_index_snapshot(
    *, client: KospiDailyIndexClient, access_token: str, start: date, end: date,
    output_path: str | Path, delay_seconds: float, max_requests: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> MarketIndexCollectionResult:
    """Collect exactly one bounded KOSPI series without overwrite or trading APIs.

    The resulting ``KOSPI.json`` is independently loadable with
    :func:`load_kospi_market_index_snapshot` and can be passed as ``market_history``
    to ``build_snapshot_from_kis`` when a stock snapshot is assembled.
    """
    _validate_request(client, access_token, start, end, delay_seconds, max_requests, sleep)
    root = Path(output_path)
    root.mkdir(parents=True, exist_ok=True)
    path = _artifact_path(root)
    if path.exists():
        return MarketIndexCollectionResult(False, path, None, None, None, "refusing to overwrite immutable KOSPI artifact")
    limiter = PageRequestBudget(max_requests=max_requests, delay_seconds=delay_seconds, sleep=sleep)
    try:
        bars = tuple(client.load_kospi_daily_bars(access_token, start, end, page_request_limiter=limiter))
        _validate_bars(bars, start, end)
        payload = _payload(bars, start, end)
        _write_json(path, payload)
    except Exception as exc:
        return MarketIndexCollectionResult(False, path, None, None, None, f"{type(exc).__name__}: {exc}")
    return MarketIndexCollectionResult(
        True, path, _sha256(path), bars[0].trade_date if bars else None,
        bars[-1].trade_date if bars else None, None,
    )


def load_kospi_market_index_snapshot(
    path: str | Path, *, expected_start: date | None = None, expected_end: date | None = None,
) -> tuple[DailyBar, ...]:
    """Load only a canonical immutable KOSPI artifact; reject malformed input.

    Optional expected bounds bind a resumed artifact to the exact collection request.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("KOSPI market-index artifact must be readable JSON") from exc
    if type(payload) is not dict or set(payload) != {
        "schema_version", "source", "market_symbol", "index_code", "asset_type",
        "requested_start", "requested_end", "observed_start", "observed_end", "bars",
    }:
        raise ValueError("KOSPI market-index artifact fields do not match the canonical format")
    if (
        payload["schema_version"] != SCHEMA_VERSION or payload["source"] != SOURCE
        or payload["market_symbol"] != KOSPI_MARKET_SYMBOL or payload["index_code"] != KOSPI_INDEX_CODE
        or payload["asset_type"] != "INDEX" or type(payload["requested_start"]) is not str
        or type(payload["requested_end"]) is not str or type(payload["bars"]) is not list
    ):
        raise ValueError("KOSPI market-index artifact identity is invalid")
    try:
        start, end = date.fromisoformat(payload["requested_start"]), date.fromisoformat(payload["requested_end"])
        if start > end:
            raise ValueError("invalid requested bounds")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("KOSPI market-index artifact request bounds are invalid") from exc
    if (expected_start is not None and start != expected_start) or (expected_end is not None and end != expected_end):
        raise ValueError("artifact request bounds do not match this collection request")
    try:
        bars = tuple(_bar_from_json(raw) for raw in payload["bars"])
        _validate_bars(bars, start, end)
    except (ArithmeticError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError("KOSPI market-index artifact bars are invalid") from exc
    observed_start = bars[0].trade_date.isoformat() if bars else None
    observed_end = bars[-1].trade_date.isoformat() if bars else None
    if payload["observed_start"] != observed_start or payload["observed_end"] != observed_end:
        raise ValueError("KOSPI market-index artifact observed bounds are invalid")
    return bars


def _validate_request(client: object, access_token: object, start: object, end: object, delay_seconds: object, max_requests: object, sleep: object) -> None:
    if not hasattr(client, "load_kospi_daily_bars") or not callable(client.load_kospi_daily_bars):
        raise ValueError("client must provide load_kospi_daily_bars")
    if type(access_token) is not str or not access_token:
        raise ValueError("access_token must be a nonempty plain str")
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("start and end must be plain dates with start not after end")
    if type(delay_seconds) not in (int, float) or delay_seconds <= 0:
        raise ValueError("delay_seconds must be nonzero and positive")
    if type(max_requests) is not int or max_requests <= 0:
        raise ValueError("max_requests must be a positive int")
    if not callable(sleep):
        raise ValueError("sleep must be callable")


def _validate_bars(bars: tuple[DailyBar, ...], start: date, end: date) -> None:
    dates: list[date] = []
    for bar in bars:
        if (
            not isinstance(bar, DailyBar) or bar.symbol != KOSPI_MARKET_SYMBOL
            or bar.asset_type != "INDEX" or not start <= bar.trade_date <= end
        ):
            raise ValueError("source returned malformed or out-of-range KOSPI daily bars")
        dates.append(bar.trade_date)
    if dates != sorted(set(dates)):
        raise ValueError("source returned unordered or duplicate KOSPI daily bars")


def _payload(bars: tuple[DailyBar, ...], start: date, end: date) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION, "source": SOURCE,
        "market_symbol": KOSPI_MARKET_SYMBOL, "index_code": KOSPI_INDEX_CODE,
        "asset_type": "INDEX", "requested_start": start.isoformat(), "requested_end": end.isoformat(),
        "observed_start": bars[0].trade_date.isoformat() if bars else None,
        "observed_end": bars[-1].trade_date.isoformat() if bars else None,
        "bars": [_bar_to_json(bar) for bar in bars],
    }


def _artifact_path(root: Path) -> Path:
    resolved_root = root.resolve()
    path = (resolved_root / _FILENAME).resolve()
    if path.parent != resolved_root:
        raise ValueError("KOSPI artifact path escapes output directory")
    return path


def _bar_to_json(bar: DailyBar) -> dict[str, object]:
    return {
        "trade_date": bar.trade_date.isoformat(), "symbol": bar.symbol, "asset_type": bar.asset_type,
        "open": str(bar.open), "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
        "volume": bar.volume, "trading_value": str(bar.trading_value), "is_tradable": bar.is_tradable,
    }


def _bar_from_json(raw: object) -> DailyBar:
    if type(raw) is not dict or set(raw) != {
        "trade_date", "symbol", "asset_type", "open", "high", "low", "close", "volume", "trading_value", "is_tradable",
    }:
        raise ValueError("stored KOSPI bar has an invalid schema")
    if any(type(raw[field]) is not str for field in ("trade_date", "symbol", "asset_type", "open", "high", "low", "close", "trading_value")) or type(raw["volume"]) is not int or type(raw["is_tradable"]) is not bool:
        raise ValueError("stored KOSPI bar has invalid field types")
    return DailyBar(
        trade_date=date.fromisoformat(raw["trade_date"]), symbol=raw["symbol"], asset_type=raw["asset_type"],
        open=Decimal(raw["open"]), high=Decimal(raw["high"]), low=Decimal(raw["low"]), close=Decimal(raw["close"]),
        volume=raw["volume"], trading_value=Decimal(raw["trading_value"]), is_tradable=raw["is_tradable"],
    )


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicitly bounded KOSPI-only read-only collection from .env."""
    parser = argparse.ArgumentParser(description="Collect one immutable KIS KOSPI daily-index artifact.")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path, help="Output directory; artifact is written as KOSPI.json")
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument("--max-requests", type=int, default=3)
    args = parser.parse_args(argv)
    path = _artifact_path(args.output)
    if path.exists():
        try:
            bars = load_kospi_market_index_snapshot(path, expected_start=args.start, expected_end=args.end)
        except ValueError as exc:
            result = MarketIndexCollectionResult(False, path, None, None, None, f"invalid immutable KOSPI artifact: {exc}")
        else:
            result = MarketIndexCollectionResult(
                True, path, _sha256(path), bars[0].trade_date if bars else None,
                bars[-1].trade_date if bars else None, None,
            )
    else:
        from dotenv import load_dotenv
        import os
        from .kis import KisClient, KisCredentials
        load_dotenv()
        app_key, app_secret = os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")
        if not app_key or not app_secret:
            parser.error("KIS_APP_KEY and KIS_APP_SECRET must be set in the environment or .env")
        client = KisClient(credentials=KisCredentials(app_key=app_key, app_secret=app_secret))
        token_cache = os.getenv("KIS_TOKEN_CACHE")
        result = collect_kospi_market_index_snapshot(
            client=client, access_token=client.get_access_token(cache_path=Path(token_cache) if token_cache else None), start=args.start, end=args.end,
            output_path=args.output, delay_seconds=args.delay_seconds, max_requests=args.max_requests,
        )
    print(json.dumps({
        "complete": result.complete, "path": str(result.path), "sha256": result.sha256,
        "observed_start": result.observed_start.isoformat() if result.observed_start else None,
        "observed_end": result.observed_end.isoformat() if result.observed_end else None,
        "error": result.error,
    }, ensure_ascii=False))
    return 0 if result.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
