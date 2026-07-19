# Production Deployment Preflight — Phase 1

## Scope and safety boundary

`python -m swing_v2.live.deployment_preflight` is a **read-only diagnosis-only command**. It cannot approve a provisioned host and it cannot output `ELIGIBLE_FOR_OPERATOR_REVIEW`: the local adapter intentionally has no trusted evidence of the active systemd-resolved unit configuration. It therefore reports `NOT_READY` until a separate root-owned evidence collector is designed, approved, and supplies verified evidence to the pure assessment core.

It performs no service activation, installation, `systemctl`, subprocess, shell, network, KIS, account, token, nonce, halt, or order action. It never reads an authority private key, token, account material, or `.env` content. Its narrow report contains only `ready_for_operator_review` and reason codes—never paths, key bytes, raw unit text, service status success, or trading readiness.

## Evidence required by the pure core

A future separately privileged collector must provide an exact, immutable snapshot of:

- distinct operator, authority, and executor principals, including exact supplementary GID memberships. The authority and executor production identities are fixed contract names: exactly `kis-nonce-authority` and `kis-executor`; they are not arbitrary replaceable accounts. A future change requires a separately reviewed configuration/contract change, tests, and deployment review;
- distinct authority/executor UIDs and complete service group sets: each service's primary GID and every supplementary GID must be disjoint from the other service's complete set. The authority cannot join the dedicated socket group; the executor may join it only to satisfy the exact socket-membership contract;
- units whose `User=` and `Group=` each match those exact fixed service-principal names; operator cannot reuse either fixed service account name, UID, private group, service supplementary group, or socket group;
- a dedicated socket group whose explicit members are exactly `{kis-executor}`. Authority does not join it; missing executor or any extra member is rejected;
- safe metadata for the required locations and only a bounded, strict ASCII Ed25519 **public** key declaration/digest;
- systemd-*resolved effective* values for each service, an exact boolean `effective_config_verified=True`, and a normalized SHA-256 digest. The digest is evidence metadata only; raw unit/drop-in content is never reported.

A base unit file is not effective configuration. `.service.d` drop-ins, directive resets, line continuations, and `ExecStart` prefixes can change it. Template parsing or `systemctl cat/show` output collected by this unprivileged command is not sufficient. A later root-owned collector must establish manager-provided effective evidence and its provenance; until then any unit presence is unverified and returns `effective_unit_config_unverified` / `NOT_READY`.

## Local filesystem diagnosis

Where it inspects the explicitly fixed public-key filename (`authority-ed25519.pub`), the local adapter anchors reads at a non-symlink directory FD (`O_DIRECTORY|O_NOFOLLOW`), opens the child with `O_NOFOLLOW`, verifies regular-file identity/ownership-mode safety and bounded size, then re-checks child and parent identity after reading. Traversal, symlinks, replacement races, unsafe metadata, invalid encoding, or unavailable primitives produce no key declaration and fail closed. It never opens the authority private-key path for content.

## Provisioning and later review

A separately authorized platform administrator must manually create distinct non-login service principals, private directories, a socket group dedicated solely to executor, public-key placement, peer-credential enforcement, and reviewed launchers. A second reviewer must review the externally collected effective-unit evidence and the pure-core report. No output of this Phase 1 CLI substitutes for that approval.

After an explicit approved deployment, a separate live safety review must independently verify installed unit identity, actual process ownership, AF_UNIX peer authentication/authorization, configuration binding, monitoring, emergency halt behavior, and the live-trading authorization boundary.
