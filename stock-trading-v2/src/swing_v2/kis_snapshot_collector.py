"""Resumable, rate-aware, read-only KIS domestic daily-bar snapshot collector."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import inspect
import json
from pathlib import Path
import re
import time
from typing import Protocol

from .contracts import DailyBar
from .kis import PageRequestBudget


SCHEMA_VERSION = 1
SOURCE = "KIS OpenAPI domestic daily price (adjusted)"


class DailyPriceClient(Protocol):
    def load_domestic_daily_bars(self, access_token: str, symbol: str, asset_type: str, start: date, end: date) -> Sequence[DailyBar]: ...


@dataclass(frozen=True)
class CollectionResult:
    complete: bool
    manifest_path: Path
    requested_symbols: tuple[str, ...]
    completed_symbols: tuple[str, ...]
    errors: Mapping[str, str]


def collect_daily_snapshot(*, client: DailyPriceClient, access_token: str, symbols: Sequence[str], asset_types: Mapping[str, str], start: date, end: date, output_path: str | Path, delay_seconds: float, max_symbols: int = 10, max_requests: int = 10, sleep: Callable[[float], None] = time.sleep) -> CollectionResult:
    """Collect one explicit, bounded set of daily bars; never calls account/order APIs."""
    _validate_request(client, access_token, symbols, asset_types, start, end, delay_seconds, max_symbols, max_requests, sleep)
    requested = tuple(symbols)
    if len(requested) > max_symbols:
        raise ValueError("requested symbols exceed max_symbols")
    root = Path(output_path)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    manifest = _load_manifest(manifest_path, requested, asset_types, start, end)
    completed: list[str] = []
    errors: dict[str, str] = {}
    requests_made = 0
    supports_page_limiter = _supports_page_limiter(client.load_domestic_daily_bars)
    page_budget = PageRequestBudget(max_requests=max_requests, delay_seconds=delay_seconds, sleep=sleep) if supports_page_limiter else None
    for symbol in requested:
        existing = manifest["symbols"].get(symbol)
        if _valid_completed_entry(root, existing, symbol, asset_types[symbol], start, end):
            completed.append(symbol)
            continue
        orphan = _recover_orphan_entry(root, symbol, asset_types[symbol], start, end)
        if orphan is not None:
            manifest["symbols"][symbol] = orphan
            completed.append(symbol)
            _write_json(manifest_path, manifest)
            continue
        if _symbol_path(root, symbol).exists():
            message = "refusing to overwrite immutable partial snapshot"
            errors[symbol] = message
            manifest["symbols"][symbol] = _error_entry(symbol, asset_types[symbol], start, end, message)
            _write_json(manifest_path, manifest)
            continue
        if not supports_page_limiter and requests_made >= max_requests:
            errors[symbol] = "request cap reached"
            manifest["symbols"][symbol] = _error_entry(symbol, asset_types[symbol], start, end, errors[symbol])
            continue
        if not supports_page_limiter and requests_made:
            sleep(delay_seconds)
        if not supports_page_limiter:
            requests_made += 1
        try:
            if page_budget is None:
                bars = tuple(client.load_domestic_daily_bars(access_token, symbol, asset_types[symbol], start, end))
            else:
                bars = tuple(client.load_domestic_daily_bars(access_token, symbol, asset_types[symbol], start, end, page_request_limiter=page_budget))
            _validate_bars(bars, symbol, asset_types[symbol], start, end)
            entry = _write_symbol(root, symbol, asset_types[symbol], start, end, bars)
            manifest["symbols"][symbol] = entry
            completed.append(symbol)
        except Exception as exc:  # source failures are recorded and do not make a snapshot complete
            message = f"{type(exc).__name__}: {exc}"
            errors[symbol] = message
            manifest["symbols"][symbol] = _error_entry(symbol, asset_types[symbol], start, end, message)
        _write_json(manifest_path, manifest)
    manifest["complete"] = len(completed) == len(requested) and not errors
    _write_json(manifest_path, manifest)
    return CollectionResult(manifest["complete"], manifest_path, requested, tuple(completed), errors)


def _validate_request(client: object, access_token: object, symbols: Sequence[str], asset_types: Mapping[str, str], start: object, end: object, delay_seconds: object, max_symbols: object, max_requests: object, sleep: object) -> None:
    if not hasattr(client, "load_domestic_daily_bars") or not callable(client.load_domestic_daily_bars):
        raise ValueError("client must provide load_domestic_daily_bars")
    if type(access_token) is not str or not access_token:
        raise ValueError("access_token must be a nonempty plain str")
    _validate_snapshot_request(symbols, asset_types, start, end, delay_seconds, max_symbols, max_requests, sleep)


def _validate_snapshot_request(symbols: Sequence[str], asset_types: Mapping[str, str], start: object, end: object, delay_seconds: object, max_symbols: object, max_requests: object, sleep: object) -> None:
    """Validate the complete collection identity without credentials or a client."""
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("start and end must be plain dates with start not after end")
    if type(delay_seconds) not in (int, float) or delay_seconds <= 0:
        raise ValueError("delay_seconds must be nonzero and positive")
    if type(max_symbols) is not int or max_symbols <= 0 or type(max_requests) is not int or max_requests <= 0:
        raise ValueError("max_symbols and max_requests must be positive ints")
    if not callable(sleep):
        raise ValueError("sleep must be callable")
    if not symbols or len(symbols) != len(set(symbols)) or any(type(s) is not str or not s for s in symbols):
        raise ValueError("symbols must be nonempty unique plain strings")
    if any(re.fullmatch(r"[A-Z0-9]{6}", symbol) is None for symbol in symbols):
        raise ValueError("symbols must be six-character uppercase alphanumeric KRX codes")
    if set(asset_types) != set(symbols) or any(type(v) is not str or not v for v in asset_types.values()):
        raise ValueError("asset_types must exactly map requested symbols to nonempty strings")


def _preflight_existing_collection(output_path: Path, symbols: tuple[str, ...], asset_types: Mapping[str, str], start: date, end: date) -> CollectionResult | None:
    """Return an immutable completed result or reject any existing collection state.

    This deliberately runs before configuration, token-cache access, or client construction.
    """
    if output_path.exists() and not output_path.is_dir():
        raise ValueError("output path must be a directory")
    if not output_path.exists():
        return None
    manifest_path = output_path / "manifest.json"
    temporary_names = {
        _temporary_path(manifest_path).name,
        *(_temporary_path(_symbol_path(output_path, symbol)).name for symbol in symbols),
    }
    if any(path.name in temporary_names for path in output_path.iterdir()):
        return CollectionResult(False, manifest_path, symbols, (), {
            "collection": "incomplete atomic temporary collection artifact exists",
        })
    artifact_paths = tuple(path for path in output_path.iterdir() if path.name == "manifest.json" or path.suffix == ".json")
    if not artifact_paths:  # An empty output directory is not a resume collision.
        return None
    try:
        if not manifest_path.is_file():
            raise ValueError("existing collection artifacts are missing manifest.json")
        manifest = _load_manifest(manifest_path, symbols, asset_types, start, end)
        entries = manifest["symbols"]
        if not isinstance(entries, dict):  # _load_manifest guarantees this; keep the boundary explicit.
            raise ValueError("existing manifest symbols are invalid")
        expected_files = {"manifest.json", *(f"{symbol}.json" for symbol in symbols)}
        actual_files = {path.name for path in artifact_paths}
        if manifest.get("complete") is not True or set(entries) != set(symbols) or actual_files != expected_files:
            raise ValueError("existing collection does not exactly match the requested completed artifacts")
        if not all(_valid_completed_entry(output_path, entries[symbol], symbol, asset_types[symbol], start, end) for symbol in symbols):
            raise ValueError("existing collection contains an invalid immutable artifact")
    except (OSError, ValueError, TypeError) as exc:
        return CollectionResult(False, manifest_path, symbols, (), {"collection": f"invalid immutable collection: {exc}"})
    return CollectionResult(True, manifest_path, symbols, symbols, {})


def _supports_page_limiter(loader: Callable[..., object]) -> bool:
    """Keep existing third-party/legacy client adapters callable without new kwargs."""
    parameters = inspect.signature(loader).parameters.values()
    return any(parameter.name == "page_request_limiter" or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)


def _load_manifest(path: Path, symbols: tuple[str, ...], asset_types: Mapping[str, str], start: date, end: date) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "source": SOURCE, "requested_start": start.isoformat(), "requested_end": end.isoformat(), "symbols": {}, "complete": False}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("existing manifest is not readable JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("source") != SOURCE or manifest.get("requested_start") != start.isoformat() or manifest.get("requested_end") != end.isoformat() or not isinstance(manifest.get("symbols"), dict):
        raise ValueError("existing manifest does not match this collection request")
    return manifest


def _valid_completed_entry(root: Path, entry: object, symbol: str, asset_type: str, start: date, end: date) -> bool:
    if not isinstance(entry, dict) or entry.get("status") != "complete" or entry.get("symbol") != symbol or entry.get("asset_type") != asset_type or entry.get("requested_start") != start.isoformat() or entry.get("requested_end") != end.isoformat():
        return False
    filename, digest = entry.get("file"), entry.get("sha256")
    if type(filename) is not str or type(digest) is not str or filename != f"{symbol}.json":
        return False
    path = _symbol_path(root, symbol)
    if not path.is_file() or _sha256(path) != digest:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return _valid_snapshot_payload(payload, symbol, asset_type, start, end) and entry.get("observed_start") == payload["observed_start"] and entry.get("observed_end") == payload["observed_end"]


def _write_symbol(root: Path, symbol: str, asset_type: str, start: date, end: date, bars: tuple[DailyBar, ...]) -> dict[str, object]:
    filename = f"{symbol}.json"
    path = _symbol_path(root, symbol)
    payload = {"schema_version": SCHEMA_VERSION, "source": SOURCE, "symbol": symbol, "asset_type": asset_type, "requested_start": start.isoformat(), "requested_end": end.isoformat(), "observed_start": bars[0].trade_date.isoformat() if bars else None, "observed_end": bars[-1].trade_date.isoformat() if bars else None, "bars": [_bar_json(bar) for bar in bars]}
    if path.exists():
        raise ValueError(f"refusing to overwrite immutable partial snapshot: {path}")
    _write_json(path, payload)
    return {"status": "complete", "symbol": symbol, "asset_type": asset_type, "file": filename, "sha256": _sha256(path), "requested_start": start.isoformat(), "requested_end": end.isoformat(), "observed_start": payload["observed_start"], "observed_end": payload["observed_end"]}


def _recover_orphan_entry(root: Path, symbol: str, asset_type: str, start: date, end: date) -> dict[str, object] | None:
    """Rebuild a missing manifest entry only from a verified immutable symbol file."""
    path = _symbol_path(root, symbol)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _valid_snapshot_payload(payload, symbol, asset_type, start, end):
        return None
    return {"status": "complete", "symbol": symbol, "asset_type": asset_type, "file": path.name, "sha256": _sha256(path), "requested_start": start.isoformat(), "requested_end": end.isoformat(), "observed_start": payload.get("observed_start"), "observed_end": payload.get("observed_end")}


def _symbol_path(root: Path, symbol: str) -> Path:
    """Return the canonical child path, rejecting any escape even after validation."""
    resolved_root = root.resolve()
    path = (resolved_root / f"{symbol}.json").resolve()
    if path.parent != resolved_root:
        raise ValueError("symbol snapshot path escapes output directory")
    return path


def _validate_bars(bars: tuple[DailyBar, ...], symbol: str, asset_type: str, start: date, end: date) -> None:
    dates: list[date] = []
    for item in bars:
        if not isinstance(item, DailyBar) or item.symbol != symbol or item.asset_type != asset_type or not start <= item.trade_date <= end:
            raise ValueError("source returned malformed or out-of-range daily bars")
        dates.append(item.trade_date)
    if dates != sorted(set(dates)):
        raise ValueError("source returned unordered or duplicate daily bars")


def _valid_snapshot_payload(payload: object, symbol: str, asset_type: str, start: date, end: date) -> bool:
    """Validate an immutable on-disk payload before it can be resumed or promoted."""
    if type(payload) is not dict or set(payload) != {
        "schema_version", "source", "symbol", "asset_type", "requested_start", "requested_end",
        "observed_start", "observed_end", "bars",
    }:
        return False
    if (
        type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION
        or type(payload["source"]) is not str or payload["source"] != SOURCE
        or type(payload["symbol"]) is not str or payload["symbol"] != symbol
        or type(payload["asset_type"]) is not str or payload["asset_type"] != asset_type
        or type(payload["requested_start"]) is not str or payload["requested_start"] != start.isoformat()
        or type(payload["requested_end"]) is not str or payload["requested_end"] != end.isoformat()
        or type(payload["bars"]) is not list
    ):
        return False
    try:
        bars = tuple(_daily_bar_from_json(raw, symbol, asset_type) for raw in payload["bars"])
        _validate_bars(bars, symbol, asset_type, start, end)
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return False
    observed_start = bars[0].trade_date.isoformat() if bars else None
    observed_end = bars[-1].trade_date.isoformat() if bars else None
    return payload["observed_start"] == observed_start and payload["observed_end"] == observed_end


def _daily_bar_from_json(raw: object, symbol: str, asset_type: str) -> DailyBar:
    if type(raw) is not dict or set(raw) != {
        "trade_date", "symbol", "asset_type", "open", "high", "low", "close", "volume", "trading_value", "is_tradable",
    }:
        raise ValueError("stored bar has an invalid schema")
    if type(raw["trade_date"]) is not str:
        raise ValueError("stored bar trade_date must be a plain ISO date")
    trade_date = date.fromisoformat(raw["trade_date"])
    if trade_date.isoformat() != raw["trade_date"]:
        raise ValueError("stored bar trade_date must be a plain ISO date")
    decimal_fields = ("open", "high", "low", "close", "trading_value")
    if (
        type(raw["symbol"]) is not str or raw["symbol"] != symbol
        or type(raw["asset_type"]) is not str or raw["asset_type"] != asset_type
        or any(type(raw[field]) is not str for field in decimal_fields)
        or type(raw["volume"]) is not int or type(raw["is_tradable"]) is not bool
    ):
        raise ValueError("stored bar has invalid field types or identity")
    return DailyBar(
        trade_date=trade_date, symbol=raw["symbol"], asset_type=raw["asset_type"],
        open=Decimal(raw["open"]), high=Decimal(raw["high"]), low=Decimal(raw["low"]),
        close=Decimal(raw["close"]), volume=raw["volume"], trading_value=Decimal(raw["trading_value"]),
        is_tradable=raw["is_tradable"],
    )


def _bar_json(bar: DailyBar) -> dict[str, object]:
    return {"trade_date": bar.trade_date.isoformat(), "symbol": bar.symbol, "asset_type": bar.asset_type, "open": str(bar.open), "high": str(bar.high), "low": str(bar.low), "close": str(bar.close), "volume": bar.volume, "trading_value": str(bar.trading_value), "is_tradable": bar.is_tradable}


def _error_entry(symbol: str, asset_type: str, start: date, end: date, error: str) -> dict[str, object]:
    return {"status": "error", "symbol": symbol, "asset_type": asset_type, "requested_start": start.isoformat(), "requested_end": end.isoformat(), "error": error}


def _temporary_path(path: Path) -> Path:
    """Return the exact sibling used by atomic JSON writes (for example, ``manifest.json.tmp``)."""
    return path.with_suffix(path.suffix + ".tmp")


def _write_json(path: Path, payload: object) -> None:
    temporary = _temporary_path(path)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    """Run a strictly explicit local collection using only KIS app credentials from .env."""
    parser = argparse.ArgumentParser(description="Collect a resumable read-only KIS daily-bar snapshot.")
    parser.add_argument("--symbol", action="append", required=True, metavar="CODE:TYPE", help="Explicit symbol and asset type; repeatable.")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument("--max-symbols", type=int, default=10)
    parser.add_argument("--max-requests", type=int, default=10)
    args = parser.parse_args(argv)
    pairs = tuple(args.symbol)
    try:
        parsed = tuple(pair.split(":", 1) for pair in pairs)
        if any(len(pair) != 2 or not pair[0] or not pair[1] for pair in parsed):
            raise ValueError
    except ValueError:
        parser.error("--symbol must be CODE:TYPE")
    symbols = tuple(symbol for symbol, _ in parsed)
    asset_types = dict(parsed)
    try:
        _validate_snapshot_request(symbols, asset_types, args.start, args.end, args.delay_seconds, args.max_symbols, args.max_requests, time.sleep)
        preflight = _preflight_existing_collection(args.output, symbols, asset_types, args.start, args.end)
    except ValueError as exc:
        parser.error(str(exc))
    if preflight is not None:
        print(json.dumps({"complete": preflight.complete, "manifest": str(preflight.manifest_path), "completed_symbols": preflight.completed_symbols, "errors": preflight.errors}, ensure_ascii=False))
        return 0 if preflight.complete else 2
    from dotenv import load_dotenv
    import os
    from .kis import KisClient, KisCredentials
    load_dotenv()
    app_key, app_secret = os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        parser.error("KIS_APP_KEY and KIS_APP_SECRET must be set in the environment or .env")
    client = KisClient(credentials=KisCredentials(app_key=app_key, app_secret=app_secret))
    token_cache = os.getenv("KIS_TOKEN_CACHE")
    result = collect_daily_snapshot(client=client, access_token=client.get_access_token(cache_path=Path(token_cache) if token_cache else None), symbols=symbols, asset_types=asset_types, start=args.start, end=args.end, output_path=args.output, delay_seconds=args.delay_seconds, max_symbols=args.max_symbols, max_requests=args.max_requests)
    print(json.dumps({"complete": result.complete, "manifest": str(result.manifest_path), "completed_symbols": result.completed_symbols, "errors": result.errors}, ensure_ascii=False))
    return 0 if result.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
