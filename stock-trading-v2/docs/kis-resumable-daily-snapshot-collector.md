# Resumable KIS daily-bar collector

`python -m swing_v2.kis_snapshot_collector` uses only the KIS OAuth token endpoint and the domestic daily-price quotation endpoint. It never invokes account, balance, order, portfolio, or trading endpoints.

```bash
.venv/bin/python -m swing_v2.kis_snapshot_collector \
  --symbol 005930:STOCK \
  --start 2026-07-06 --end 2026-07-17 \
  --output data/kis-daily/005930-2026-07-17 \
  --delay-seconds 0.25 --max-symbols 1 --max-requests 1
```

KIS credentials are read from the existing `.env` (`KIS_APP_KEY`, `KIS_APP_SECRET`) and are never emitted by the CLI. Use only an explicit small symbol list; the collector does no universe discovery or bulk sweep.

## Optional local OAuth cache

Set `KIS_TOKEN_CACHE=.cache/kis_token.json` to let both KIS collector CLIs reuse a still-valid OAuth bearer token instead of needlessly requesting a new one. The cache contains a bearer secret plus issue/expiry timestamps, is atomically written with mode `0600`, and is rejected if it is malformed, expired (including the early-expiry safety skew), owned by another user, or readable/writable by group or others. Cache files are local-only: `.cache/` and token-like JSON files are ignored by Git. Do not print, share, or commit the cache. If `KIS_TOKEN_CACHE` is absent, the previous uncached token-request behavior is used.

Each symbol is written once as an immutable JSON partial snapshot, and `manifest.json` records schema version, source, requested dates, observed dates, SHA-256, and any errors. Resume verifies the file hash and matching request metadata before skipping a completed symbol. A corrupt/mismatched partial is **not trusted** and is never silently overwritten. The manifest is complete only if every requested symbol completed successfully.

The daily-price endpoint can have incomplete historical retention or non-trading dates. Requested bounds are not coverage claims: consumers must use each symbol's `observed_start` and `observed_end` in the manifest.
