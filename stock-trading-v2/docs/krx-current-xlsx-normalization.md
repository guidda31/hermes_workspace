# KRX current XLSX normalizer (forward-only)

`swing_v2.krx_xlsx_normalizer.normalize_krx_xlsx(...)` converts three **local** KRX
Data Marketplace XLSX exports into the strict `load_universe_metadata` JSON format
and a companion SHA-256 manifest. It neither calls a network service nor alters the
source XLSX files.

## Time/provenance constraint

The supplied exports do not expose an intrinsic as-of timestamp. Their `2026-07-18`
date is only filename/user-supplied evidence, so the resulting snapshot declares:

- `as_of = effective_from = 2026-07-18` on every record;
- **eligible only for signal dates on or after 2026-07-18**; and
- **never valid for historical membership/backtests** before that date.

The manifest says this explicitly, records each original source filename and
SHA-256, and defines both the normalized-output hash and the combined source hash
stored in every record's `content_hash` provenance field.

## Conservative policy

- Stock eligibility: only `증권구분=주권` and `주식종류=보통주`, with foreign red
  flags denied. `관리종목` is retained as `STOCK` + `MANAGEMENT_ISSUE`; `SPAC` is
  retained as `SPAC`; all other source security types are `UNKNOWN`.
- ETF detail and ETF basic rows must have exactly matching short-code sets; duplicate,
  missing, or mismatched codes fail rather than being guessed.
- ETF eligibility needs every one of: `기초시장분류=국내`, `기초자산분류=주식`,
  `추적배수=일반`, and taxonomy exactly `주식-시장대표` or beginning
  `주식-업종섹터`. Leverage/inverse and non-domestic markets retain exclusion flags;
  unclear taxonomy becomes `UNKNOWN`.

KRX worksheets falsely declare an `A1` dimension. The normalizer resets that
read-only `openpyxl` dimension before parsing, avoiding a silent header-only import.

## Current local artifact

- JSON: `/home/guidda/hermes_workspace/data/normalized/krx_universe_2026-07-18.json`
- Manifest: `/home/guidda/hermes_workspace/data/normalized/krx_universe_2026-07-18.manifest.json`

These are local ignored outputs (`data/`), not tracked project inputs.
