# Storage, migration, backup, and restore

Citefold schema version 2 adds an explicit root manifest and root-wide maintenance operations. This is an embedded, local-filesystem contract: it is not a distributed database or a substitute for host-level backup, access control, or encryption.

## Use a dedicated root

The value passed to `Citefold(...)` or `--root` must be a directory used only for Citefold state. Do not place source media, application uploads, logs, or unrelated files inside it.

On first normal operation, a missing or empty root is initialized with `citefold-store.json`. A non-empty directory without a recognized Citefold manifest or legacy scope layout fails closed instead of being adopted. A manifest from a newer schema also fails closed.

Typical layout:

```text
{root}/
  citefold-store.json             # root format, schema, store, and generation IDs
  migration-events.jsonl          # completed schema transitions, when present
  tenants/{tenant}/users/{user}/namespaces/{namespace}/
    assets/sha256/                 # original and derived bytes
    ledgers/                       # authoritative JSONL history
    indexes/memory.sqlite3         # rebuildable index
    episodes/ profile/ tasks/ ... # rebuildable/human-readable projections
```

## Inspect without writing

Both commands below are read-only with respect to the storage root:

```bash
citefold --root /path/to/citefold-data status
citefold --root /path/to/citefold-data doctor
```

`status` reports one of these states:

| State | Meaning | Normal next action |
|---|---|---|
| `uninitialized` | Root is missing or empty | Run `init` or another normal operation |
| `current` | Recognized schema 2 store | Continue normally |
| `legacy` | Recognized implicit schema 1 / v0.1 store | Stop v0.1 writers, then preflight and migrate |
| `future` | Store is newer than this Citefold build | Upgrade the library; do not write |
| `recovery_required` | An interrupted migration left additive recovery state, or restore left an intent journal | Rerun the matching `migrate` or `restore` operation |
| `invalid` | Root or manifest is not recognized or is corrupt | Investigate; do not initialize over it |

Normal API and CLI memory operations refuse `legacy`, `future`, `recovery_required`, and `invalid` roots. `doctor` reports the state without silently initializing or migrating it.

## Upgrade a v0.1 root

Before migration:

1. Stop every v0.1 process that can access the root. v0.1 does not participate in the v0.2 root lock.
2. Upgrade all writers together. Do not point v0.1 code at a migrated schema 2 root.
3. Make sure the root is dedicated to Citefold and the backup destination is outside it.
4. Keep enough free space for a ZIP backup of all durable store files plus working space.

Run the read-only preflight first:

```bash
citefold --root /path/to/citefold-data migrate --dry-run
```

The plan checks the recognized source/target versions, JSONL readability, scope consistency, live asset hashes and sizes, active citation closure, unsafe files, backup placement, and available space. Missing bytes for an already hard-deleted asset are reported as a warning rather than a blocker.

If `ready` is `true`, migrate with either the default backup location beside the root or an explicit path:

```bash
citefold --root /path/to/citefold-data migrate

citefold --root /path/to/citefold-data migrate \
  --backup-to /secure/backups/citefold-before-v0.2.zip
```

The migration is an additive metadata transaction. Its sequence is:

1. hold the v0.2 exclusive root lock plus every existing v0.1 scope-writer and known ledger lock;
2. capture the canonical v0.1 file set, create the backup outside the root, and verify every archived file;
3. check that the legacy data did not change while the backup was created;
4. write an owned additive-recovery state and migration event;
5. check the canonical v0.1 files again, then commit the schema 2 root manifest;
6. remove the additive-recovery state after the manifest is durable.

The v0.1 → v0.2 transition adds only root management metadata. It does not rewrite the existing v0.1 ledgers, assets, indexes, or projections. If any legacy file changes despite the locks, migration aborts and leaves the newer canonical data in place. This is why old writers must still be stopped: the migration detects an uncooperative concurrent change, but never resolves it by discarding user data.

The verified backup is a recovery artifact for an operator; migration never automatically replaces the v0.1 root from it. When an interrupted migration is rerun, Citefold verifies the recorded backup and then does only one of two things with metadata owned by that transaction:

- if the manifest commit is complete and its hashes/identity match, remove the stale recovery state and report the store as current;
- if only the migration event/state was written, remove those matching owned files, keep all canonical v0.1 data, and retry the additive transition under the normal preflight rules.

Repeating `migrate` on a current root returns `up_to_date`. Keep the recorded backup available until recovery state is cleared. If an interrupted run used an explicit backup path and cleanup returns the store to `legacy`, choose a fresh path for the next backup because existing archives are never overwritten.

The checked-in compatibility test uses a store generated by the v0.1.0 implementation and validates this flow locally. That is regression evidence, not proof for every real deployment, filesystem, crash mode, or operator procedure. Rehearse the upgrade on a copy of your own data.

## Create and verify backups

```bash
citefold --root /path/to/citefold-data backup

citefold --root /path/to/citefold-data backup \
  --output /secure/backups/citefold-2026-08-03.zip
```

The default destination is a sibling directory named `{root}.backups`. A destination inside the root is rejected, and an existing archive is never overwritten.

Each backup is a ZIP with a Citefold backup manifest, per-file sizes and SHA-256 hashes, and a whole-store fingerprint. Citefold verifies the archive before returning success. Citefold runtime writer/ledger lock files and transient migration state are excluded; ordinary durable files whose names end in `.lock` are preserved alongside assets, ledgers, projections, indexes, legacy evidence, the schema manifest, and migration history.

The manifest is not signed or authenticated. Verification detects corruption and inconsistencies inside an archive; it does not prove who created an archive or detect a replacement performed by an attacker who can rewrite both its content and manifest.

Backups contain private source media and history that may no longer be active, including deletion tombstones. An archive made before hard deletion may still retain the bytes later removed from the active root. Protect every archive as sensitive data, encrypt it outside Citefold, define a retention/deletion policy, and test restoration.

## Restore a verified backup

Restore into a missing or empty root:

```bash
citefold --root /path/to/restored-citefold restore \
  /secure/backups/citefold-2026-08-03.zip
```

Replacing a non-empty root requires an explicit flag:

```bash
citefold --root /path/to/citefold-data restore \
  /secure/backups/citefold-2026-08-03.zip \
  --replace
```

Before extraction, Citefold rejects corrupt archives, undeclared or duplicate entries, path traversal, symlinks, and hash/fingerprint mismatches. With `--replace`, the previous root is moved to a sibling path like `.citefold-data.displaced-<id>` and is **not deleted automatically**. The JSON result reports `displaced_root`; inspect it, retain it until the restoration is accepted, then remove it under your own retention policy.

A restored schema 2 store receives a new generation ID so live v0.2 instances invalidate cached views. A restored v0.1 backup remains a legacy root and must be migrated before normal use.

### Interrupted restore journal

Restore extracts and validates the replacement first, then writes a root-sibling intent journal named `.{root-name}.citefold-restore.json` before moving either directory. The journal binds the root, archive digest, replacement identity, original-root identity, temporary path, and displaced path to one transaction. It is outside the root so it survives the directory swap.

If the process stops after that journal is durable but before the swap is finished, `status` and normal APIs report `recovery_required` even if the root path is temporarily missing or already contains the replacement. Do not delete or rename the journal, temporary directory, or displaced directory by hand.

Rerun `restore` for the same archive transaction to roll the validated temporary replacement forward and clear the journal:

```bash
citefold --root /path/to/citefold-data restore \
  /secure/backups/citefold-2026-08-03.zip \
  --replace
```

If the archive file has been lost after the journal was written, the same command with the **same original archive path** can still finish when the journal and validated replacement directory are intact; recovery uses their recorded identities rather than re-extracting the archive. A different path is accepted only when it points to an archive with the recorded digest. Changed archive content or inconsistent directory identity fails closed. When the replacement directory is no longer available but the original displaced root is intact, Citefold can put the original root back and requires a fresh restore attempt rather than inventing replacement data.

## Python API

The same operations are available without constructing `Citefold` or a `MemoryScope`:

```python
from pathlib import Path

from citefold import (
    backup_store,
    inspect_store,
    migrate_store,
    plan_migration,
    restore_store,
    verify_backup,
)

root = Path("/path/to/citefold-data")
migration_backup = Path("/secure/backups/pre-v0.2.zip")

status = inspect_store(root)          # read-only StoreStatus
plan = plan_migration(                # read-only MigrationPlan
    root,
    backup_path=migration_backup,
)

if status.state == "legacy" and plan.ready:
    migrated = migrate_store(
        root,
        backup_path=migration_backup,
    )
    print(migrated.status, migrated.backup_sha256)

backup = backup_store(root, Path("/secure/backups/current.zip"))
verified = verify_backup(backup.archive)
assert verified.verified

# This displaces a non-empty current root; retain and inspect the returned path.
restored = restore_store(root, backup.archive, replace=True)
print(restored.displaced_root)
```

`migrate_store(root, dry_run=True)` is also available and returns a `MigrationPlan`. In `MigrationResult`, `pre_fingerprint` and `post_fingerprint` describe the unchanged canonical v0.1 data set; the latter intentionally does not include the new schema manifest or migration event. Schema, migration, and backup-integrity failures use `StorageError` subclasses; ordinary filesystem failures such as an existing destination can still raise `OSError` subclasses. Callers should treat restoration and deletion of displaced roots as operator-controlled actions.

## Concurrency and platform boundary

Normal v0.2 operations take a shared root lock. Migration, backup, and restore take an exclusive root lock; per-scope and ledger serialization remains inside that root guard. Cross-process root locking uses POSIX advisory file locking, with in-process coordination as well.

These guarantees are intended for local POSIX filesystems. Windows cross-process parity, network/distributed filesystem locking, atomic rename behavior, multi-node coordination, and crashes at every possible filesystem boundary have not been established. Deterministic tests cover key interruptions before/after migration manifest commit and between restore directory swaps; they are not exhaustive crash injection or real power-loss evidence. Stop all writers for an upgrade or restore, even where the v0.2 lock would normally coordinate them, and validate behavior on the exact filesystem used in deployment.
