# KIS production submitter process boundary

`KisProductionTradingClient` is a **client contract**, not an automated trading
runtime. It must not execute in the same general Python process as strategy,
research, notebook, CLI, or web application code.

A future real executor must run as a separately privileged process/service with:

1. a one-shot, explicit operator action;
2. fresh market data, account balance, and open-order reconciliation immediately
   before submission;
3. an independently managed per-broker symbol/account/order allowlist; and
4. operating-system credential separation plus broker-side limits.

The local audit digest and process-local integrity seals detect ordinary mistakes
or mutation; they do not authorize arbitrary code that already runs in the same
process. The current module does not load `.env`, issue OAuth tokens, provide a
CLI, retry broker operations, or submit amendments/cancellations. Its tests use
only injected fake transports.

## Ambiguous broker-state halt (Phase 1)

After audit creation, every transport exception, non-2xx response, malformed
response, missing broker acknowledgement, or KIS rejection creates a durable
`ambiguous-halt-<sha256>.json` marker inside the secure audit root. The marker is
bound by a SHA-256 digest to both the production account and opened audit-root
device/inode; it stores neither the raw account number nor tokens or credentials.
It is created write-once through the audit root's `O_NOFOLLOW` FD with `O_EXCL`,
secure file metadata, and file/directory `fsync`. A root replacement, symlink,
unsafe marker, or malformed marker fails closed.

Every new cash-limit submission first applies the disabled-by-default live gate,
then enters the account-scoped submission lock and checks this marker before audit
or POST. The halt applies to all future intents (including a new client using the
same account/root), not only the original idempotency key. There is deliberately
no public clear, automatic retry, or release path in Phase 1. Releasing a halt
requires a separately designed, privileged reconciliation workflow.

Before risk validation and audit, the submitter captures independently validated
primitive decision values into an immutable action snapshot. The snapshot itself is
risk-checked and supplies the audit intent, canonical ID, KIS wire body, and
`tr_id`; no post-audit transport decision rereads the caller's mutable intent.
Thus, a later caller-object mutation cannot change the audited or submitted action.
