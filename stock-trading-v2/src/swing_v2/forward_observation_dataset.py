"""Assemble a strictly local, forward-observation KIS backtest dataset.

This module only reads immutable JSON artifacts.  It has no provider, token, dotenv,
or order-execution dependency.  The output is an immutable envelope containing a
strict ``DailyBarSnapshot`` payload plus input provenance; KRX metadata is recorded
only as a future selection reference and is expressly not historical membership.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import posixpath
import tempfile
from typing import Mapping, Sequence

from .backtest_data import DailyBarSnapshot, SnapshotMetadata
from .contracts import DailyBar


FORMAT_VERSION = 2
ARTIFACT_KIND = "forward-observation-kis-snapshot"
STOCK_SOURCE = "KIS OpenAPI domestic daily price (adjusted)"
KOSPI_SOURCE = "KIS OpenAPI domestic daily index chart (KOSPI code 0001)"
_BAR_FIELDS = {"trade_date", "symbol", "asset_type", "open", "high", "low", "close", "volume", "trading_value", "is_tradable"}


def assemble_forward_observation_dataset(
    *,
    stock_manifest_path: str | Path,
    kospi_artifact_path: str | Path,
    krx_universe_metadata_path: str | Path,
    requested_symbols: Sequence[str],
    requested_asset_types: Mapping[str, str],
    requested_start: date,
    requested_end: date,
    data_as_of: date,
    output_path: str | Path,
) -> DailyBarSnapshot:
    """Validate source artifacts then atomically create one immutable local dataset.

    ``data_as_of`` must be the common latest observed date, never a requested or
    metadata date.  Failure happens before the output is created or replaced.
    """
    _validate_request(requested_symbols, requested_asset_types, requested_start, requested_end, data_as_of, output_path)
    symbols = tuple(requested_symbols)
    # ``abspath`` is lexical only: preserve source-path provenance without
    # resolving symlinks, while always emitting an absolute POSIX/WSL path.
    manifest_file = _absolute_lexical_path(stock_manifest_path)
    kospi_file = _absolute_lexical_path(kospi_artifact_path)
    krx_file = _absolute_lexical_path(krx_universe_metadata_path)
    manifest_bytes, manifest = _read_json_object(manifest_file, "stock manifest")
    _validate_manifest(manifest, symbols, requested_start, requested_end)
    manifest_symbols = manifest["symbols"]
    assert isinstance(manifest_symbols, dict)  # established by _validate_manifest

    histories: dict[str, tuple[DailyBar, ...]] = {}
    stock_provenance: dict[str, dict[str, str]] = {}
    observed_ends: set[date] = set()
    for symbol in symbols:
        entry = manifest_symbols[symbol]
        assert isinstance(entry, dict)  # established by _validate_manifest
        payload_path = manifest_file.parent / entry["file"]
        if payload_path.parent != manifest_file.parent or payload_path.name != f"{symbol}.json":
            raise ValueError(f"stock manifest file identity is invalid for {symbol}")
        payload_bytes, payload = _read_json_object(payload_path, f"stock payload {symbol}")
        actual_hash = _sha256(payload_bytes)
        if actual_hash != entry["sha256"]:
            raise ValueError(f"stock payload sha256 mismatch for {symbol}")
        histories[symbol], observed_end = _validate_stock_payload(
            payload, symbol, requested_asset_types[symbol], requested_start, requested_end, entry,
        )
        observed_ends.add(observed_end)
        stock_provenance[symbol] = {"path": str(payload_path), "sha256": actual_hash}

    kospi_bytes, kospi = _read_json_object(kospi_file, "KOSPI artifact")
    market_history, kospi_observed_end = _validate_kospi_payload(kospi, requested_start, requested_end)
    observed_ends.add(kospi_observed_end)
    if observed_ends != {data_as_of}:
        raise ValueError("data_as_of must equal the common latest observed actual date")
    calendar = tuple(bar.trade_date for bar in market_history)
    calendar_dates = set(calendar)
    if any(bar.trade_date not in calendar_dates for bars in histories.values() for bar in bars):
        raise ValueError("stock history contains a date absent from KOSPI trade calendar")

    krx_bytes, krx = _read_json_object(krx_file, "KRX universe metadata")
    krx_as_of = _validate_krx_metadata(krx)
    snapshot = DailyBarSnapshot(
        metadata=SnapshotMetadata(
            source="KIS immutable artifacts assembled locally; KRX metadata is forward-observation only",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            data_as_of=data_as_of.isoformat(),
            trading_value_is_close_times_volume_proxy=False,
        ),
        market_symbol="KOSPI", asset_types=dict(requested_asset_types), trade_calendar=calendar,
        histories=histories, market_history=market_history,
    )
    output = Path(output_path)
    payload: dict[str, object] = {
        "artifact_kind": ARTIFACT_KIND,
        "format_version": FORMAT_VERSION,
        "data_as_of": data_as_of.isoformat(),
        "snapshot": _snapshot_json(snapshot),
        "provenance": {
            "stock_manifest": {"path": str(manifest_file), "sha256": _sha256(manifest_bytes)},
            "stock_payloads": stock_provenance,
            "kospi_artifact": {"path": str(kospi_file), "sha256": _sha256(kospi_bytes)},
        },
        "krx_universe_metadata": {
            "path": str(krx_file), "sha256": _sha256(krx_bytes), "as_of": krx_as_of.isoformat(),
            "lifecycle": "forward_observation_only",
            "historical_backtest_prohibition": "KRX metadata must not be used as historical classification or membership before its as_of date.",
        },
    }
    payload["integrity"] = {
        "algorithm": "sha256",
        "digest": _canonical_envelope_digest(payload),
    }
    _write_new_immutable_json(output, payload)
    return snapshot


def load_forward_observation_dataset(path: str | Path) -> DailyBarSnapshot:
    """Load and strictly validate an assembled envelope without any I/O beyond it."""
    _, raw = _read_json_object(Path(path), "forward-observation dataset")
    required = {"artifact_kind", "format_version", "data_as_of", "snapshot", "provenance", "krx_universe_metadata", "integrity"}
    if set(raw) != required or raw.get("artifact_kind") != ARTIFACT_KIND or raw.get("format_version") != FORMAT_VERSION:
        raise ValueError("invalid forward-observation dataset envelope; legacy formats are not approved")
    _validate_envelope_integrity(raw)
    as_of = _date(raw["data_as_of"], "dataset data_as_of")
    snapshot = _snapshot_from_json(raw["snapshot"])
    if snapshot.metadata.data_as_of != as_of.isoformat():
        raise ValueError("dataset and snapshot data_as_of mismatch")
    _validate_snapshot_final_observation(snapshot, as_of)
    _validate_output_provenance(raw["provenance"], raw["krx_universe_metadata"], snapshot)
    return snapshot


def _validate_snapshot_final_observation(snapshot: DailyBarSnapshot, data_as_of: date) -> None:
    if snapshot.trade_calendar[-1] != data_as_of or snapshot.market_history[-1].trade_date != data_as_of:
        raise ValueError("dataset data_as_of must equal every actual final market/calendar observation")
    if any(bars[-1].trade_date != data_as_of for bars in snapshot.histories.values()):
        raise ValueError("dataset data_as_of must equal every actual final stock observation")


def _validate_request(symbols: Sequence[str], asset_types: Mapping[str, str], start: date, end: date, as_of: date, output: str | Path) -> None:
    if type(start) is not date or type(end) is not date or type(as_of) is not date or start > end or end != as_of:
        raise ValueError("requested bounds must be plain dates with requested_end equal to data_as_of")
    values = tuple(symbols)
    if not 1 <= len(values) <= 3 or len(set(values)) != len(values):
        raise ValueError("requested_symbols must contain one to three unique symbols")
    if any(type(symbol) is not str or not symbol or symbol == "KOSPI" for symbol in values):
        raise ValueError("requested_symbols contain an invalid symbol")
    if not isinstance(asset_types, Mapping) or set(asset_types) != set(values):
        raise ValueError("requested_asset_types must contain exactly the requested symbols")
    if any(type(value) is not str or not value for value in asset_types.values()):
        raise ValueError("requested_asset_types contain an invalid asset type")
    output_path = Path(output)
    allowed_names = {
        f"forward-observation-v2-{as_of.isoformat()}.json",
        f"forward-observation-v3-{as_of.isoformat()}.json",
    }
    if output_path.name not in allowed_names:
        raise ValueError("output filename must name the explicit v2-schema data_as_of boundary")


def _validate_manifest(raw: object, symbols: tuple[str, ...], start: date, end: date) -> None:
    required = {"complete", "requested_end", "requested_start", "schema_version", "source", "symbols"}
    if not isinstance(raw, dict) or set(raw) != required or raw.get("schema_version") != 1 or raw.get("source") != STOCK_SOURCE or raw.get("complete") is not True:
        raise ValueError("stock manifest schema or source is invalid")
    if not isinstance(raw["symbols"], dict) or not set(symbols).issubset(raw["symbols"]):
        raise ValueError("stock manifest does not contain every requested symbol")
    _artifact_requested_bounds(raw, start, end, "stock manifest")
    for symbol in symbols:
        entry = raw["symbols"][symbol]
        required_entry = {"asset_type", "file", "observed_end", "observed_start", "requested_end", "requested_start", "sha256", "status", "symbol"}
        if not isinstance(entry, dict) or set(entry) != required_entry or entry.get("symbol") != symbol or entry.get("asset_type") != "STOCK" or entry.get("status") != "complete":
            raise ValueError(f"stock manifest identity is invalid for {symbol}")
        if type(entry["sha256"]) is not str or len(entry["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in entry["sha256"]):
            raise ValueError(f"stock manifest sha256 is invalid for {symbol}")
        _artifact_requested_bounds(entry, start, end, f"stock manifest entry {symbol}")


def _validate_stock_payload(raw: object, symbol: str, asset_type: str, start: date, end: date, entry: Mapping[str, object]) -> tuple[tuple[DailyBar, ...], date]:
    required = {"asset_type", "bars", "observed_end", "observed_start", "requested_end", "requested_start", "schema_version", "source", "symbol"}
    if not isinstance(raw, dict) or set(raw) != required or raw.get("schema_version") != 1 or raw.get("source") != STOCK_SOURCE or raw.get("symbol") != symbol or raw.get("asset_type") != asset_type:
        raise ValueError(f"stock payload schema, identity, or source is invalid for {symbol}")
    for field in ("requested_start", "requested_end", "observed_start", "observed_end"):
        if raw[field] != entry[field]:
            raise ValueError(f"stock payload and manifest disagree for {symbol}")
    _artifact_requested_bounds(raw, start, end, f"stock payload {symbol}")
    bars = _validate_bars(raw["bars"], symbol, asset_type, start, end, f"stock payload {symbol}")
    _validate_observed_bounds(raw, bars, f"stock payload {symbol}")
    return bars, bars[-1].trade_date


def _validate_kospi_payload(raw: object, start: date, end: date) -> tuple[tuple[DailyBar, ...], date]:
    required = {"asset_type", "bars", "index_code", "market_symbol", "observed_end", "observed_start", "requested_end", "requested_start", "schema_version", "source"}
    if not isinstance(raw, dict) or set(raw) != required or raw.get("schema_version") != 1 or raw.get("source") != KOSPI_SOURCE or raw.get("market_symbol") != "KOSPI" or raw.get("index_code") != "0001" or raw.get("asset_type") != "INDEX":
        raise ValueError("KOSPI artifact schema, identity, or source is invalid")
    _artifact_requested_bounds(raw, start, end, "KOSPI artifact")
    bars = _validate_bars(raw["bars"], "KOSPI", "INDEX", start, end, "KOSPI artifact")
    _validate_observed_bounds(raw, bars, "KOSPI artifact")
    return bars, bars[-1].trade_date


def _artifact_requested_bounds(raw: Mapping[str, object], start: date, end: date, label: str) -> None:
    requested_start = _date(raw["requested_start"], f"{label} requested_start")
    requested_end = _date(raw["requested_end"], f"{label} requested_end")
    if requested_start > start or requested_end < end:
        raise ValueError(f"{label} does not cover requested date bounds")


def _validate_observed_bounds(raw: Mapping[str, object], bars: tuple[DailyBar, ...], label: str) -> None:
    if _date(raw["observed_start"], f"{label} observed_start") != bars[0].trade_date or _date(raw["observed_end"], f"{label} observed_end") != bars[-1].trade_date:
        raise ValueError(f"{label} observed bounds do not match bars")


def _validate_bars(raw: object, symbol: str, asset_type: str, start: date, end: date, label: str) -> tuple[DailyBar, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{label} bars must be a nonempty array")
    bars: list[DailyBar] = []
    previous: date | None = None
    for item in raw:
        if not isinstance(item, dict) or set(item) != _BAR_FIELDS or item.get("symbol") != symbol or item.get("asset_type") != asset_type:
            raise ValueError(f"{label} bar schema or identity is invalid")
        if any(type(item[name]) is not str for name in ("trade_date", "open", "high", "low", "close", "trading_value")) or type(item["volume"]) is not int or type(item["is_tradable"]) is not bool:
            raise ValueError(f"{label} bar types are invalid")
        day = _date(item["trade_date"], f"{label} bar trade_date")
        if day < start or day > end or (previous is not None and previous >= day):
            raise ValueError(f"{label} dates must be sorted, unique, and requested-bounded")
        try:
            bar = DailyBar(day, symbol, asset_type, Decimal(item["open"]), Decimal(item["high"]), Decimal(item["low"]), Decimal(item["close"]), item["volume"], Decimal(item["trading_value"]), item["is_tradable"])
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{label} bar values are invalid") from exc
        bars.append(bar)
        previous = day
    return tuple(bars)


def _validate_krx_metadata(raw: object) -> date:
    if not isinstance(raw, dict) or set(raw) != {"format_version", "records"} or raw.get("format_version") != 1 or not isinstance(raw["records"], list) or not raw["records"]:
        raise ValueError("KRX universe metadata schema is invalid")
    as_of_values: set[date] = set()
    for record in raw["records"]:
        if not isinstance(record, dict) or set(record) != {"symbol", "asset_type", "effective_from", "effective_to", "flags", "etf_exposure", "source", "version", "content_hash", "as_of"}:
            raise ValueError("KRX universe metadata record schema is invalid")
        if type(record["symbol"]) is not str or type(record["asset_type"]) is not str or type(record["as_of"]) is not str or record["effective_from"] != record["as_of"]:
            raise ValueError("KRX universe metadata record identity is invalid")
        as_of_values.add(_date(record["as_of"], "KRX metadata as_of"))
    if len(as_of_values) != 1:
        raise ValueError("KRX universe metadata must have one as_of date")
    return as_of_values.pop()


def _snapshot_json(snapshot: DailyBarSnapshot) -> dict[str, object]:
    return {"format_version": 1, "metadata": {"source": snapshot.metadata.source, "retrieved_at": snapshot.metadata.retrieved_at, "data_as_of": snapshot.metadata.data_as_of, "trading_value_is_close_times_volume_proxy": snapshot.metadata.trading_value_is_close_times_volume_proxy}, "market_symbol": snapshot.market_symbol, "asset_types": dict(snapshot.asset_types), "trade_calendar": [day.isoformat() for day in snapshot.trade_calendar], "histories": {symbol: [_bar_json(bar) for bar in bars] for symbol, bars in snapshot.histories.items()}, "market_history": [_bar_json(bar) for bar in snapshot.market_history]}


def _snapshot_from_json(raw: object) -> DailyBarSnapshot:
    required = {"format_version", "metadata", "market_symbol", "asset_types", "trade_calendar", "histories", "market_history"}
    if not isinstance(raw, dict) or set(raw) != required or raw.get("format_version") != 1 or not isinstance(raw["metadata"], dict):
        raise ValueError("embedded snapshot schema is invalid")
    try:
        histories = {symbol: _validate_bars(bars, symbol, raw["asset_types"][symbol], date.min, date.max, "embedded snapshot") for symbol, bars in raw["histories"].items()}
        market = _validate_bars(raw["market_history"], raw["market_symbol"], "INDEX", date.min, date.max, "embedded market history")
        return DailyBarSnapshot(SnapshotMetadata(**raw["metadata"]), raw["market_symbol"], raw["asset_types"], tuple(_date(day, "embedded calendar") for day in raw["trade_calendar"]), histories, market)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("embedded snapshot is invalid") from exc


def _validate_output_provenance(provenance: object, krx: object, snapshot: DailyBarSnapshot) -> None:
    base_keys = {"stock_manifest", "stock_payloads", "kospi_artifact"}
    extension_keys = base_keys | {"extension_base"}
    if not isinstance(provenance, dict) or set(provenance) not in (base_keys, extension_keys) or not isinstance(krx, dict):
        raise ValueError("dataset provenance is invalid")
    _validate_provenance_reference(provenance["stock_manifest"], "stock manifest")
    _validate_provenance_reference(provenance["kospi_artifact"], "KOSPI artifact")
    if "extension_base" in provenance:
        _validate_provenance_reference(provenance["extension_base"], "extension base")
    stock_payloads = provenance["stock_payloads"]
    if not isinstance(stock_payloads, dict) or set(stock_payloads) != set(snapshot.asset_types):
        raise ValueError("dataset stock payload provenance is invalid")
    for symbol, reference in stock_payloads.items():
        if type(symbol) is not str:
            raise ValueError("dataset stock payload provenance symbol is invalid")
        _validate_provenance_reference(reference, f"stock payload {symbol}")
    required_krx = {"path", "sha256", "as_of", "lifecycle", "historical_backtest_prohibition"}
    if set(krx) != required_krx or krx["lifecycle"] != "forward_observation_only" or "must not be used" not in krx["historical_backtest_prohibition"]:
        raise ValueError("dataset KRX lifecycle is invalid")
    _validate_provenance_reference({"path": krx["path"], "sha256": krx["sha256"]}, "KRX metadata")
    _date(krx["as_of"], "dataset KRX as_of")


def _validate_provenance_reference(reference: object, label: str) -> None:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError(f"dataset {label} provenance is invalid")
    path, digest = reference["path"], reference["sha256"]
    if not _is_canonical_absolute_local_path(path) or type(digest) is not str or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"dataset {label} provenance path or sha256 is invalid")


def _is_canonical_absolute_local_path(path: object) -> bool:
    """Accept only lexical, canonical POSIX/WSL paths without filesystem access."""
    return (
        type(path) is str
        and "\x00" not in path
        and Path(path).is_absolute()
        and not path.startswith("//")
        and posixpath.normpath(path) == path
    )


def _absolute_lexical_path(path: str | Path) -> Path:
    """Make an input path canonical and absolute without resolving symlinks."""
    return Path(os.path.abspath(path))


def _validate_envelope_integrity(raw: Mapping[str, object]) -> None:
    integrity = raw["integrity"]
    if not isinstance(integrity, dict) or set(integrity) != {"algorithm", "digest"} or integrity.get("algorithm") != "sha256":
        raise ValueError("dataset integrity envelope is invalid")
    digest = integrity["digest"]
    if type(digest) is not str or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("dataset integrity digest is invalid")
    if digest != _canonical_envelope_digest(raw):
        raise ValueError("dataset integrity digest mismatch")


def _canonical_envelope_digest(raw: Mapping[str, object]) -> str:
    unsigned = {key: value for key, value in raw.items() if key != "integrity"}
    canonical = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _sha256(canonical)


def _bar_json(bar: DailyBar) -> dict[str, object]:
    return {"trade_date": bar.trade_date.isoformat(), "symbol": bar.symbol, "asset_type": bar.asset_type, "open": str(bar.open), "high": str(bar.high), "low": str(bar.low), "close": str(bar.close), "volume": bar.volume, "trading_value": str(bar.trading_value), "is_tradable": bar.is_tradable}


def _read_json_object(path: Path, label: str) -> tuple[bytes, dict[str, object]]:
    try:
        content = path.read_bytes()
        value = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be readable JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return content, value


def _write_new_immutable_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
    try:
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ValueError("immutable dataset output already exists") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _date(value: object, label: str) -> date:
    if type(value) is not str:
        raise ValueError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _sha256(content: bytes) -> str:
    return sha256(content).hexdigest()


def _parse_asset_type(value: str) -> tuple[str, str]:
    symbol, separator, asset_type = value.partition("=")
    if not separator or not symbol or not asset_type:
        raise argparse.ArgumentTypeError("asset type must be SYMBOL=TYPE")
    return symbol, asset_type


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a local-only KIS forward-observation snapshot.")
    parser.add_argument("--stock-manifest", required=True)
    parser.add_argument("--kospi-artifact", required=True)
    parser.add_argument("--krx-universe-metadata", required=True)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--asset-type", action="append", type=_parse_asset_type, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--data-as-of", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    assemble_forward_observation_dataset(stock_manifest_path=args.stock_manifest, kospi_artifact_path=args.kospi_artifact, krx_universe_metadata_path=args.krx_universe_metadata, requested_symbols=args.symbol, requested_asset_types=dict(args.asset_type), requested_start=_date(args.start, "start"), requested_end=_date(args.end, "end"), data_as_of=_date(args.data_as_of, "data_as_of"), output_path=args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
