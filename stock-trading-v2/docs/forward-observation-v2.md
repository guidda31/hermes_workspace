# Forward-observation dataset v2

`forward-observation-v2-YYYY-MM-DD.json` is the approved immutable
forward-observation envelope format. The loader rejects all v1 envelopes;
`forward-observation-2026-07-16.json` remains a legacy, unapproved artifact
and is deliberately not replaced.

## Integrity and bounds

A v2 envelope contains an `integrity` object with `algorithm: "sha256"` and a
lowercase 64-hex `digest`. The digest is SHA-256 over deterministic canonical
UTF-8 JSON (sorted keys and compact separators) of the whole envelope with the
`integrity` member omitted. The loader verifies it before interpreting the
snapshot or provenance.

The loader also requires that `data_as_of` is the exact final date of the KOSPI
calendar, KOSPI history, and every symbol history. All embedded bars and the
calendar are already required to be ascending, unique, and no later than
`data_as_of`; therefore a valid v2 artifact cannot silently become stale or
leak a later observation.

Provenance references retain source paths without reopening them at load time,
so an artifact remains loadable if its original local inputs are later absent.
Every recorded path must be a nonempty string and every provenance SHA-256
must be exactly 64 lowercase hexadecimal characters.

The detached digest detects a modification when its stored digest is not also
replaced. It is an integrity/tamper-evidence check, not a signed authenticity
scheme; use separately retained artifact bytes or a signature if adversarial
re-signing must be prevented.
