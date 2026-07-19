"""Immutable, local daily-bar snapshots for reproducible backtests.

Snapshots are deliberately self-contained: reading one never consults a market-data
provider.  ``build_snapshot_from_fdr`` is the separate, explicit acquisition seam.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from .contracts import DailyBar

if TYPE_CHECKING:
    from .public_data import FinanceDataReaderAdapter
    from .kis import KisClient


SNAPSHOT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class SnapshotMetadata:
    """Provenance required to interpret a public-data snapshot honestly."""

    source: str
    retrieved_at: str
    data_as_of: str
    trading_value_is_close_times_volume_proxy: bool

    def __post_init__(self) -> None:
        if type(self.source) is not str or not self.source.strip():
            raise ValueError("snapshot metadata source must be a nonempty plain str")
        if type(self.retrieved_at) is not str:
            raise ValueError("snapshot metadata retrieved_at must be an ISO-8601 timestamp string")
        try:
            timestamp = datetime.fromisoformat(self.retrieved_at)
        except ValueError as exc:
            raise ValueError("snapshot metadata retrieved_at must be an ISO-8601 timestamp") from exc
        if timestamp.tzinfo is None:
            raise ValueError("snapshot metadata retrieved_at must include a timezone")
        if type(self.data_as_of) is not str:
            raise ValueError("snapshot metadata data_as_of must be an ISO date string")
        try:
            date.fromisoformat(self.data_as_of)
        except ValueError as exc:
            raise ValueError("snapshot metadata data_as_of must be an ISO date") from exc
        if type(self.trading_value_is_close_times_volume_proxy) is not bool:
            raise ValueError("snapshot metadata proxy flag must be a bool")


@dataclass(frozen=True)
class DailyBarSnapshot:
    """Validated normalized records, with no mutable collection exposed by default."""

    metadata: SnapshotMetadata
    market_symbol: str
    asset_types: Mapping[str, str]
    trade_calendar: Sequence[date]
    histories: Mapping[str, Sequence[DailyBar]]
    market_history: Sequence[DailyBar]

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, SnapshotMetadata):
            raise ValueError("snapshot metadata must be SnapshotMetadata")
        _validate_identity(self.market_symbol, "market_symbol")
        asset_types = _normalized_asset_types(self.asset_types)
        calendar = tuple(self.trade_calendar)
        if not calendar or any(type(day) is not date for day in calendar):
            raise ValueError("snapshot trade_calendar must contain plain dates")
        if any(calendar[index] >= calendar[index + 1] for index in range(len(calendar) - 1)):
            raise ValueError("snapshot trade_calendar must be strictly ascending and unique")
        data_as_of = date.fromisoformat(self.metadata.data_as_of)
        if calendar[-1] > data_as_of:
            raise ValueError("snapshot trade_calendar cannot be later than metadata data_as_of")
        histories = _normalized_histories(self.histories, asset_types, calendar, data_as_of)
        market_history = _normalized_market_history(self.market_history, self.market_symbol, calendar, data_as_of)
        object.__setattr__(self, "asset_types", MappingProxyType(asset_types))
        object.__setattr__(self, "trade_calendar", calendar)
        object.__setattr__(self, "histories", MappingProxyType(histories))
        object.__setattr__(self, "market_history", market_history)


def save_snapshot(snapshot: DailyBarSnapshot, path: str | Path) -> None:
    """Write canonical JSON with Decimal values encoded as lossless strings."""
    if not isinstance(snapshot, DailyBarSnapshot):
        raise ValueError("snapshot must be DailyBarSnapshot")
    destination = Path(path)
    payload = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "metadata": {
            "source": snapshot.metadata.source,
            "retrieved_at": snapshot.metadata.retrieved_at,
            "data_as_of": snapshot.metadata.data_as_of,
            "trading_value_is_close_times_volume_proxy": snapshot.metadata.trading_value_is_close_times_volume_proxy,
        },
        "market_symbol": snapshot.market_symbol,
        "asset_types": dict(snapshot.asset_types),
        "trade_calendar": [day.isoformat() for day in snapshot.trade_calendar],
        "histories": {symbol: [_bar_to_json(bar) for bar in bars] for symbol, bars in snapshot.histories.items()},
        "market_history": [_bar_to_json(bar) for bar in snapshot.market_history],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_snapshot(path: str | Path) -> DailyBarSnapshot:
    """Read one canonical snapshot; reject malformed or non-normalized records."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("snapshot must be readable JSON") from exc
    if not isinstance(raw, dict) or raw.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise ValueError("unsupported or missing snapshot format_version")
    required = {"format_version", "metadata", "market_symbol", "asset_types", "trade_calendar", "histories", "market_history"}
    if set(raw) != required:
        raise ValueError("snapshot JSON fields do not match the normalized format")
    try:
        metadata_raw = raw["metadata"]
        if not isinstance(metadata_raw, dict):
            raise ValueError("snapshot metadata must be an object")
        metadata = SnapshotMetadata(**metadata_raw)
        calendar = tuple(_date_from_json(value, "trade_calendar") for value in raw["trade_calendar"])
        histories_raw = raw["histories"]
        if not isinstance(histories_raw, dict):
            raise ValueError("snapshot histories must be an object")
        histories = {symbol: tuple(_bar_from_json(bar) for bar in bars) for symbol, bars in histories_raw.items()}
        market_history_raw = raw["market_history"]
        if not isinstance(market_history_raw, list):
            raise ValueError("snapshot market_history must be an array")
        return DailyBarSnapshot(
            metadata=metadata, market_symbol=raw["market_symbol"], asset_types=raw["asset_types"],
            trade_calendar=calendar, histories=histories,
            market_history=tuple(_bar_from_json(bar) for bar in market_history_raw),
        )
    except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
        raise ValueError("invalid snapshot JSON") from exc


class SnapshotBacktestData:
    """``BacktestData`` implementation backed exclusively by one local snapshot.

    Its constructor constructs all indexes from the snapshot.  Query methods never
    call FinanceDataReader, perform I/O, or otherwise fetch runtime market data.
    """

    def __init__(self, snapshot: DailyBarSnapshot) -> None:
        if not isinstance(snapshot, DailyBarSnapshot):
            raise ValueError("snapshot must be DailyBarSnapshot")
        self._snapshot = snapshot
        self._bars_by_symbol_date = MappingProxyType({
            symbol: MappingProxyType({bar.trade_date: bar for bar in history})
            for symbol, history in snapshot.histories.items()
        })
        self._market_by_date = MappingProxyType({bar.trade_date: bar for bar in snapshot.market_history})

    def get_trade_calendar(self, start_date: date, end_date: date) -> tuple[date, ...]:
        _validate_date_range(start_date, end_date)
        return tuple(day for day in self._snapshot.trade_calendar if start_date <= day <= end_date)

    def get_bars(self, trade_date: date) -> Mapping[str, DailyBar | None]:
        _validate_plain_date(trade_date, "trade_date")
        return MappingProxyType({symbol: by_date.get(trade_date) for symbol, by_date in self._bars_by_symbol_date.items()})

    def get_market_index_bar(self, trade_date: date) -> DailyBar | None:
        _validate_plain_date(trade_date, "trade_date")
        return self._market_by_date.get(trade_date)

    def get_historical_closes(self, symbol: str, end_date: date, window: int) -> tuple[Decimal, ...]:
        return tuple(bar.close for bar in self.get_historical_bars(symbol, end_date, window))

    def get_historical_bars(self, symbol: str, end_date: date, window: int) -> tuple[DailyBar, ...]:
        _validate_identity(symbol, "symbol")
        _validate_plain_date(end_date, "end_date")
        if type(window) is not int or window <= 0:
            raise ValueError("window must be a positive int")
        history = self._history_for(symbol)
        # Histories are validated ascending; filtering makes the no-lookahead
        # boundary explicit even if a caller requests a date outside the calendar.
        observed = tuple(bar for bar in history if bar.trade_date <= end_date)
        return observed[-window:]

    def get_asset_type(self, symbol: str) -> str:
        _validate_identity(symbol, "symbol")
        if symbol in self._snapshot.asset_types:
            return self._snapshot.asset_types[symbol]
        if symbol == self._snapshot.market_symbol:
            return self._snapshot.market_history[0].asset_type
        raise ValueError(f"unknown snapshot symbol: {symbol}")

    def _history_for(self, symbol: str) -> tuple[DailyBar, ...]:
        if symbol == self._snapshot.market_symbol:
            return tuple(self._snapshot.market_history)
        try:
            return tuple(self._snapshot.histories[symbol])
        except KeyError as exc:
            raise ValueError(f"unknown snapshot symbol: {symbol}") from exc


def build_snapshot_from_fdr(
    *,
    adapter: FinanceDataReaderAdapter,
    symbols: Sequence[str],
    asset_types: Mapping[str, str],
    market_symbol: str,
    start: date,
    end: date,
    metadata: SnapshotMetadata,
    output_path: str | Path | None = None,
) -> DailyBarSnapshot:
    """Fetch an explicit, bounded set of FDR series and optionally write a snapshot.

    ``symbols`` and ``asset_types`` are injected by the caller; this function does
    no universe discovery or ETF/security classification.  The market index is
    fetched as asset type ``INDEX`` solely to construct the trading calendar.
    """
    if not hasattr(adapter, "load_daily_bars") or not callable(adapter.load_daily_bars):
        raise ValueError("adapter must provide load_daily_bars")
    _validate_date_range(start, end)
    if not isinstance(metadata, SnapshotMetadata):
        raise ValueError("metadata must be SnapshotMetadata")
    symbol_list = tuple(symbols)
    if not symbol_list or len(symbol_list) != len(set(symbol_list)):
        raise ValueError("symbols must be a nonempty sequence of unique symbols")
    for symbol in symbol_list:
        _validate_identity(symbol, "symbol")
    _validate_identity(market_symbol, "market_symbol")
    if set(asset_types) != set(symbol_list):
        raise ValueError("asset_types must contain exactly the explicitly requested symbols")
    normalized_asset_types = _normalized_asset_types(asset_types)
    histories = {
        symbol: _bounded_fdr_history(adapter, symbol, normalized_asset_types[symbol], start, end)
        for symbol in symbol_list
    }
    market_history = _bounded_fdr_history(adapter, market_symbol, "INDEX", start, end)
    calendar = tuple(bar.trade_date for bar in market_history)
    snapshot = DailyBarSnapshot(metadata, market_symbol, normalized_asset_types, calendar, histories, market_history)
    if output_path is not None:
        save_snapshot(snapshot, output_path)
    return snapshot


def _bounded_fdr_history(adapter: FinanceDataReaderAdapter, symbol: str, asset_type: str, start: date, end: date) -> tuple[DailyBar, ...]:
    bars = tuple(adapter.load_daily_bars(symbol=symbol, asset_type=asset_type, start=start, end=end))
    if any(bar.trade_date < start or bar.trade_date > end for bar in bars):
        raise ValueError(f"FDR returned an out-of-range bar for {symbol}")
    return bars


def build_snapshot_from_kis(
    *,
    adapter: "KisClient",
    access_token: str,
    symbols: Sequence[str],
    asset_types: Mapping[str, str],
    market_symbol: str,
    market_history: Sequence[DailyBar],
    start: date,
    end: date,
    metadata: SnapshotMetadata,
    output_path: str | Path | None = None,
) -> DailyBarSnapshot:
    """Acquire bounded KIS stock/ETF bars into an immutable local snapshot.

    The domestic-stock price endpoint does not provide the market-index series,
    so the caller explicitly supplies compatible index bars.  This does not
    discover ETF/security classifications or management/halt history.
    """
    if not hasattr(adapter, "load_domestic_daily_bars") or not callable(adapter.load_domestic_daily_bars):
        raise ValueError("adapter must provide load_domestic_daily_bars")
    if type(access_token) is not str or not access_token:
        raise ValueError("access_token must be a nonempty plain str")
    _validate_date_range(start, end)
    if not isinstance(metadata, SnapshotMetadata):
        raise ValueError("metadata must be SnapshotMetadata")
    if "KIS" not in metadata.source.upper():
        raise ValueError("KIS snapshots must identify KIS in metadata.source")
    symbol_list = tuple(symbols)
    if not symbol_list or len(symbol_list) != len(set(symbol_list)):
        raise ValueError("symbols must be a nonempty sequence of unique symbols")
    for symbol in symbol_list:
        _validate_identity(symbol, "symbol")
    _validate_identity(market_symbol, "market_symbol")
    if set(asset_types) != set(symbol_list):
        raise ValueError("asset_types must contain exactly the explicitly requested symbols")
    normalized_asset_types = _normalized_asset_types(asset_types)
    histories = {
        symbol: _bounded_kis_history(adapter, access_token, symbol, normalized_asset_types[symbol], start, end)
        for symbol in symbol_list
    }
    all_market_history = tuple(market_history)
    if any(not isinstance(bar, DailyBar) for bar in all_market_history):
        raise ValueError("market_history must contain DailyBar values")
    bounded_market_history = tuple(bar for bar in all_market_history if start <= bar.trade_date <= end)
    calendar = tuple(bar.trade_date for bar in bounded_market_history)
    snapshot = DailyBarSnapshot(metadata, market_symbol, normalized_asset_types, calendar, histories, bounded_market_history)
    if output_path is not None:
        save_snapshot(snapshot, output_path)
    return snapshot


def _bounded_kis_history(adapter: "KisClient", access_token: str, symbol: str, asset_type: str, start: date, end: date) -> tuple[DailyBar, ...]:
    bars = tuple(adapter.load_domestic_daily_bars(access_token, symbol, asset_type, start, end))
    if any(not isinstance(bar, DailyBar) or bar.trade_date < start or bar.trade_date > end for bar in bars):
        raise ValueError(f"KIS returned an invalid or out-of-range bar for {symbol}")
    return bars


def _validate_plain_date(value: object, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be a plain date")


def _validate_date_range(start: date, end: date) -> None:
    _validate_plain_date(start, "start_date")
    _validate_plain_date(end, "end_date")
    if start > end:
        raise ValueError("start_date must not be after end_date")


def _validate_identity(value: object, name: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"snapshot {name} must be a nonempty plain str")


def _normalized_asset_types(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("snapshot asset_types must be a nonempty mapping")
    result = dict(raw)
    for symbol, asset_type in result.items():
        _validate_identity(symbol, "asset type symbol")
        _validate_identity(asset_type, "asset type")
    return result


def _normalized_histories(raw: object, asset_types: Mapping[str, str], calendar: tuple[date, ...], data_as_of: date) -> dict[str, tuple[DailyBar, ...]]:
    if not isinstance(raw, Mapping) or set(raw) != set(asset_types):
        raise ValueError("snapshot histories must have exactly one entry per asset type symbol")
    result: dict[str, tuple[DailyBar, ...]] = {}
    for symbol, values in raw.items():
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ValueError(f"snapshot history for {symbol} must be a sequence")
        bars = tuple(values)
        _validate_bars(bars, symbol, asset_types[symbol], calendar, data_as_of)
        result[symbol] = bars
    return result


def _normalized_market_history(raw: object, market_symbol: str, calendar: tuple[date, ...], data_as_of: date) -> tuple[DailyBar, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("snapshot market_history must be a sequence")
    bars = tuple(raw)
    _validate_bars(bars, market_symbol, None, calendar, data_as_of)
    return bars


def _validate_bars(bars: tuple[DailyBar, ...], symbol: str, asset_type: str | None, calendar: tuple[date, ...], data_as_of: date) -> None:
    if not bars or any(not isinstance(bar, DailyBar) for bar in bars):
        raise ValueError(f"snapshot history for {symbol} must contain DailyBar values")
    calendar_set = set(calendar)
    for index, bar in enumerate(bars):
        if bar.symbol != symbol or (asset_type is not None and bar.asset_type != asset_type):
            raise ValueError(f"snapshot history identities do not match {symbol}")
        if bar.trade_date not in calendar_set:
            raise ValueError(f"snapshot history date for {symbol} is not in trade_calendar")
        if bar.trade_date > data_as_of:
            raise ValueError(f"snapshot history date for {symbol} is later than metadata data_as_of")
        if index and bars[index - 1].trade_date >= bar.trade_date:
            raise ValueError(f"snapshot history for {symbol} must be strictly ascending and unique")


def _bar_to_json(bar: DailyBar) -> dict[str, object]:
    return {
        "trade_date": bar.trade_date.isoformat(), "symbol": bar.symbol, "asset_type": bar.asset_type,
        "open": str(bar.open), "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
        "volume": bar.volume, "trading_value": str(bar.trading_value), "is_tradable": bar.is_tradable,
    }


def _bar_from_json(raw: object) -> DailyBar:
    if not isinstance(raw, dict) or set(raw) != {"trade_date", "symbol", "asset_type", "open", "high", "low", "close", "volume", "trading_value", "is_tradable"}:
        raise ValueError("snapshot daily bar fields do not match the normalized format")
    if any(type(raw[field]) is not str for field in ("open", "high", "low", "close", "trading_value")):
        raise ValueError("snapshot Decimal fields must be strings")
    if type(raw["volume"]) is not int or type(raw["is_tradable"]) is not bool:
        raise ValueError("snapshot daily bar volume and tradable fields have invalid types")
    return DailyBar(
        trade_date=_date_from_json(raw["trade_date"], "daily bar trade_date"), symbol=raw["symbol"], asset_type=raw["asset_type"],
        open=Decimal(raw["open"]), high=Decimal(raw["high"]), low=Decimal(raw["low"]), close=Decimal(raw["close"]),
        volume=raw["volume"], trading_value=Decimal(raw["trading_value"]), is_tradable=raw["is_tradable"],
    )


def _date_from_json(value: object, name: str) -> date:
    if type(value) is not str:
        raise ValueError(f"snapshot {name} must be an ISO date string")
    return date.fromisoformat(value)
