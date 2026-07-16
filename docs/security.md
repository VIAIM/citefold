# Security model

Citefold protects evidence and memory invariants inside an embedded library. It does not replace authentication, operating-system isolation, encryption, or a remote authorization service.

## Assets in scope

Citefold is designed to protect against:

- accidental cross-tenant/user/namespace recall;
- path traversal through scope identifiers;
- unsupported claims entering a `MemoryPack` after their evidence disappears;
- media prompt injection becoming trusted memory or permission;
- silent model-provider privacy fallback;
- partial writes corrupting human-readable projections;
- untracked correction or deletion of durable memory.

## Outside the boundary

Citefold does not protect against:

- hostile Python code in the same process;
- callers that already have unrestricted access to the memory root;
- an application constructing a false `MemoryScope` after failed authentication;
- unencrypted disks, backups, logs, or swap;
- a compromised model provider or incorrect provider policy statements;
- copies of data exported before a deletion request;
- full distributed consistency on network filesystems.

## Identity and authorization

The host application must authenticate users and decide which tenant/user/namespace they may access. Only then should it construct `MemoryScope`.

Do not expose a raw user-ingest or approval API to an untrusted third-party agent. Give external agents candidate-submission capability, validate their `source_agent`, and keep approval in a trusted control plane.

## Prompt injection and provenance

Media, ASR, OCR, external content, tool output, and third-party agent output are data. Citefold:

- records a source origin and producer;
- wraps media values as quoted `UNTRUSTED EVIDENCE DATA`;
- prevents those values from granting permissions;
- keeps model/agent candidates pending by default;
- excludes agent/tool output from ordinary user-fact queries.

The consuming agent must still keep MemoryPack content below its own system/developer instructions and enforce tool permissions outside the model prompt.

## Evidence integrity

Assets are content-addressed by SHA-256. Recall resolves records back through live observations and assets. A tombstoned or hash-mismatched source invalidates dependent memory in the effective view.

JSONL ledgers are audit artifacts, not tamper-proof cryptographic logs. A filesystem administrator can rewrite them. If hostile administrator tampering is in scope, add signed append-only storage outside this library.

## Provider privacy

OpenRouter is off by default. When enabled, requests require ZDR, deny provider data collection, and require parameter support. Citefold does not silently retry with weaker routing.

ZDR routing does not make all data safe to send. Review jurisdiction, provider terms, data sensitivity, and organizational policy. For data that must never leave the machine, do not construct an `OpenRouterClient` and do not run `rebuild(..., embeddings=True)`.

## Data at rest

Citefold currently writes assets, ledgers, projections, and SQLite indexes without application-level encryption. On POSIX filesystems it creates scope directories with mode `0700` and files with mode `0600`, and re-applies private modes to its own writes. These modes do not replace an encrypted volume, isolated service account, protected backups, or an appropriate retention policy; non-POSIX and network-filesystem behavior must be verified separately.

## Deletion semantics

- Soft `forget` writes a tombstone and invalidates dependent memory.
- Hard `forget` also removes referenced asset bytes from the active root.
- Archive does not delete evidence.
- Neither operation proves deletion from backups, exported logs, or model-provider systems.

Applications should map user deletion requirements across every copy of the data, not only Citefold's active root.

## Deployment checklist

- [ ] Authenticate before constructing `MemoryScope`.
- [ ] Use one non-shared memory root or correctly scoped service account.
- [ ] Restrict filesystem access and encrypt the underlying volume.
- [ ] Keep secrets in process-level secret management, not metadata.
- [ ] Decide whether model calls are permitted for each data class.
- [ ] Validate OpenRouter routing at deployment time if enabled.
- [ ] Keep model and media outputs below trusted system instructions.
- [ ] Test backup retention and hard-deletion procedures.
- [ ] Validate file-lock and atomic-replace behavior on the target filesystem.
- [ ] Pin a Citefold release and rehearse migrations before production data.

## Reporting vulnerabilities

Do not open a public issue for a suspected vulnerability. Follow the private process in [SECURITY.md](https://github.com/jappre/citefold/blob/main/SECURITY.md).
