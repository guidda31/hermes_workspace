# Point-in-time KRX universe metadata contract

`FinanceDataReader` listing data is a **current** listing snapshot. It must not be
used to infer historical security type, preferred-share/SPAC status, management
issues, halts, or ETF classification.

This project therefore has no historical KRX-universe dataset yet. A dated external
source still must be acquired, retained locally, hashed, and imported before a broad
historical study can claim a credible universe.

## Normalized import formats

`load_universe_metadata(path)` accepts a local JSON or CSV file only. It makes no
network calls and performs no listing discovery.

JSON has exactly:

```json
{
  "format_version": 1,
  "records": [
    {
      "symbol": "069500",
      "asset_type": "ETF",
      "effective_from": "2024-01-01",
      "effective_to": null,
      "flags": [],
      "etf_exposure": "DOMESTIC_INDEX_OR_SECTOR",
      "source": "external-provider-or-archived-krx-file",
      "version": "provider-version-or-file-date",
      "content_hash": "sha256:<64 lowercase hex characters>",
      "as_of": "2024-01-01"
    }
  ]
}
```

CSV has exactly the same record fields as columns. Use an empty field for null
`effective_to` / `etf_exposure`; separate multiple `flags` with `|`.

Every record is immutable and must include a nonempty `source`, `version`, SHA-256
content hash, and dated `as_of`, in addition to inclusive `effective_from` and
optional inclusive `effective_to`. Duplicate or overlapping windows for a symbol
are rejected.

### Provenance time consistency

The importer preserves the declared hash in the normalized record but does **not**
claim to verify a local source file against it.  Hash format and provenance fields
are validated only.

To prevent a current listing/classification snapshot from fabricating older
membership, `effective_from` may not precede `provenance.as_of`. A selection at
date *t* can use a record only when its `provenance.as_of <= t` (records that are
not yet effective are denied as inactive). Consequently, a historical source must
assert an `as_of` date on or before every session it is intended to support; a
later snapshot cannot be backfilled into an earlier study period.

## Conservative eligibility at date *t*

Call `select_eligible_universe(snapshot, t, requested_symbols=...)`. It returns
symbols sorted deterministically and a symbol-level exclusion audit trail.

Allowed only when active at *t*:

- `STOCK` with no exclusion flag
- `ETF` explicitly classified as `DOMESTIC_INDEX_OR_SECTOR`, with no exclusion flag

Denied with an auditable reason:

- asset types `PREFERRED`, `SPAC`, `ETN`, or explicit `UNKNOWN`
- `MANAGEMENT_ISSUE`, `TRADING_HALTED`, `ETF_LEVERAGED`, `ETF_INVERSE`,
  `ETF_FOREIGN_INDEX`
- ETF with no explicit local index/sector classification
- requested symbol with missing or inactive metadata

ETF eligibility is classification-driven; no security-name heuristic is used.

## Candidate pipeline seam

`BacktestConfig` requires a `UniverseMetadataSnapshot`; there is intentionally no
unsafe default. `BacktestRunner` selects its point-in-time eligible symbols before
requesting candidate asset types/history or calculating candidates, and retains
denials in `BacktestResult.universe_exclusions`. The standalone
`swing_v2.backtest.assess_eligible_close_time_candidates(...)` provides the same
filtered seam for other local research uses. Neither path invokes KIS, orders, or
network calls.
