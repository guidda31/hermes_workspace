"""Local-only immutable one-session extensions for forward-observation datasets.

This module deliberately has no provider, credentials, dotenv, or order dependency.
It accepts already-collected local KIS JSON artifacts and creates a distinct v3
forward-observation envelope only after all input integrity and date checks pass.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

from .backtest_data import DailyBarSnapshot, SnapshotMetadata
from .contracts import DailyBar
from .forward_observation_dataset import (
    ARTIFACT_KIND,
    FORMAT_VERSION,
    _absolute_lexical_path,
    _bar_json,
    _canonical_envelope_digest,
    _date,
    _read_json_object,
    _snapshot_json,
    _validate_kospi_payload,
    _validate_manifest,
    _validate_stock_payload,
    _write_new_immutable_json,
    load_forward_observation_dataset,
)


def assemble_one_session_extension(
    *,
    base_dataset_path: str | Path,
    stock_manifest_path: str | Path,
    kospi_artifact_path: str | Path,
    data_as_of: date,
    output_path: str | Path,
) -> DailyBarSnapshot:
    """Append exactly one validated local session to a validated immutable v3 base.

    The destination must be the explicit ``forward-observation-v3-YYYY-MM-DD.json``
    filename for the appended actual date.  It is created once and never replaced.
    """
    if type(data_as_of) is not date:
        raise ValueError("data_as_of must be a plain date")
    output = Path(output_path)
    if output.name != f"forward-observation-v3-{data_as_of.isoformat()}.json":
        raise ValueError("output filename must name the explicit v3 data_as_of boundary")

    base_file = _absolute_lexical_path(base_dataset_path)
    if base_file.is_symlink():
        raise ValueError("base dataset must not be a symlink")
    base = load_forward_observation_dataset(base_file)
    base_as_of = _date(base.metadata.data_as_of, "base dataset data_as_of")
    if base_file.name != f"forward-observation-v3-{base_as_of.isoformat()}.json":
        raise ValueError("base dataset must use the canonical approved v3 base filename")
    base_bytes = base_file.read_bytes()
    if data_as_of <= base_as_of:
        raise ValueError("extension date must be strictly later than base data_as_of")

    symbols = tuple(base.asset_types)
    manifest_file = _absolute_lexical_path(stock_manifest_path)
    manifest_bytes, manifest = _read_json_object(manifest_file, "stock manifest")
    _validate_exact_one_session_manifest(manifest, symbols, data_as_of)
    _validate_manifest(manifest, symbols, data_as_of, data_as_of)
    entries = manifest["symbols"]
    assert isinstance(entries, dict)

    appended_histories: dict[str, DailyBar] = {}
    stock_provenance: dict[str, dict[str, str]] = {}
    for symbol in symbols:
        entry = entries[symbol]
        assert isinstance(entry, dict)
        payload_path = manifest_file.parent / entry["file"]
        if payload_path.parent != manifest_file.parent or payload_path.name != f"{symbol}.json":
            raise ValueError(f"stock manifest file identity is invalid for {symbol}")
        payload_bytes, payload = _read_json_object(payload_path, f"stock payload {symbol}")
        digest = sha256(payload_bytes).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"stock payload sha256 mismatch for {symbol}")
        bars, observed_end = _validate_stock_payload(
            payload, symbol, base.asset_types[symbol], data_as_of, data_as_of, entry,
        )
        if len(bars) != 1 or observed_end != data_as_of or bars[0].trade_date != data_as_of:
            raise ValueError(f"stock payload must contain exactly the extension date for {symbol}")
        appended_histories[symbol] = bars[0]
        stock_provenance[symbol] = {"path": str(payload_path), "sha256": digest}

    kospi_file = _absolute_lexical_path(kospi_artifact_path)
    kospi_bytes, kospi = _read_json_object(kospi_file, "KOSPI artifact")
    _validate_exact_one_session_artifact(kospi, data_as_of, "KOSPI artifact")
    market_bars, observed_end = _validate_kospi_payload(kospi, data_as_of, data_as_of)
    if len(market_bars) != 1 or observed_end != data_as_of or market_bars[0].trade_date != data_as_of:
        raise ValueError("KOSPI artifact must contain exactly the extension date")

    histories = {symbol: (*base.histories[symbol], appended_histories[symbol]) for symbol in symbols}
    snapshot = DailyBarSnapshot(
        metadata=SnapshotMetadata(
            source=base.metadata.source,
            retrieved_at=base.metadata.retrieved_at,
            data_as_of=data_as_of.isoformat(),
            trading_value_is_close_times_volume_proxy=base.metadata.trading_value_is_close_times_volume_proxy,
        ),
        market_symbol=base.market_symbol,
        asset_types=dict(base.asset_types),
        trade_calendar=(*base.trade_calendar, data_as_of),
        histories=histories,
        market_history=(*base.market_history, market_bars[0]),
    )
    payload: dict[str, object] = {
        "artifact_kind": ARTIFACT_KIND,
        "format_version": FORMAT_VERSION,
        "data_as_of": data_as_of.isoformat(),
        "snapshot": _snapshot_json(snapshot),
        "provenance": {
            "stock_manifest": {"path": str(manifest_file), "sha256": sha256(manifest_bytes).hexdigest()},
            "stock_payloads": stock_provenance,
            "kospi_artifact": {"path": str(kospi_file), "sha256": sha256(kospi_bytes).hexdigest()},
            "extension_base": {"path": str(base_file), "sha256": sha256(base_bytes).hexdigest()},
        },
        "krx_universe_metadata": _base_krx_metadata(base_file),
    }
    payload["integrity"] = {"algorithm": "sha256", "digest": _canonical_envelope_digest(payload)}
    _write_new_immutable_json(output, payload)
    return snapshot


def _base_krx_metadata(base_file: Path) -> dict[str, object]:
    """Copy validated metadata from the base envelope without dereferencing provenance."""
    _, raw = _read_json_object(base_file, "forward-observation dataset")
    value = raw["krx_universe_metadata"]
    assert isinstance(value, dict)
    return dict(value)


def _validate_exact_one_session_manifest(raw: object, symbols: tuple[str, ...], day: date) -> None:
    if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), dict):
        raise ValueError("stock manifest schema is invalid")
    if set(raw["symbols"]) != set(symbols):
        raise ValueError("stock manifest symbols must exactly match the base dataset")
    expected = day.isoformat()
    if raw.get("requested_start") != expected or raw.get("requested_end") != expected:
        raise ValueError("stock manifest must be exact for the extension date")
    for symbol in symbols:
        entry = raw["symbols"][symbol]
        _validate_exact_one_session_artifact(entry, day, f"stock manifest entry {symbol}")


def _validate_exact_one_session_artifact(raw: object, day: date, label: str) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} schema is invalid")
    expected = day.isoformat()
    for field in ("requested_start", "requested_end", "observed_start", "observed_end"):
        if raw.get(field) != expected:
            raise ValueError(f"{label} must be exact for the extension date")
