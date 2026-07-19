# External durable nonce-consumption authority — inactive Phase 1

`external_nonce_authority.py` is an **installable design and local-test implementation only**. It starts no service, creates no OS users, generates no key, reads no `.env`, and contains no KIS, broker, order, HTTP, or generic TCP-listener code.

## Contract

- `DurableNonceAuthorityCore` accepts only the exact `ExternalNonceConsumptionRequest` from `privileged_protocol.py`. It rejects malformed or expired inputs before a consumption record is inserted. The request includes an executor-generated, opaque, exact 32-byte **per-invocation challenge**.
- It computes a request fingerprint, nonce fingerprint, and challenge digest, then uses a SQLite `BEGIN IMMEDIATE` transaction with UNIQUE constraints. The signed operator-envelope nonce remains the durable one-shot key: the nonce UNIQUE constraint rejects replay even when a caller supplies a different per-invocation challenge. The first request receives an exact typed receipt; replay (including after a fresh core opens the same DB) is rejected. There is **no expiry cleanup**: a consumed record is never deleted by this implementation, because deleting it could permit replay before a signed expiry.
- The database retains only fingerprints, challenge digest, and receipt digest—not raw nonce, raw invocation challenge, raw account reference, or key. Receipts/errors likewise do not carry those values.
- Each receipt carries only authority ID/version, SHA-256 request fingerprint/challenge digest/receipt digest, and a canonical unpadded-base64url Ed25519 signature—never a raw nonce, invocation challenge, account reference, or key. The signature covers the authority ID/version and the exact canonical consumption request (nonce, action fingerprint, account-binding digest, expiry, and current invocation challenge). The executor recomputes those values locally and verifies with a separately provisioned exact 32-byte raw Ed25519 **public** key; it cannot sign receipts. No HMAC receipt fallback exists. The fresh challenge detects a stale, cached, or spoofed wire response; it is not a substitute for the authority DB's durable envelope-nonce one-shot enforcement.
- `AuthorityIdentity` is frozen exact configuration: service ID, positive version, a numeric executor-UID allowlist, and absolute AF_UNIX socket path deployment metadata. The core separately requires an authority UID not in that allowlist.
- The handler is explicit only: it binds/starts nothing. A production caller must invoke its AF_UNIX `SO_PEERCRED` path. Same-UID authority peers and non-allowlisted peers are rejected. The wire is capped, length-prefixed canonical JSON with exact schema and duplicate-key rejection.

## Local filesystem boundary and limitations

For the local test template, the authority root (the immediate DB parent) must be owned by the current UID and mode `0700`; the DB must be a regular non-symlink file, current-UID owned, mode `0600`. Symlink and unsafe-root checks fail closed. SQLite WAL and FULL synchronous transactions are configured; sidecar files are restricted when created.

These checks do **not** claim that a same-UID attacker cannot delete/recreate the database or root. Durable replay protection requires a distinct OS principal, a durable filesystem, backups, and independent audit/monitoring. Do not claim that local SQLite alone is an append-only or anti-deletion authority.

## Deployment gate / runbook (do not execute from this repository)

This WSL session has no passwordless `sudo`; deployment is blocked. Before an authorized system administrator deploys anything:

1. Create distinct `kis-nonce-authority` and `kis-executor` OS principals. Never reuse the authority UID in the executor allowlist.
2. Provision an authority-owned `0700` state root on durable storage, with backup/restore procedures that preserve consumed state. Inject an independently audited exact 32-byte Ed25519 **private seed** only into the authority using an external secret manager/HSM or equivalent; no default generation exists. Do not put that seed in this repository, `.env`, executor configuration, unit file, or command line. Provision its 32-byte raw public verification key separately to the executor.
3. Review and adapt `deploy/systemd/*.template` outside the repo. Supply a separately reviewed launcher that explicitly binds only the configured AF_UNIX socket, owns it as the authority principal, and invokes `UnixAuthorityWireHandler.handle_socket`; never expose TCP/localhost transport.
4. Configure the executor to use only the AF_UNIX socket, run as `kis-executor`, and verify authority ID/version, request fingerprint/digest, and Ed25519 receipt signature through `ExternalNonceAuthorityAdapter` using only the provisioned public key before accepting a receipt.
5. Perform independent security review, backup/restore replay testing, audit integration, and operational incident procedures. Only then may an administrator install/enable units.

The templates are intentionally non-active: no `systemctl`, privilege escalation, account creation, key generation, service installation, socket bind, or KIS/order operation is part of Phase 1.
