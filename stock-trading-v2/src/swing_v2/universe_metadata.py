"""Point-in-time, provenance-backed KRX universe classification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
import csv
import json
from pathlib import Path
import re
from collections.abc import Mapping
from typing import Sequence


class AssetType(str, Enum):
    UNKNOWN = "UNKNOWN"
    STOCK = "STOCK"
    ETF = "ETF"
    PREFERRED = "PREFERRED"
    SPAC = "SPAC"
    ETN = "ETN"


class ClassificationFlag(str, Enum):
    MANAGEMENT_ISSUE = "MANAGEMENT_ISSUE"
    TRADING_HALTED = "TRADING_HALTED"
    ETF_LEVERAGED = "ETF_LEVERAGED"
    ETF_INVERSE = "ETF_INVERSE"
    ETF_FOREIGN_INDEX = "ETF_FOREIGN_INDEX"


class EtfExposure(str, Enum):
    DOMESTIC_INDEX_OR_SECTOR = "DOMESTIC_INDEX_OR_SECTOR"


@dataclass(frozen=True)
class MetadataProvenance:
    """Identifies the external dated classification source without inventing it."""

    source: str
    version: str
    content_hash: str
    as_of: date

    def __post_init__(self) -> None:
        if type(self.source) is not str or not self.source.strip():
            raise ValueError("metadata provenance source must be a nonempty plain str")
        if type(self.version) is not str or not self.version.strip():
            raise ValueError("metadata provenance version must be a nonempty plain str")
        if type(self.content_hash) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_hash):
            raise ValueError("metadata provenance content_hash must be sha256:<lowercase hex>")
        if type(self.as_of) is not date:
            raise ValueError("metadata provenance as_of must be a plain date")


@dataclass(frozen=True)
class UniverseMetadataRecord:
    """One externally supplied classification effective over an inclusive date range."""

    symbol: str
    asset_type: AssetType
    effective_from: date
    effective_to: date | None
    flags: frozenset[ClassificationFlag]
    etf_exposure: EtfExposure | None
    provenance: MetadataProvenance

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol.strip():
            raise ValueError("metadata symbol must be a nonempty plain str")
        if not isinstance(self.asset_type, AssetType):
            raise ValueError("metadata asset_type must be AssetType")
        if type(self.effective_from) is not date:
            raise ValueError("metadata effective_from must be a plain date")
        if self.effective_to is not None and type(self.effective_to) is not date:
            raise ValueError("metadata effective_to must be a plain date or None")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("metadata effective_to must not precede effective_from")
        if not isinstance(self.flags, frozenset) or any(not isinstance(flag, ClassificationFlag) for flag in self.flags):
            raise ValueError("metadata flags must be a frozenset of ClassificationFlag")
        if self.etf_exposure is not None and not isinstance(self.etf_exposure, EtfExposure):
            raise ValueError("metadata etf_exposure must be EtfExposure or None")
        if self.asset_type is not AssetType.ETF and self.etf_exposure is not None:
            raise ValueError("only ETF metadata may declare etf_exposure")
        if not isinstance(self.provenance, MetadataProvenance):
            raise ValueError("metadata provenance is required")
        if self.effective_from < self.provenance.as_of:
            raise ValueError("metadata effective_from must not precede provenance as_of")


@dataclass(frozen=True)
class UniverseMetadataSnapshot:
    """An immutable, non-overlapping collection of external classifications."""

    records: tuple[UniverseMetadataRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple) or not self.records:
            raise ValueError("metadata snapshot records must be a nonempty tuple")
        if any(not isinstance(record, UniverseMetadataRecord) for record in self.records):
            raise ValueError("metadata snapshot records must contain UniverseMetadataRecord values")
        ordered = sorted(self.records, key=lambda item: (item.symbol, item.effective_from, item.effective_to or date.max))
        for prior, current in zip(ordered, ordered[1:]):
            if prior.symbol != current.symbol:
                continue
            if prior.effective_to is None or current.effective_from <= prior.effective_to:
                raise ValueError(f"metadata records overlap or duplicate for symbol: {current.symbol}")


@dataclass(frozen=True)
class UniverseExclusion:
    symbol: str
    reason: str


@dataclass(frozen=True)
class UniverseSelection:
    symbols: tuple[str, ...]
    exclusions: tuple[UniverseExclusion, ...]


_IMPORT_FIELDS = frozenset({
    "symbol", "asset_type", "effective_from", "effective_to", "flags", "etf_exposure",
    "source", "version", "content_hash", "as_of",
})


def load_universe_metadata(path: str | Path) -> UniverseMetadataSnapshot:
    """Load fixture-compatible normalized CSV or JSON; never discovers classifications.

    JSON is ``{"format_version": 1, "records": [...]}``. CSV uses exactly the
    same record fields, with multiple flags separated by ``|`` and an empty string
    for null ``effective_to`` or ``etf_exposure``.
    """
    source_path = Path(path)
    try:
        if source_path.suffix.lower() == ".json":
            raw = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping) or set(raw) != {"format_version", "records"} or raw["format_version"] != 1:
                raise ValueError("metadata JSON must have format_version 1 and records")
            rows = raw["records"]
            if not isinstance(rows, list):
                raise ValueError("metadata JSON records must be a list")
        elif source_path.suffix.lower() == ".csv":
            with source_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None or set(reader.fieldnames) != _IMPORT_FIELDS:
                    raise ValueError("metadata CSV fields do not match normalized format")
                rows = list(reader)
        else:
            raise ValueError("metadata path must end in .json or .csv")
        return UniverseMetadataSnapshot(records=tuple(_record_from_import_row(row) for row in rows))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("metadata"):
            raise
        raise ValueError("invalid normalized universe metadata") from exc


def _record_from_import_row(raw: object) -> UniverseMetadataRecord:
    if not isinstance(raw, Mapping) or set(raw) != _IMPORT_FIELDS:
        raise ValueError("metadata record fields do not match normalized format")
    flags_raw = raw["flags"]
    if isinstance(flags_raw, list):
        flags_values = flags_raw
    elif type(flags_raw) is str:
        flags_values = [] if not flags_raw else flags_raw.split("|")
    else:
        raise ValueError("metadata flags must be a JSON list or CSV pipe-delimited string")
    effective_to_raw = raw["effective_to"]
    etf_exposure_raw = raw["etf_exposure"]
    try:
        return UniverseMetadataRecord(
            symbol=raw["symbol"], asset_type=AssetType(raw["asset_type"]),
            effective_from=_import_date(raw["effective_from"]),
            effective_to=None if effective_to_raw in (None, "") else _import_date(effective_to_raw),
            flags=frozenset(ClassificationFlag(value) for value in flags_values),
            etf_exposure=None if etf_exposure_raw in (None, "") else EtfExposure(etf_exposure_raw),
            provenance=MetadataProvenance(
                source=raw["source"], version=raw["version"], content_hash=raw["content_hash"], as_of=_import_date(raw["as_of"]),
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid normalized metadata record") from exc


def _import_date(value: object) -> date:
    if type(value) is not str:
        raise ValueError("metadata date must be an ISO date string")
    return date.fromisoformat(value)


def select_eligible_universe(
    snapshot: UniverseMetadataSnapshot,
    at: date,
    *,
    requested_symbols: Sequence[str] | None = None,
) -> UniverseSelection:
    """Return only classifications effective at ``at``; unknown symbols are denied.

    When ``requested_symbols`` is supplied, it is the complete upstream candidate
    set.  This makes missing classifications visible rather than silently omitted.
    """
    if not isinstance(snapshot, UniverseMetadataSnapshot):
        raise ValueError("snapshot must be UniverseMetadataSnapshot")
    if type(at) is not date:
        raise ValueError("at must be a plain date")
    active_by_symbol = {
        record.symbol: record
        for record in snapshot.records
        if record.effective_from <= at and (record.effective_to is None or at <= record.effective_to)
    }
    if requested_symbols is None:
        requested = tuple(active_by_symbol)
    else:
        if isinstance(requested_symbols, (str, bytes)):
            raise ValueError("requested_symbols must be a sequence of unique nonempty plain str")
        requested = tuple(requested_symbols)
        if len(requested) != len(set(requested)) or any(type(symbol) is not str or not symbol.strip() for symbol in requested):
            raise ValueError("requested_symbols must be a sequence of unique nonempty plain str")

    symbols: list[str] = []
    exclusions: list[UniverseExclusion] = []
    for symbol in sorted(requested):
        record = active_by_symbol.get(symbol)
        if record is None:
            exclusions.append(UniverseExclusion(symbol, "METADATA_MISSING_OR_NOT_EFFECTIVE"))
        elif record.provenance.as_of > at:
            exclusions.append(UniverseExclusion(symbol, "PROVENANCE_NOT_AVAILABLE_AT_DATE"))
        elif (reason := _ineligibility_reason(record)) is None:
            symbols.append(symbol)
        else:
            exclusions.append(UniverseExclusion(symbol, reason))
    return UniverseSelection(symbols=tuple(symbols), exclusions=tuple(exclusions))


def _ineligibility_reason(record: UniverseMetadataRecord) -> str | None:
    if record.asset_type is not AssetType.STOCK and record.asset_type is not AssetType.ETF:
        return f"ASSET_TYPE_{record.asset_type.value}"
    for flag in ClassificationFlag:
        if flag in record.flags:
            return f"FLAG_{flag.value}"
    if record.asset_type is AssetType.ETF and record.etf_exposure is not EtfExposure.DOMESTIC_INDEX_OR_SECTOR:
        return "ETF_EXPOSURE_UNKNOWN"
    return None
