# KIS Production Reconciliation — Phase 1

`swing_v2.live.production_reconciliation` is a production-KIS **read-only**
parsing contract. It accepts only injected credentials, token, account, session,
and clock; it neither loads environment files nor creates a default network
client. Its transport surface is deliberately limited to injected HTTP `GET`
calls for KRX open orders, daily order/fill history, and balance.

## Explicitly non-authoritative

Phase 1 is **not** authorization to release an ambiguous broker-state halt.
`assess_ambiguous_halt` is pure evidence classification only: it neither mutates
nor deletes a halt/audit marker, and it never makes a release decision. In
particular, it never returns `CLEAR_EVIDENCE` for a Phase 1
`ReconciliationSnapshot`, including when an order/fill record exactly matches
an expected broker reference and reports a full fill. Such a match is
`UNRESOLVED`; conflicting identity, side, symbol, or quantity is a
`CONTRADICTION`. Missing, rejected, malformed, partial, duplicate, or otherwise
unrecognized data remains unresolved or contradictory conservatively.

A snapshot records the requested fill date range, per-source observation time,
completed pagination page count, a non-atomic-observation marker, and the scope
of its account-binding hash. The hash is only a local hash of the injected
account and is not an expected-account binding or broker attestation. The
sequential balance/open-order/fill GETs are not atomic. The deterministic local
SHA-256 snapshot digest detects ordinary corruption only; it is unkeyed and
makes no tamper-proof provenance, broker-origin, freshness, account-ownership,
or authorization claim. No raw account number or token is persisted in a
snapshot.

## Requirements for any future privileged reconciler

A separate, privileged design must not reuse this module as its halt-release
authority. It needs all of the following before a separate explicit operator
release action:

1. expected-account binding and an original audited intent bound to the broker
   acknowledgement/order identity;
2. fresh, bounded observations with proven complete pagination and requested
   date/range coverage;
3. an atomic or otherwise safely coordinated broker view that reconciles the
   bound account's positions, cash, orders, and fills;
4. external integrity/signature or equivalent independently verifiable broker
   provenance; and
5. an explicit operator release in a separately privileged process/service.

Phase 1 performs none of these steps. It has no halt mutation, submit, amend,
cancel, token, or environment-loading capability, and it neither deletes a
marker nor makes a release decision.
