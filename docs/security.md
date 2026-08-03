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
- accidental adoption of an unrelated non-empty storage directory;
- path traversal, symlinks, undeclared files, and hash mismatches in Citefold backup archives;
- untracked correction or deletion of durable memory.

## Outside the boundary

Citefold does not protect against:

- hostile Python code in the same process;
- callers that already have unrestricted access to the memory root;
- an application constructing a false `MemoryScope` after failed authentication;
- unencrypted disks, backups, logs, or swap;
- a compromised model provider or incorrect provider policy statements;
- copies of data exported before a deletion request;
- retention or secure disposal of backups and displaced roots;
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

Use a dedicated Citefold root. Mixing uploads, logs, or unrelated files into it can expose those files through a whole-root backup and causes an uninitialized non-empty directory to fail closed.

## Migration, backup, and restore

The v0.1 → v0.2 migration creates and verifies a backup of durable store files before adding schema metadata. It holds existing v0.1 scope/ledger locks and checks the canonical file fingerprint before commit. If legacy data changes anyway, the transaction aborts and preserves the newer data. Migration recovery never rolls the whole root back from an older archive; it only removes or completes transaction-owned state/event/manifest metadata whose identities and hashes match.

The archive manifest records every included path, size, and SHA-256 hash plus a whole-store fingerprint. Restore rejects path traversal, symlinks, duplicate or undeclared entries, content mismatches, and a self-consistent archive that is not a valid Citefold store before swapping the target directory.

Integrity verification is not confidentiality. Backups contain the durable evidence, media, ledgers, projections, indexes, and deletion history present in the root. A backup created before hard deletion may still contain the removed asset bytes. Store archives on an encrypted, access-controlled location and apply deletion/retention rules to each copy.

Backup manifests are not signed or authenticated. Their hashes detect accidental corruption and internal mismatches, not malicious replacement by an actor who can rewrite both archive content and manifest.

`restore --replace` preserves the old non-empty root under a sibling `displaced_root` path. This makes operator rollback possible but creates another sensitive copy that Citefold does not erase automatically. Inspect it, keep it only as long as needed, and dispose of it according to host policy.

Restore writes a root-sibling intent journal only after the replacement has been extracted and validated. The journal, temporary replacement, and displaced root survive directory-swap interruptions; while the journal exists, inspection and normal APIs fail closed with `recovery_required`. Rerunning the same restore transaction can roll the validated replacement forward, even if its archive file was subsequently lost and the original archive path is passed again, provided the journal and temporary replacement remain intact. Protect the root's parent directory as sensitive state and do not manually alter transaction paths.

Normal v0.2 operations use a shared root lock; migration, backup, and restore use an exclusive root lock. Cross-process locking relies on POSIX advisory locking. Stop all v0.1 writers before migration because they do not know about this lock, and validate locking/atomic rename behavior on the deployment filesystem. See [Storage](storage.md) for the complete procedure.

## Deletion semantics

- Soft `forget` writes a tombstone and invalidates dependent memory.
- Hard `forget` also removes referenced asset bytes from the active root.
- Archive does not delete evidence.
- Neither operation proves deletion from backups, exported logs, or model-provider systems.

Applications should map user deletion requirements across every copy of the data, not only Citefold's active root.

## Deployment checklist

- [ ] Authenticate before constructing `MemoryScope`.
- [ ] Use one non-shared memory root or correctly scoped service account.
- [ ] Keep the Citefold root dedicated; store uploads and logs elsewhere.
- [ ] Restrict filesystem access and encrypt the underlying volume.
- [ ] Keep secrets in process-level secret management, not metadata.
- [ ] Decide whether model calls are permitted for each data class.
- [ ] Validate OpenRouter routing at deployment time if enabled.
- [ ] Keep model and media outputs below trusted system instructions.
- [ ] Test backup retention and hard-deletion procedures.
- [ ] Test journaled restore recovery, review the reported `displaced_root`, and securely expire it.
- [ ] Validate file-lock and atomic-replace behavior on the target filesystem.
- [ ] Pin a Citefold release, stop old writers, and rehearse migrations on a data copy before production data.

## Reporting vulnerabilities

Do not open a public issue for a suspected vulnerability. Follow the private process in [SECURITY.md](https://github.com/VIAIM/citefold/blob/main/SECURITY.md).
