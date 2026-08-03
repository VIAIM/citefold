from __future__ import annotations

import hashlib
import errno
import json
import os
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported runtime yet.
    fcntl = None


STORE_FORMAT = "citefold.local-store"
LEGACY_SCHEMA_VERSION = 1
CURRENT_SCHEMA_VERSION = 2
STORE_MANIFEST = "citefold-store.json"
MIGRATION_EVENTS = "migration-events.jsonl"
MIGRATION_STATE = "migration-state.json"
BACKUP_MANIFEST = "CITEFOLD_BACKUP_MANIFEST.json"
BACKUP_FORMAT = "citefold.backup"
BACKUP_FORMAT_VERSION = 1
RESTORE_JOURNAL_FORMAT = "citefold.restore-journal"
RESTORE_JOURNAL_VERSION = 1
MAX_RESTORE_JOURNAL_BYTES = 64 * 1024
MAX_BACKUP_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_BACKUP_TOTAL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024 * 1024
MAX_BACKUP_COMPRESSION_RATIO = 1000.0
_BACKUP_IO_CHUNK_BYTES = 1024 * 1024
_KNOWN_LEDGER_NAMES = (
    "assets",
    "observations",
    "episodes",
    "candidates",
    "records",
    "revisions",
    "deletions",
    "consolidations",
    "access",
    "model_calls",
)
_KNOWN_LEDGER_LOCK_NAMES = frozenset(
    f"{ledger_name}.jsonl.lock" for ledger_name in _KNOWN_LEDGER_NAMES
)


class StorageError(RuntimeError):
    pass


class UnrecognizedStoreError(StorageError):
    pass


class MigrationRequiredError(StorageError):
    pass


class UnsupportedSchemaVersionError(StorageError):
    pass


class MigrationPreflightError(StorageError):
    pass


class BackupValidationError(StorageError):
    pass


class MigrationRecoveryRequired(StorageError):
    pass


@dataclass(frozen=True)
class StoreStatus:
    root: Path
    state: str
    schema_version: int | None
    current_schema_version: int
    migration_required: bool
    recovery_required: bool
    store_id: str | None = None
    generation_id: str | None = None
    scope_count: int = 0
    issue: str | None = None


@dataclass(frozen=True)
class MigrationPlan:
    root: Path
    state: str
    source_version: int | None
    target_version: int
    ready: bool
    backup_path: Path
    scope_count: int
    file_count: int
    total_bytes: int
    fingerprint: str
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    schema_version: int | None
    file_count: int
    total_bytes: int
    fingerprint: str
    sha256: str
    verified: bool


@dataclass(frozen=True)
class MigrationResult:
    status: str
    root: Path
    source_version: int
    target_version: int
    backup_path: Path | None
    backup_sha256: str | None
    pre_fingerprint: str
    post_fingerprint: str
    event_id: str | None


@dataclass(frozen=True)
class RestoreResult:
    status: str
    root: Path
    archive: Path
    displaced_root: Path | None
    generation_id: str | None
    fingerprint: str


@dataclass(frozen=True)
class _FileEntry:
    path: str
    size: int
    sha256: str


class _RootRWLock:
    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.readers: dict[int, int] = {}
        self.writer: int | None = None
        self.writer_depth = 0
        self.writer_shared_depth: dict[int, int] = {}
        self.waiting_writers = 0

    def acquire_shared(self) -> bool:
        thread_id = threading.get_ident()
        with self.condition:
            if self.writer == thread_id:
                self.writer_shared_depth[thread_id] = self.writer_shared_depth.get(thread_id, 0) + 1
                return False
            if self.readers.get(thread_id, 0):
                self.readers[thread_id] += 1
                return False
            while self.writer is not None or self.waiting_writers:
                self.condition.wait()
            self.readers[thread_id] = 1
            return True

    def release_shared(self) -> None:
        thread_id = threading.get_ident()
        with self.condition:
            if self.writer_shared_depth.get(thread_id, 0):
                self.writer_shared_depth[thread_id] -= 1
                if not self.writer_shared_depth[thread_id]:
                    self.writer_shared_depth.pop(thread_id, None)
                return
            depth = self.readers.get(thread_id, 0)
            if not depth:
                raise RuntimeError("root shared lock released by a non-owner")
            if depth == 1:
                self.readers.pop(thread_id, None)
                self.condition.notify_all()
            else:
                self.readers[thread_id] = depth - 1

    def acquire_exclusive(self) -> bool:
        thread_id = threading.get_ident()
        with self.condition:
            if self.writer == thread_id:
                self.writer_depth += 1
                return False
            if self.readers.get(thread_id, 0):
                raise RuntimeError("cannot upgrade a root shared lock to exclusive")
            self.waiting_writers += 1
            try:
                while self.writer is not None or self.readers:
                    self.condition.wait()
                self.writer = thread_id
                self.writer_depth = 1
                return True
            finally:
                self.waiting_writers -= 1

    def release_exclusive(self) -> None:
        thread_id = threading.get_ident()
        with self.condition:
            if self.writer != thread_id:
                raise RuntimeError("root exclusive lock released by a non-owner")
            self.writer_depth -= 1
            if not self.writer_depth:
                self.writer = None
                self.condition.notify_all()


_ROOT_LOCKS: dict[str, _RootRWLock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _normalized_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve(strict=False)


def _root_lock_path(root: Path) -> Path:
    return root.parent / f".{root.name}.citefold.lock"


def _restore_journal_path(root: Path) -> Path:
    return root.parent / f".{root.name}.citefold-restore.json"


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _open_regular_file(path: Path, flags: int, mode: int = 0o600) -> int:
    safe_flags = flags
    if hasattr(os, "O_CLOEXEC"):
        safe_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        safe_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, safe_flags, mode)
    except OSError as exc:
        if path.is_symlink():
            raise StorageError(f"path must not be a symlink: {path}") from exc
        raise
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise StorageError(f"path must be a regular file: {path}")
    return descriptor


@contextmanager
def root_lock(root: str | Path, *, exclusive: bool) -> Iterator[None]:
    normalized = _normalized_root(root)
    key = str(normalized)
    with _ROOT_LOCKS_GUARD:
        local = _ROOT_LOCKS.setdefault(key, _RootRWLock())
    outermost = local.acquire_exclusive() if exclusive else local.acquire_shared()
    handle = None
    try:
        if outermost:
            normalized.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path = _root_lock_path(normalized)
            descriptor = _open_regular_file(lock_path, os.O_CREAT | os.O_RDWR)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "r+b")
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        if handle is not None:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        if exclusive:
            local.release_exclusive()
        else:
            local.release_shared()


def inspect_store(root: str | Path) -> StoreStatus:
    path = _normalized_root(root)
    if _path_exists(_restore_journal_path(path)):
        return _status(path, "recovery_required", None, issue="an interrupted restore requires recovery")
    if not path.exists():
        return _status(path, "uninitialized", None)
    if path.is_symlink() or not path.is_dir():
        return _status(path, "invalid", None, issue="storage root must be a real directory")
    if (path / MIGRATION_STATE).exists():
        return _status(path, "recovery_required", None, issue="an interrupted migration requires recovery")

    manifest_path = path / STORE_MANIFEST
    if manifest_path.exists():
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return _status(path, "invalid", None, issue="store manifest must be a regular file")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _status(path, "invalid", None, issue=f"store manifest is corrupt: {exc}")
        if not isinstance(manifest, dict) or manifest.get("format") != STORE_FORMAT:
            return _status(path, "invalid", None, issue="store manifest format is not recognized")
        version = manifest.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return _status(path, "invalid", None, issue="store manifest schema_version is invalid")
        if version > CURRENT_SCHEMA_VERSION:
            return _status(path, "future", version)
        if version != CURRENT_SCHEMA_VERSION:
            return _status(path, "invalid", version, issue="manifest version is not supported")
        store_id = manifest.get("store_id")
        generation_id = manifest.get("generation_id")
        if not isinstance(store_id, str) or not store_id or not isinstance(generation_id, str) or not generation_id:
            return _status(path, "invalid", version, issue="store manifest identity is incomplete")
        incomplete_scopes = [
            candidate
            for candidate in _legacy_scope_candidates(path)
            if not _has_known_ledger(candidate / "ledgers")
        ]
        if incomplete_scopes:
            relative = incomplete_scopes[0].relative_to(path)
            return _status(path, "invalid", version, issue=f"storage scope is incomplete: {relative}")
        return _status(
            path,
            "current",
            version,
            store_id=store_id,
            generation_id=generation_id,
        )

    try:
        if not any(path.iterdir()):
            return _status(path, "uninitialized", None)
    except OSError as exc:
        return _status(path, "invalid", None, issue=f"storage root cannot be inspected: {exc}")
    if _scope_roots(path):
        return _status(path, "legacy", LEGACY_SCHEMA_VERSION)
    return _status(path, "invalid", None, issue="non-empty directory is not a recognized Citefold store")


def _status(
    root: Path,
    state: str,
    version: int | None,
    *,
    issue: str | None = None,
    store_id: str | None = None,
    generation_id: str | None = None,
) -> StoreStatus:
    return StoreStatus(
        root=root,
        state=state,
        schema_version=version,
        current_schema_version=CURRENT_SCHEMA_VERSION,
        migration_required=state == "legacy",
        recovery_required=state == "recovery_required",
        store_id=store_id,
        generation_id=generation_id,
        scope_count=len(_scope_roots(root)) if root.is_dir() else 0,
        issue=issue,
    )


@contextmanager
def current_store(root: str | Path) -> Iterator[StoreStatus]:
    path = _normalized_root(root)
    status = inspect_store(path)
    if status.state == "uninitialized":
        with root_lock(path, exclusive=True):
            status = inspect_store(path)
            if status.state == "uninitialized":
                _initialize_store(path)
            elif status.state != "current":
                _raise_for_status(status)
    with root_lock(path, exclusive=False):
        status = inspect_store(path)
        _raise_for_status(status)
        yield status


def _raise_for_status(status: StoreStatus) -> None:
    if status.state == "current":
        return
    if status.state == "legacy":
        raise MigrationRequiredError(
            f"Citefold schema 1 data requires explicit migration; run "
            f"`citefold --root {status.root} migrate --dry-run` first"
        )
    if status.state == "future":
        raise UnsupportedSchemaVersionError(
            f"store schema {status.schema_version} is newer than supported schema {CURRENT_SCHEMA_VERSION}"
        )
    if status.state == "recovery_required":
        raise MigrationRecoveryRequired(status.issue or "migration recovery is required")
    raise UnrecognizedStoreError(status.issue or f"unrecognized storage root: {status.root}")


def _initialize_store(root: Path, *, migrated_from: int | None = None) -> dict[str, Any]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(root, 0o700)
    manifest = _new_store_manifest(migrated_from=migrated_from)
    _atomic_json(root / STORE_MANIFEST, manifest)
    return manifest


def _new_store_manifest(
    *,
    migrated_from: int | None = None,
    migration_id: str | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "format": STORE_FORMAT,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "store_id": str(uuid.uuid4()),
        "generation_id": str(uuid.uuid4()),
        "manifest_created_at": _now_iso(),
    }
    if migrated_from is not None:
        manifest["migrated_from_schema_version"] = migrated_from
    if migration_id is not None:
        manifest["migration_id"] = migration_id
    return manifest


def plan_migration(
    root: str | Path,
    target_version: int = CURRENT_SCHEMA_VERSION,
    backup_path: str | Path | None = None,
) -> MigrationPlan:
    path = _normalized_root(root)
    status = inspect_store(path)
    blockers: list[str] = []
    warnings: list[str] = []
    if target_version != CURRENT_SCHEMA_VERSION:
        blockers.append(f"target schema {target_version} is not supported")
    if status.state not in {"legacy", "current"}:
        blockers.append(status.issue or f"store state {status.state} cannot be migrated")
    elif status.state == "current" and status.schema_version != target_version:
        blockers.append(f"schema {status.schema_version} cannot migrate to {target_version}")

    entries: list[_FileEntry] = []
    if status.state in {"legacy", "current"}:
        entries, scan_blockers = _scan_store_files(path)
        blockers.extend(scan_blockers)
        if not scan_blockers:
            validation_blockers, validation_warnings = _validate_store(path)
            blockers.extend(validation_blockers)
            warnings.extend(validation_warnings)
        if status.state == "legacy":
            for name in (STORE_MANIFEST, MIGRATION_EVENTS, MIGRATION_STATE):
                if _path_exists(path / name):
                    blockers.append(f"reserved migration path already exists: {name}")

    destination = _backup_destination(path, status.schema_version, backup_path)
    if _is_within(destination, path):
        blockers.append("backup path must be outside the storage root")
    if _path_exists(destination):
        blockers.append(f"backup path already exists: {destination}")
    probe = destination.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir() or not os.access(probe, os.W_OK):
        blockers.append(f"backup parent is not writable: {destination.parent}")
    elif entries:
        free = shutil.disk_usage(probe).free
        required = sum(item.size for item in entries) + 1024 * 1024
        if free < required:
            blockers.append(f"insufficient free space for backup: need at least {required} bytes")

    return MigrationPlan(
        root=path,
        state=status.state,
        source_version=status.schema_version,
        target_version=target_version,
        ready=not blockers,
        backup_path=destination,
        scope_count=status.scope_count,
        file_count=len(entries),
        total_bytes=sum(item.size for item in entries),
        fingerprint=_fingerprint(entries),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def backup_store(root: str | Path, destination: str | Path | None = None) -> BackupResult:
    path = _normalized_root(root)
    with root_lock(path, exclusive=True):
        if _path_exists(_restore_journal_path(path)):
            raise MigrationRecoveryRequired("an interrupted restore must be completed before backup")
        status = inspect_store(path)
        if status.state not in {"legacy", "current", "future"}:
            _raise_for_status(status)
        target = _backup_destination(path, status.schema_version, destination)
        return _backup_store_unlocked(path, target, status)


def _backup_store_unlocked(
    root: Path,
    destination: Path,
    status: StoreStatus,
    *,
    entries: list[_FileEntry] | None = None,
) -> BackupResult:
    if _is_within(destination, root):
        raise MigrationPreflightError("backup path must be outside the storage root")
    if _path_exists(destination):
        raise FileExistsError(f"backup path already exists: {destination}")
    if entries is None:
        entries, blockers = _scan_store_files(root)
        if blockers:
            raise MigrationPreflightError("; ".join(blockers))
    if sum(entry.size for entry in entries) > MAX_BACKUP_TOTAL_UNCOMPRESSED_BYTES:
        raise MigrationPreflightError("store exceeds the backup uncompressed-size safety limit")
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and not parent_existed:
        os.chmod(destination.parent, 0o700)
    manifest = {
        "format": BACKUP_FORMAT,
        "backup_format_version": BACKUP_FORMAT_VERSION,
        "source_schema_version": status.schema_version,
        "source_store_id": status.store_id,
        "created_at": _now_iso(),
        "fingerprint": _fingerprint(entries),
        "files": [item.__dict__ for item in entries],
    }
    with tempfile.NamedTemporaryFile("wb", dir=destination.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(BACKUP_MANIFEST, _canonical_json(manifest))
            for entry in entries:
                archive.write(root / entry.path, f"store/{PurePosixPath(entry.path)}")
        if os.name == "posix":
            os.chmod(temporary_path, 0o600)
        _fsync_file(temporary_path)
        _verify_backup_archive(temporary_path)
        os.link(temporary_path, destination)
        temporary_path.unlink()
        _fsync_directory(destination.parent)
        if os.name == "posix":
            os.chmod(destination, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    verified = _verify_backup_archive(destination)
    return BackupResult(
        archive=destination,
        schema_version=verified["source_schema_version"],
        file_count=len(entries),
        total_bytes=sum(item.size for item in entries),
        fingerprint=verified["fingerprint"],
        sha256=_sha256_file(destination),
        verified=True,
    )


def verify_backup(archive: str | Path) -> BackupResult:
    path = Path(archive).expanduser().resolve(strict=False)
    manifest = _verify_backup_archive(path)
    entries = [
        _FileEntry(path=item["path"], size=item["size"], sha256=item["sha256"])
        for item in manifest["files"]
    ]
    return BackupResult(
        archive=path,
        schema_version=manifest["source_schema_version"],
        file_count=len(entries),
        total_bytes=sum(item.size for item in entries),
        fingerprint=manifest["fingerprint"],
        sha256=_sha256_file(path),
        verified=True,
    )


def migrate_store(
    root: str | Path,
    target_version: int = CURRENT_SCHEMA_VERSION,
    backup_path: str | Path | None = None,
    dry_run: bool = False,
) -> MigrationResult | MigrationPlan:
    path = _normalized_root(root)
    if dry_run:
        return plan_migration(path, target_version=target_version, backup_path=backup_path)
    with root_lock(path, exclusive=True):
        if _path_exists(_restore_journal_path(path)):
            raise MigrationRecoveryRequired("an interrupted restore must be completed with restore first")
        status = inspect_store(path)
        if status.state == "recovery_required":
            _recover_interrupted_migration(path)
            status = inspect_store(path)
        if status.state == "current" and status.schema_version == target_version:
            return MigrationResult(
                status="up_to_date",
                root=path,
                source_version=target_version,
                target_version=target_version,
                backup_path=None,
                backup_sha256=None,
                pre_fingerprint="",
                post_fingerprint="",
                event_id=None,
            )
        with _legacy_store_locks(path):
            status = inspect_store(path)
            plan = plan_migration(path, target_version=target_version, backup_path=backup_path)
            if not plan.ready:
                raise MigrationPreflightError("; ".join(plan.blockers))
            if plan.source_version != LEGACY_SCHEMA_VERSION:
                raise MigrationPreflightError(f"unsupported source schema: {plan.source_version}")
            original_entries, blockers = _scan_store_files(path)
            if blockers:
                raise MigrationPreflightError("; ".join(blockers))
            backup = _backup_store_unlocked(
                path,
                plan.backup_path,
                status,
                entries=original_entries,
            )
            current_entries, current_blockers = _scan_store_files(path)
            if current_blockers or current_entries != original_entries:
                raise MigrationPreflightError(
                    "legacy store changed while its backup was created; stop v0.1 writers and retry"
                )
            current_validation, _warnings = _validate_store(path)
            if current_validation:
                raise MigrationPreflightError("; ".join(current_validation))

            migration_id = uuid.uuid4().hex
            event_id = f"migration_{migration_id}"
            manifest = _new_store_manifest(
                migrated_from=LEGACY_SCHEMA_VERSION,
                migration_id=migration_id,
            )
            event = {
                "event_id": event_id,
                "migration_id": migration_id,
                "operation": "MIGRATE",
                "source_schema_version": LEGACY_SCHEMA_VERSION,
                "target_schema_version": target_version,
                "backup_path": str(backup.archive),
                "backup_sha256": backup.sha256,
                "pre_fingerprint": plan.fingerprint,
                "created_at": _now_iso(),
                "status": "completed",
            }
            manifest_text = _canonical_json(manifest) + "\n"
            event_text = _canonical_json(event) + "\n"
            state = {
                "format": "citefold.migration-state",
                "state_version": 1,
                "strategy": "additive-root-metadata",
                "migration_id": migration_id,
                "source_schema_version": LEGACY_SCHEMA_VERSION,
                "target_schema_version": target_version,
                "backup_path": str(backup.archive),
                "backup_sha256": backup.sha256,
                "pre_fingerprint": plan.fingerprint,
                "event_sha256": hashlib.sha256(event_text.encode("utf-8")).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
                "created_at": _now_iso(),
            }
            state_written = False
            result = MigrationResult(
                status="migrated",
                root=path,
                source_version=LEGACY_SCHEMA_VERSION,
                target_version=target_version,
                backup_path=backup.archive,
                backup_sha256=backup.sha256,
                pre_fingerprint=plan.fingerprint,
                post_fingerprint=plan.fingerprint,
                event_id=event_id,
            )
            try:
                _atomic_json(path / MIGRATION_STATE, state)
                state_written = True
                _atomic_write(path / MIGRATION_EVENTS, event_text)
                final_entries, final_blockers = _scan_legacy_data_files(path)
                if final_blockers or final_entries != original_entries:
                    raise MigrationPreflightError(
                        "legacy store changed before schema commit; no user data was rolled back"
                    )
                final_validation, _warnings = _validate_store(path)
                if final_validation:
                    raise MigrationPreflightError("; ".join(final_validation))
                _atomic_write(path / STORE_MANIFEST, manifest_text)
                written_manifest = _strict_json_object(path / STORE_MANIFEST)
                if (
                    written_manifest.get("format") != STORE_FORMAT
                    or written_manifest.get("schema_version") != target_version
                    or written_manifest.get("migration_id") != migration_id
                ):
                    raise MigrationPreflightError("postflight store manifest validation failed")
                _durable_unlink(path / MIGRATION_STATE)
                post = inspect_store(path)
                if post.state != "current" or post.schema_version != target_version:
                    raise MigrationPreflightError("postflight schema validation failed")
                return result
            except Exception:
                if state_written:
                    committed = _recover_interrupted_migration(path)
                    if committed:
                        return result
                raise


def restore_store(
    root: str | Path,
    archive: str | Path,
    replace: bool = False,
) -> RestoreResult:
    path = _normalized_root(root)
    backup_path = Path(archive).expanduser().resolve(strict=False)
    with root_lock(path, exclusive=True):
        journal_path = _restore_journal_path(path)
        if _path_exists(journal_path):
            journal = _read_restore_journal(path)
            journal_archive = Path(journal["archive_path"])
            if backup_path != journal_archive:
                if not backup_path.is_file() or _sha256_file(backup_path) != journal["archive_sha256"]:
                    raise MigrationRecoveryRequired("a different backup restore is already pending")
            elif backup_path.is_file() and _sha256_file(backup_path) != journal["archive_sha256"]:
                raise MigrationRecoveryRequired("the pending restore archive has changed")
            return _recover_restore_transaction(
                path,
                expected_archive_sha256=journal["archive_sha256"],
            )
        verify_backup(backup_path)
        return _restore_archive_unlocked(path, backup_path, replace=replace, refresh_generation=True)


def _restore_archive_unlocked(
    root: Path,
    archive_path: Path,
    *,
    replace: bool,
    refresh_generation: bool,
) -> RestoreResult:
    manifest = _verify_backup_archive(archive_path)
    if root.exists() and not root.is_dir():
        raise FileExistsError(f"restore target is not a directory: {root}")
    if root.exists() and any(root.iterdir()) and not replace:
        raise FileExistsError(f"restore target is not empty: {root}")
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    transaction_id = uuid.uuid4().hex
    temporary = root.parent / f".{root.name}.restore-{transaction_id}"
    displaced = root.parent / f".{root.name}.displaced-{transaction_id}"
    temporary.mkdir(mode=0o700)
    journal_path = _restore_journal_path(root)
    journal_written = False
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for item in manifest["files"]:
                relative = PurePosixPath(item["path"])
                member = f"store/{relative}"
                target = temporary.joinpath(*relative.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                if os.name == "posix":
                    os.chmod(target, 0o600)
        extracted, blockers = _scan_store_files(temporary)
        if blockers or _fingerprint(extracted) != manifest["fingerprint"]:
            raise BackupValidationError("extracted backup fingerprint does not match its manifest")
        _validate_extracted_store_schema(temporary, manifest.get("source_schema_version"))
        if refresh_generation and (temporary / STORE_MANIFEST).is_file():
            restored_manifest = _strict_json_object(temporary / STORE_MANIFEST)
            restored_manifest["generation_id"] = str(uuid.uuid4())
            restored_manifest["restored_at"] = _now_iso()
            _atomic_json(temporary / STORE_MANIFEST, restored_manifest)
        _validate_extracted_store_schema(temporary, manifest.get("source_schema_version"))
        replacement_entries, replacement_blockers = _scan_store_files(temporary)
        if replacement_blockers:
            raise BackupValidationError("restored store contains unsafe files")
        replacement_stat = temporary.stat()

        had_root = root.exists() and any(root.iterdir())
        original: dict[str, Any] = {
            "dev": None,
            "ino": None,
            "fingerprint": None,
        }
        if had_root:
            original_entries, original_blockers = _scan_store_files(root)
            if original_blockers:
                raise BackupValidationError("existing restore target contains unsafe files")
            original_stat = root.stat()
            original = {
                "dev": original_stat.st_dev,
                "ino": original_stat.st_ino,
                "fingerprint": _fingerprint(original_entries),
            }
        elif root.exists():
            root.rmdir()
            _fsync_directory(root.parent)
        _fsync_tree(temporary)
        journal = {
            "format": RESTORE_JOURNAL_FORMAT,
            "journal_version": RESTORE_JOURNAL_VERSION,
            "transaction_id": transaction_id,
            "root": str(root),
            "archive_path": str(archive_path),
            "archive_sha256": _sha256_file(archive_path),
            "backup_fingerprint": manifest["fingerprint"],
            "temporary_name": temporary.name,
            "displaced_name": displaced.name,
            "had_root": had_root,
            "original": original,
            "replacement": {
                "dev": replacement_stat.st_dev,
                "ino": replacement_stat.st_ino,
                "fingerprint": _fingerprint(replacement_entries),
                "schema_version": manifest.get("source_schema_version"),
            },
            "created_at": _now_iso(),
        }
        _atomic_json(journal_path, journal)
        journal_written = True
        return _recover_restore_transaction(root, expected_archive_sha256=journal["archive_sha256"])
    finally:
        if temporary.exists() and not journal_written and not _path_exists(journal_path):
            shutil.rmtree(temporary)


def _recover_restore_transaction(
    root: Path,
    *,
    expected_archive_sha256: str | None = None,
) -> RestoreResult:
    journal_path = _restore_journal_path(root)
    journal = _read_restore_journal(root)
    if expected_archive_sha256 is not None and journal["archive_sha256"] != expected_archive_sha256:
        raise MigrationRecoveryRequired("a different backup restore is already pending")
    temporary = root.parent / journal["temporary_name"]
    displaced = root.parent / journal["displaced_name"]
    had_root = journal["had_root"]

    root_kind = _restore_path_kind(root, journal)
    temporary_kind = _restore_path_kind(temporary, journal)
    displaced_kind = _restore_path_kind(displaced, journal)
    for label, kind in (("root", root_kind), ("temporary", temporary_kind), ("displaced", displaced_kind)):
        if kind == "unknown":
            raise MigrationRecoveryRequired(f"restore {label} has an unexpected identity")

    if root_kind == "replacement":
        if temporary_kind != "missing" or (had_root and displaced_kind != "original"):
            raise MigrationRecoveryRequired("restore directory state is inconsistent after installation")
        return _finish_restore_transaction(root, journal, displaced if had_root else None)

    if root_kind == "original":
        if displaced_kind != "missing" or temporary_kind != "replacement":
            raise MigrationRecoveryRequired("restore directory state is inconsistent before replacement")
        _durable_replace(root, displaced)
        _durable_replace(temporary, root)
        return _finish_restore_transaction(root, journal, displaced)

    if root_kind == "missing" and temporary_kind == "replacement":
        if had_root and displaced_kind != "original":
            raise MigrationRecoveryRequired("the displaced original store is missing")
        if not had_root and displaced_kind != "missing":
            raise MigrationRecoveryRequired("an unexpected displaced store exists")
        _durable_replace(temporary, root)
        return _finish_restore_transaction(root, journal, displaced if had_root else None)

    if root_kind == "missing" and temporary_kind == "missing" and displaced_kind == "original":
        _durable_replace(displaced, root)
        _durable_unlink(journal_path)
        raise MigrationRecoveryRequired("restore rolled back to the original store; retry the command")

    if root_kind == "missing" and not had_root and temporary_kind == "missing":
        raise MigrationRecoveryRequired("restore replacement data is missing")

    raise MigrationRecoveryRequired("restore directory state cannot be recovered automatically")


def _finish_restore_transaction(
    root: Path,
    journal: dict[str, Any],
    displaced: Path | None,
) -> RestoreResult:
    if _restore_path_kind(root, journal) != "replacement":
        raise MigrationRecoveryRequired("restored root failed identity validation")
    _validate_extracted_store_schema(root, journal["replacement"]["schema_version"])
    _durable_unlink(_restore_journal_path(root))
    status = inspect_store(root)
    return RestoreResult(
        status="restored",
        root=root,
        archive=Path(journal["archive_path"]),
        displaced_root=displaced,
        generation_id=status.generation_id,
        fingerprint=journal["backup_fingerprint"],
    )


def _read_restore_journal(root: Path) -> dict[str, Any]:
    path = _restore_journal_path(root)
    if path.is_symlink() or not path.is_file():
        raise MigrationRecoveryRequired("restore journal must be a regular file")
    if path.stat().st_size > MAX_RESTORE_JOURNAL_BYTES:
        raise MigrationRecoveryRequired("restore journal is too large")
    try:
        value = _strict_json_object(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationRecoveryRequired(f"restore journal is corrupt: {exc}") from exc
    if value.get("format") != RESTORE_JOURNAL_FORMAT or value.get("journal_version") != 1:
        raise MigrationRecoveryRequired("restore journal format is not supported")
    transaction_id = value.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or len(transaction_id) != 32
        or any(character not in "0123456789abcdef" for character in transaction_id)
    ):
        raise MigrationRecoveryRequired("restore journal transaction identity is invalid")
    if value.get("root") != str(root):
        raise MigrationRecoveryRequired("restore journal root does not match this store")
    expected_names = {
        "temporary_name": f".{root.name}.restore-{transaction_id}",
        "displaced_name": f".{root.name}.displaced-{transaction_id}",
    }
    for key, expected in expected_names.items():
        if value.get(key) != expected or Path(expected).name != expected:
            raise MigrationRecoveryRequired(f"restore journal {key} is invalid")
    if not isinstance(value.get("archive_path"), str) or not Path(value["archive_path"]).is_absolute():
        raise MigrationRecoveryRequired("restore journal archive path is invalid")
    if not _is_sha256(value.get("archive_sha256")):
        raise MigrationRecoveryRequired("restore journal archive digest is invalid")
    if not _is_sha256(value.get("backup_fingerprint")):
        raise MigrationRecoveryRequired("restore journal backup fingerprint is invalid")
    if not isinstance(value.get("had_root"), bool):
        raise MigrationRecoveryRequired("restore journal root state is invalid")
    for key in ("original", "replacement"):
        if not isinstance(value.get(key), dict):
            raise MigrationRecoveryRequired(f"restore journal {key} identity is invalid")
    original = value["original"]
    if value["had_root"]:
        if not all(isinstance(original.get(key), int) for key in ("dev", "ino")) or not _is_sha256(
            original.get("fingerprint")
        ):
            raise MigrationRecoveryRequired("restore journal original identity is invalid")
    elif any(original.get(key) is not None for key in ("dev", "ino", "fingerprint")):
        raise MigrationRecoveryRequired("restore journal empty-root identity is invalid")
    replacement = value["replacement"]
    if (
        not all(isinstance(replacement.get(key), int) for key in ("dev", "ino", "schema_version"))
        or not _is_sha256(replacement.get("fingerprint"))
        or replacement["schema_version"] < LEGACY_SCHEMA_VERSION
    ):
        raise MigrationRecoveryRequired("restore journal replacement identity is invalid")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _restore_path_kind(path: Path, journal: dict[str, Any]) -> str:
    if not _path_exists(path):
        return "missing"
    if path.is_symlink() or not path.is_dir():
        return "unknown"
    stat_value = path.stat()
    entries, blockers = _scan_store_files(path)
    if blockers:
        return "unknown"
    fingerprint = _fingerprint(entries)
    for kind in ("replacement", "original"):
        identity = journal[kind]
        if (
            identity.get("dev") == stat_value.st_dev
            and identity.get("ino") == stat_value.st_ino
            and identity.get("fingerprint") == fingerprint
        ):
            return kind
    return "unknown"


def _recover_interrupted_migration(root: Path) -> bool:
    state_path = root / MIGRATION_STATE
    try:
        state = _strict_json_object(state_path)
        if (
            state.get("format") != "citefold.migration-state"
            or state.get("state_version") != 1
            or state.get("strategy") != "additive-root-metadata"
        ):
            raise ValueError("unsupported migration state format")
        migration_id = str(state["migration_id"])
        if len(migration_id) != 32:
            raise ValueError("invalid migration identity")
        backup_path = Path(str(state["backup_path"])).expanduser().resolve(strict=False)
        expected_sha = str(state["backup_sha256"])
        expected_event_sha = str(state["event_sha256"])
        expected_manifest_sha = str(state["manifest_sha256"])
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationRecoveryRequired(f"migration state cannot be recovered: {exc}") from exc
    verified = verify_backup(backup_path)
    if verified.sha256 != expected_sha:
        raise MigrationRecoveryRequired("migration backup digest does not match recovery state")
    manifest_path = root / STORE_MANIFEST
    event_path = root / MIGRATION_EVENTS
    if _path_exists(manifest_path):
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or _sha256_file(manifest_path) != expected_manifest_sha
        ):
            raise MigrationRecoveryRequired("migration manifest does not match recovery state")
        if (
            event_path.is_symlink()
            or not event_path.is_file()
            or _sha256_file(event_path) != expected_event_sha
        ):
            raise MigrationRecoveryRequired("migration event does not match recovery state")
        _durable_unlink(state_path)
        return True
    if _path_exists(event_path):
        if event_path.is_symlink() or not event_path.is_file() or _sha256_file(event_path) != expected_event_sha:
            raise MigrationRecoveryRequired("migration event does not match recovery state")
        _durable_unlink(event_path)
    _durable_unlink(state_path)
    return False


def _validate_extracted_store_schema(root: Path, declared_schema: Any) -> None:
    if (
        not isinstance(declared_schema, int)
        or isinstance(declared_schema, bool)
        or declared_schema < LEGACY_SCHEMA_VERSION
    ):
        raise BackupValidationError("backup source schema is invalid")
    if declared_schema == LEGACY_SCHEMA_VERSION:
        valid = not (root / STORE_MANIFEST).exists() and bool(_scope_roots(root))
    else:
        try:
            store_manifest = _strict_json_object(root / STORE_MANIFEST)
        except (OSError, ValueError, json.JSONDecodeError):
            valid = False
        else:
            valid = (
                store_manifest.get("format") == STORE_FORMAT
                and store_manifest.get("schema_version") == declared_schema
                and isinstance(store_manifest.get("store_id"), str)
                and bool(store_manifest.get("store_id"))
                and isinstance(store_manifest.get("generation_id"), str)
                and bool(store_manifest.get("generation_id"))
            )
    if not valid:
        raise BackupValidationError(
            f"extracted backup is not a recognizable Citefold store for declared schema {declared_schema}"
        )
    blockers, _warnings = _validate_store(root)
    if blockers:
        raise BackupValidationError(
            "extracted backup failed Citefold store validation: " + "; ".join(blockers[:3])
        )


def _scan_store_files(root: Path) -> tuple[list[_FileEntry], list[str]]:
    entries: list[_FileEntry] = []
    blockers: list[str] = []
    if not root.is_dir():
        return entries, [f"storage root is not a directory: {root}"]
    try:
        paths = _walk_paths_no_symlinks(root)
    except OSError as exc:
        return entries, [f"storage root cannot be traversed safely: {exc}"]
    for path in paths:
        relative_path = path.relative_to(root)
        relative = relative_path.as_posix()
        if path.is_symlink():
            blockers.append(f"symlink is not allowed in a store: {relative}")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            blockers.append(f"non-regular file is not allowed in a store: {relative}")
            continue
        if _is_citefold_runtime_lock(relative_path) or relative_path == Path(MIGRATION_STATE):
            continue
        try:
            entries.append(_FileEntry(relative, path.stat().st_size, _sha256_file(path)))
        except OSError as exc:
            blockers.append(f"cannot read {relative}: {exc}")
    return entries, blockers


def _walk_paths_no_symlinks(root: Path) -> list[Path]:
    paths: list[Path] = []

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, directories, files in os.walk(
        root,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        current = Path(directory)
        for name in directories:
            paths.append(current / name)
        for name in files:
            paths.append(current / name)
    return sorted(paths)


def _scan_legacy_data_files(root: Path) -> tuple[list[_FileEntry], list[str]]:
    entries, blockers = _scan_store_files(root)
    management = {STORE_MANIFEST, MIGRATION_EVENTS, MIGRATION_STATE}
    return [entry for entry in entries if entry.path not in management], blockers


def _legacy_scope_candidates(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    tenants = root / "tenants"
    if tenants.is_symlink() or not tenants.is_dir():
        return []
    candidates: list[Path] = []
    for tenant in sorted(tenants.iterdir()):
        users = tenant / "users"
        if tenant.is_symlink() or not tenant.is_dir() or users.is_symlink() or not users.is_dir():
            continue
        for user in sorted(users.iterdir()):
            namespaces = user / "namespaces"
            if user.is_symlink() or not user.is_dir() or namespaces.is_symlink() or not namespaces.is_dir():
                continue
            for namespace in sorted(namespaces.iterdir()):
                if not namespace.is_symlink() and namespace.is_dir():
                    candidates.append(namespace)
    return candidates


@contextmanager
def _legacy_store_locks(root: Path) -> Iterator[None]:
    if fcntl is None:
        raise MigrationPreflightError("v0.1 migration requires POSIX file locking")
    scopes = _legacy_scope_candidates(root)
    descriptors: list[int] = []
    try:
        for scope in scopes:
            if scope.is_symlink() or not scope.is_dir():
                raise MigrationPreflightError(f"legacy scope is not a real directory: {scope}")
            ledgers = scope / "ledgers"
            if ledgers.is_symlink() or not ledgers.is_dir():
                raise MigrationPreflightError(f"legacy ledger directory is invalid: {ledgers}")
            descriptor = _open_regular_file(ledgers / ".writer.lock", os.O_CREAT | os.O_RDWR)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            descriptors.append(descriptor)
        if _legacy_scope_candidates(root) != scopes:
            raise MigrationPreflightError("legacy scope set changed while migration locks were acquired")
        ledger_locks = sorted(
            scope / "ledgers" / f"{name}.jsonl.lock"
            for scope in scopes
            for name in _KNOWN_LEDGER_NAMES
        )
        for lock_path in ledger_locks:
            descriptor = _open_regular_file(lock_path, os.O_CREAT | os.O_RDWR)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _is_citefold_runtime_lock(relative: Path) -> bool:
    parts = relative.parts
    if (
        len(parts) != 8
        or parts[0] != "tenants"
        or parts[2] != "users"
        or parts[4] != "namespaces"
        or parts[6] != "ledgers"
    ):
        return False
    name = parts[7]
    return name == ".writer.lock" or name in _KNOWN_LEDGER_LOCK_NAMES


def _validate_store(root: Path) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    allowed_root_names = {"tenants", STORE_MANIFEST, MIGRATION_EVENTS, MIGRATION_STATE}
    for path in root.iterdir():
        if path.name not in allowed_root_names:
            blockers.append(f"unrecognized path at storage root: {path.name}")
    complete_scopes = set(_scope_roots(root))
    for candidate in _legacy_scope_candidates(root):
        if candidate not in complete_scopes:
            blockers.append(f"storage scope is incomplete or unrecognized: {candidate.relative_to(root)}")
    try:
        paths = _walk_paths_no_symlinks(root)
    except OSError as exc:
        return [f"storage root cannot be traversed safely: {exc}"], warnings
    for path in (candidate for candidate in paths if candidate.suffix == ".jsonl"):
        if path.name.endswith(".lock"):
            continue
        try:
            records = _read_jsonl(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(str(exc))
            continue
        expected_scope = _scope_from_path(root, path)
        if expected_scope is None:
            continue
        relative_parts = path.relative_to(root).parts
        scope_required = len(relative_parts) > 6 and relative_parts[6] in {"ledgers", "evidence"}
        for index, record in enumerate(records, start=1):
            actual_scope = record.get("scope") if isinstance(record.get("scope"), dict) else record
            missing = [key for key in expected_scope if key not in actual_scope]
            if scope_required and missing:
                blockers.append(
                    f"missing scope metadata in {path.relative_to(root)} line {index}: "
                    f"{', '.join(missing)}"
                )
            for key, value in expected_scope.items():
                if key in actual_scope and actual_scope[key] != value:
                    blockers.append(
                        f"scope mismatch in {path.relative_to(root)} line {index}: "
                        f"expected {key}={value}"
                    )
    for scope_root in _scope_roots(root):
        scope_blockers, scope_warnings = _validate_scope_data(scope_root)
        blockers.extend(scope_blockers)
        warnings.extend(scope_warnings)
    return blockers, warnings


def _validate_scope_data(scope_root: Path) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    expected_scope = {
        "tenant_id": scope_root.parents[3].name,
        "user_id": scope_root.parents[1].name,
        "namespace": scope_root.name,
    }
    ledgers = scope_root / "ledgers"
    try:
        assets = _read_jsonl(ledgers / "assets.jsonl")
        observations = _by_id(_read_jsonl(ledgers / "observations.jsonl"), "observation_id")
        episodes = _by_id(_read_jsonl(ledgers / "episodes.jsonl"), "episode_id")
        deletions = _read_jsonl(ledgers / "deletions.jsonl")
        records = _current_records(
            _read_jsonl(ledgers / "records.jsonl"),
            _read_jsonl(ledgers / "revisions.jsonl"),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [str(exc)], warnings
    deleted = {str(item.get("target_ref")) for item in deletions if item.get("target_ref")}
    asset_map = _by_id(assets, "asset_id")
    valid_assets: set[str] = set()
    for asset_id, asset in asset_map.items():
        relative = asset.get("storage_path")
        asset_deleted = asset_id in deleted or f"asset:{asset_id}" in deleted
        path = _safe_child(scope_root, relative)
        if path is None:
            blockers.append(f"asset {asset_id} has unsafe storage_path: {relative}")
            continue
        if not path.is_file():
            if asset_deleted:
                warnings.append(f"deleted asset bytes are absent as expected: {asset_id}")
                continue
            blockers.append(f"live asset bytes are missing: {asset_id}")
            continue
        digest = _sha256_file(path)
        byte_size = asset.get("byte_size")
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
            blockers.append(f"asset {asset_id} has invalid byte_size: {byte_size!r}")
            continue
        if digest != asset.get("sha256") or path.stat().st_size != byte_size:
            blockers.append(f"asset bytes do not match ledger metadata: {asset_id}")
            continue
        if not asset_deleted:
            valid_assets.add(asset_id)

    def valid_observation(observation_id: str) -> bool:
        if observation_id in deleted or f"observation:{observation_id}" in deleted:
            return False
        observation = observations.get(observation_id)
        return observation is not None and observation.get("asset_id") in valid_assets

    def valid_ref(reference: str) -> bool:
        prefix, separator, identifier = reference.partition(":")
        if separator and prefix == "asset":
            return identifier in valid_assets
        if separator and prefix == "observation":
            return valid_observation(identifier)
        if separator and prefix == "episode":
            if identifier in deleted or f"episode:{identifier}" in deleted:
                return False
            episode = episodes.get(identifier)
            return bool(episode and episode.get("observation_ids")) and all(
                valid_observation(str(item)) for item in episode["observation_ids"]
            )
        path_value, anchor, evidence_id = reference.partition("#")
        path = _safe_child(scope_root, path_value)
        if not anchor or path is None or not path_value.startswith("evidence/") or not path.is_file():
            return False
        try:
            return any(
                item.get("id") == evidence_id
                and all(item.get(key) == value for key, value in expected_scope.items())
                for item in _read_jsonl(path)
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    for record in records.values():
        if record.get("status") != "active":
            continue
        references = record.get("evidence_refs")
        if not isinstance(references, list) or not references:
            blockers.append(f"active record has no evidence: {record.get('record_id')}")
            continue
        invalid = [str(reference) for reference in references if not valid_ref(str(reference))]
        if invalid:
            blockers.append(
                f"active record {record.get('record_id')} has invalid evidence: {', '.join(invalid)}"
            )
    return blockers, warnings


def _verify_backup_archive(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BackupValidationError(f"backup archive does not exist: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise BackupValidationError("backup contains duplicate archive paths")
            total_uncompressed = 0
            for info in infos:
                name = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (
                    info.filename != name.as_posix()
                    or "\\" in info.filename
                    or name.is_absolute()
                    or ".." in name.parts
                    or not name.parts
                    or stat.S_ISLNK(mode)
                ):
                    raise BackupValidationError(f"unsafe backup entry: {info.filename}")
                if info.filename == BACKUP_MANIFEST:
                    if info.file_size > MAX_BACKUP_MANIFEST_BYTES:
                        raise BackupValidationError("backup manifest is too large")
                    continue
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_BACKUP_TOTAL_UNCOMPRESSED_BYTES:
                    raise BackupValidationError("backup uncompressed size exceeds the safety limit")
                if info.file_size and (
                    not info.compress_size
                    or info.file_size / info.compress_size > MAX_BACKUP_COMPRESSION_RATIO
                ):
                    raise BackupValidationError(
                        f"backup entry exceeds the compression ratio limit: {info.filename}"
                    )
            if names.count(BACKUP_MANIFEST) != 1:
                raise BackupValidationError("backup manifest is missing or duplicated")
            manifest_info = archive.getinfo(BACKUP_MANIFEST)
            manifest_bytes = _read_zip_member_limited(
                archive,
                manifest_info,
                MAX_BACKUP_MANIFEST_BYTES,
            )
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            if not isinstance(manifest, dict):
                raise BackupValidationError("backup manifest must be a JSON object")
            if manifest.get("format") != BACKUP_FORMAT or manifest.get("backup_format_version") != 1:
                raise BackupValidationError("backup format is not supported")
            files = manifest.get("files")
            if not isinstance(files, list):
                raise BackupValidationError("backup file manifest is invalid")
            entries: list[_FileEntry] = []
            expected_names = {BACKUP_MANIFEST}
            entry_paths: set[str] = set()
            for item in files:
                if not isinstance(item, dict):
                    raise BackupValidationError("backup file entry must be an object")
                try:
                    entry_path = item["path"]
                    entry_size = item["size"]
                    entry_sha = item["sha256"]
                except KeyError as exc:
                    raise BackupValidationError(f"invalid backup file entry: {exc}") from exc
                if not isinstance(entry_path, str):
                    raise BackupValidationError("backup file path must be a string")
                if not isinstance(entry_size, int) or isinstance(entry_size, bool) or entry_size < 0:
                    raise BackupValidationError(f"invalid backup file size: {entry_size!r}")
                if (
                    not isinstance(entry_sha, str)
                    or len(entry_sha) != 64
                    or any(character not in "0123456789abcdef" for character in entry_sha)
                ):
                    raise BackupValidationError("backup file sha256 is invalid")
                entry = _FileEntry(entry_path, entry_size, entry_sha)
                relative = PurePosixPath(entry.path)
                if (
                    entry.path != relative.as_posix()
                    or "\\" in entry.path
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                ):
                    raise BackupValidationError(f"backup path is not canonical: {entry.path}")
                if entry.path in entry_paths:
                    raise BackupValidationError(f"duplicate backup manifest path: {entry.path}")
                entry_paths.add(entry.path)
                member = f"store/{relative}"
                expected_names.add(member)
                try:
                    info = archive.getinfo(member)
                except KeyError as exc:
                    raise BackupValidationError(f"backup file is missing: {entry.path}") from exc
                if info.file_size != entry.size:
                    raise BackupValidationError(f"backup file size mismatch: {entry.path}")
                size, digest = _hash_zip_member(archive, info, entry.size)
                if size != entry.size or digest != entry.sha256:
                    raise BackupValidationError(f"backup file hash mismatch: {entry.path}")
                entries.append(entry)
            if set(names) != expected_names:
                extras = sorted(set(names) - expected_names)
                raise BackupValidationError(f"backup contains undeclared files: {extras}")
            if manifest.get("fingerprint") != _fingerprint(entries):
                raise BackupValidationError("backup fingerprint does not match file manifest")
            return manifest
    except BackupValidationError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupValidationError(f"backup archive is corrupt: {exc}") from exc


def _read_zip_member_limited(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
) -> bytes:
    content = bytearray()
    with archive.open(info) as source:
        while True:
            chunk = source.read(min(_BACKUP_IO_CHUNK_BYTES, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limit:
                raise BackupValidationError(f"backup entry is too large: {info.filename}")
    return bytes(content)


def _hash_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    expected_size: int,
) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with archive.open(info) as source:
        for chunk in iter(lambda: source.read(_BACKUP_IO_CHUNK_BYTES), b""):
            size += len(chunk)
            if size > expected_size:
                raise BackupValidationError(f"backup file exceeds its declared size: {info.filename}")
            digest.update(chunk)
    return size, digest.hexdigest()


def _scope_roots(root: Path) -> list[Path]:
    return [path for path in _legacy_scope_candidates(root) if _has_known_ledger(path / "ledgers")]


def _has_known_ledger(ledgers: Path) -> bool:
    return ledgers.is_dir() and not ledgers.is_symlink() and all(
        (ledgers / f"{name}.jsonl").is_file()
        and not (ledgers / f"{name}.jsonl").is_symlink()
        for name in _KNOWN_LEDGER_NAMES
    )


def _scope_from_path(root: Path, path: Path) -> dict[str, str] | None:
    parts = path.relative_to(root).parts
    if len(parts) < 7 or parts[0] != "tenants" or parts[2] != "users" or parts[4] != "namespaces":
        return None
    return {"tenant_id": parts[1], "user_id": parts[3], "namespace": parts[5]}


def _current_records(records: list[dict[str, Any]], revisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("record_id"):
            current.setdefault(str(record["record_id"]), record)
    for revision in revisions:
        previous = revision.get("previous_record")
        if isinstance(previous, dict) and previous.get("record_id"):
            current[str(previous["record_id"])] = previous
        record = revision.get("record")
        if isinstance(record, dict) and record.get("record_id"):
            current[str(record["record_id"])] = record
    return current


def _by_id(records: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(item[key]): item for item in records if item.get(key)}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt JSONL {path} at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL {path} line {line_number} is not an object")
        result.append(value)
    return result


def _safe_child(root: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if any(part == ".." for part in PurePosixPath(relative).parts):
        return None
    return candidate


def _backup_destination(root: Path, version: int | None, value: str | Path | None) -> Path:
    if value is not None:
        return Path(value).expanduser().resolve(strict=False)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root.parent / f"{root.name}.backups"
    return directory / f"{root.name}-schema{version or 'unknown'}-{timestamp}-{uuid.uuid4().hex[:8]}.zip"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _fingerprint(entries: list[_FileEntry]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.path):
        digest.update(entry.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write(path, _canonical_json(value) + "\n")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        if os.name == "posix":
            os.chmod(temporary_path, 0o600)
        temporary_path.replace(path)
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _fsync_file(path: Path) -> None:
    descriptor = _open_regular_file(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
                raise
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    paths = _walk_paths_no_symlinks(root)
    files = [path for path in paths if path.is_file() and not path.is_symlink()]
    for path in files:
        _fsync_file(path)
    directories = sorted(
        [path for path in paths if path.is_dir() and not path.is_symlink()],
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        _fsync_directory(path)
    _fsync_directory(root)
    _fsync_directory(root.parent)


def _durable_replace(source: Path, target: Path) -> None:
    if source.parent != target.parent:
        raise StorageError("durable directory replacement requires a shared parent")
    source.replace(target)
    _fsync_directory(target.parent)


def _durable_unlink(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
