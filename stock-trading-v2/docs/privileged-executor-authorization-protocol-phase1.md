# Privileged executor authorization protocol — Phase 1

`src/swing_v2/live/privileged_protocol.py` is a **non-submitting contract only**. It
cannot contact KIS or any broker: it contains no credential loading, OAuth/token work,
HTTP/session dependency, socket/daemon/CLI server, reconciliation client, transport
method, halt-clear operation, or order-submission API.

## What it provides

A test-only `PrivilegedAuthorizationIssuer` signs one exact
`OperatorAuthorizationRequest` with an injected 32-byte HMAC-SHA256 key. The request
binds all of the following:

- fixed protocol version and the literal `PRODUCTION` target;
- an account **binding digest**, never the raw account identifier;
- a canonical live-intent fingerprint and a recomputed material-action fingerprint;
- one exact KIS/KRX six-digit symbol, allowed classification, side, positive quantity,
  positive `Decimal` limit price, and `LIMIT` mode only;
- exact UTC issue/expiry timestamps (at most 60 seconds), an opaque exact 32-byte
  nonce, and the fixed explicit operator confirmation phrase.

`PrivilegedDispatchVerifier` requires an injected
`ExternalNonceConsumptionAuthority`; there is **no default and no local fallback**. After
HMAC, timing, account, action allowlist, and TOCTOU checks, it calls
`consume_once(ExternalNonceConsumptionRequest)`. That canonical request binds the opaque
nonce, action fingerprint, account-binding digest, and expiry. The authority must
atomically consume/persist that binding and either return an exact typed
`ExternalNonceConsumptionReceipt` or raise. Malformed dependencies, authority failures,
malformed receipts, or receipt authority ID/version mismatches fail closed and produce no
`ApprovedDispatch`.

`ApprovedDispatch` is immutable and non-executable. It carries the opaque receipt's
`authority_id`, `authority_version`, and `receipt_digest` as a
`nonce_consumption_receipt`; it carries no raw nonce, account reference, or signing key.
A receipt means only that the external authority reported consumption. It is **not**
broker authorization and does not execute an order.

## Required production authority boundary

This repository intentionally supplies **no production nonce-consumption authority**. A
real authority must be outside the strategy process and run under a separately privileged
OS principal, or behind a separately privileged durable database/service. It must atomically
persist consumption before returning its receipt, authenticate its caller and response
channel, bind exactly the canonical nonce/action/account/expiry input, and retain consumed
records for the relevant replay window according to its own durable policy.

A same-UID local filesystem implementation is explicitly prohibited as a production
authority. In particular, deletion/root reset, directory replacement, and inode reuse make
local device/inode anchors unsuitable for durable replay prevention. This protocol does not
claim replay durability on its own; durable replay protection is solely the responsibility
of the external privileged authority.

The HMAC is not an OS-user authorization boundary. A key must never be generated, read, or
stored in the strategy process in real use. The issuer must instead be an external privileged
operator mechanism. A future executor must run under a separate OS user/service account,
own its broker credentials and verification keys, use one-shot IPC with peer authentication,
require fresh authoritative reconciliation and an operator action before any attempt, and
retain broker-side limits. The executor must not rely only on this protocol to decide broker
state.

This Phase 1 protocol cannot clear a halt and cannot submit an order.

## Test-only example

Tests inject `DeterministicNonProductionNonceAuthority`, an in-memory fake explicitly
labelled non-production. It only exercises the protocol: it tracks consumed canonical
bindings so a fresh verifier sharing the fake rejects replay. It is not a deployment pattern.
The suite also verifies missing/malformed authorities and forged/malformed receipts fail
closed, no local nonce-store public surface remains, and HMAC/type/TTL/allowlist/TOCTOU and
non-network/halt boundaries remain enforced.
