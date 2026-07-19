# Authoritative Halt-Release Evidence / Attestation — Phase 1

`swing_v2.live.authoritative_halt_release` is a local, typed cryptographic
**contract**, not an external privileged reconciler and not a halt-release
mechanism. It has no KIS client, account/token/OAuth handling, network,
filesystem, submit/amend/cancel, or halt-marker mutation capability.

## What it evaluates

A caller supplies an immutable request that binds protocol version, hashed account
binding, original audited action fingerprint, exact broker branch/order
acknowledgement reference, opaque marker identity/digest, UTC audit/request times,
an exact operator-review phrase, and a per-invocation opaque 32-byte challenge.
The caller must also supply the *current expected* opaque marker identity and
digest to the evaluator; either mismatch is a `CONTRADICTION`. The raw challenge
is not exposed in the attestation; its digest is part of the request fingerprint.

Before an otherwise eligible result is returned, the evaluator creates a fresh
exact 32-byte **authority-invocation challenge** and calls an injected
`ExternalReviewChallengeAuthority` with the exact release-request fingerprint,
expected account binding, operator-review challenge, authority-invocation
challenge, fixed protocol id/version, and an expiry of `requested_at + 60
seconds`. This invocation challenge is distinct from the signed operator-review
challenge. The evaluator requires explicit expected review-authority ID, version,
and Ed25519 public verification key; there are no local defaults.

The independently privileged authority must perform durable, atomic one-shot
consumption and return an Ed25519-signed receipt binding its identity/version,
canonical consumption-request fingerprint, account binding, review-challenge
binding and expiry, invocation-challenge digest, and opaque receipt digest. The
verifier checks all of those bindings before eligibility, so a cached genuine
receipt from another evaluator invocation and a synthetic typed receipt both fail
closed. It must be a real external authority in production (an adapted external
nonce service is an appropriate implementation) with a separately protected
private signing key and durable consumption store; this module provides no local,
filesystem, HMAC, or in-memory fallback. Missing, malformed, failed, forged, or
replayed consumption is `UNRESOLVED`; receipt material and raw challenges are
never placed in the decision.

Immediately after receipt verification, the evaluator revalidates the complete
captured primitive snapshot of caller-controlled expected material (including
broker branch/order acknowledgement, marker identity/digest, account/action
bindings, and authority configuration). Mutation during authority consumption is
therefore `UNRESOLVED` after the authority's one-shot side effect.

A separately owned reconciliation authority must attest a complete canonical
scope: both observation boundaries must be no more than 120 seconds old and the
observation duration at most 120 seconds; the fills query must end exactly at the
observation end, be at most 31 days, and contain `submitted_at`. (The original
submission itself may be older than 120 seconds, but must be no later than
`requested_at`.) The scope also binds complete open orders, page-complete bounded
fills, balances, positions, cash, and an explicitly declared atomic collection
identifier. The public verifier accepts only the authority's Ed25519 public key.
The test-only signing helper requires an explicit private seed and is not a
production authority path.

Only a current, exact, authentic `NO_CONTRADICTION` attestation with fresh
complete atomic evidence yields `ELIGIBLE_FOR_SEPARATE_OPERATOR_REVIEW`.
This word is intentionally narrow: it is neither a release nor authorization to
release. Missing, partial, stale, non-atomic, malformed, replayed, or signature-
mismatched material is `UNRESOLVED`; a material request mismatch or explicit
authority contradiction is `CONTRADICTION`.

## Production requirement

Production requires a separately owned and separately privileged authority
service collecting fresh atomic broker evidence, signing its attestation, and an
independent explicit operator action. A later privileged halt-marker clear action
must be separately designed and audited. This Phase 1 code intentionally performs
none of those actions and never deletes, clears, or mutates a halt marker.

`production_reconciliation.py` remains a non-authoritative sequential read-only
snapshot contract and cannot be promoted through this module into a halt clear.
