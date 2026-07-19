# Local public daily-bar snapshots

`src/swing_v2/backtest_data.py` provides a normalized, immutable JSON snapshot and
`SnapshotBacktestData`, which serves a backtest only from that local snapshot. It
performs no runtime market-data fetches.

## Acquisition boundary

Use `build_snapshot_from_fdr` only to acquire data deliberately. The caller must
inject a small explicit `symbols` list and a complete `asset_types` map, plus the
market-index symbol and bounded date range. The builder does **not** discover a
universe or infer/classify stocks, ETFs, ETNs, leveraged products, or point-in-time
membership. It can optionally write the resulting snapshot with `save_snapshot`.

Snapshot metadata records the source, timezone-bearing retrieval timestamp,
source-data-as-of date, and whether trading value is a proxy. JSON encodes every
`Decimal` as a string so a saved fixture round-trips without binary float loss.

## Data limitations

The public source is FinanceDataReader (FDR), so availability, adjustments, and
coverage are limited by that upstream service. FDR OHLCV does not provide official
KRX turnover in this adapter: `trading_value` is explicitly the **close × volume**
proxy, not official intraday transaction value. A snapshot is reproducible for its
saved inputs, but it is not a point-in-time universe/classification dataset and
must not be represented as one.
