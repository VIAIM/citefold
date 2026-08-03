from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .models import (
    Asset,
    Episode,
    EvidenceValidationError,
    MemoryCandidate,
    MemoryRecord,
    MemoryScope,
    Observation,
    Revision,
    finite_number,
    unit_interval,
)
from .storage import StorageError, current_store

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported runtime yet.
    fcntl = None


Clock = Callable[[], datetime]
LEDGER_NAMES = (
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
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _chmod_private(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod_private(path, 0o700)


@contextmanager
def _open_regular_lock(path: Path) -> Iterator[Any]:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        if path.is_symlink():
            raise StorageError(f"lock path must not be a symlink: {path}") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StorageError(f"lock path must be a regular file: {path}")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class LedgerStore:
    """Append-only canonical store plus content-addressed raw assets.

    Markdown and SQLite files are projections. The ledgers under ``ledgers/``
    are sufficient to reconstruct trusted records and their evidence chain.
    """

    def __init__(self, root: str | Path, clock: Clock | None = None) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._asset_validation_cache: dict[tuple[str, int, int, int, int], bool] = {}
        self._ledger_cache: dict[str, tuple[int, int, tuple[dict[str, Any], ...]]] = {}
        self._ledger_index_cache: dict[tuple[str, str], tuple[int, int, dict[str, dict[str, Any]]]] = {}
        self._unique_key_cache: dict[tuple[str, str], tuple[int, int, set[Any]]] = {}
        self._ensured_scopes: set[str] = set()
        self._writer_local = threading.local()
        self._schema_generation: str | None = None

    @property
    def schema_generation(self) -> str | None:
        return self._schema_generation

    def scope_root(self, scope: MemoryScope) -> Path:
        if not isinstance(scope, MemoryScope):
            raise TypeError("scope must be a MemoryScope")
        return (
            self.root
            / "tenants"
            / scope.tenant_id
            / "users"
            / scope.user_id
            / "namespaces"
            / scope.namespace
        )

    def ensure_scope(self, scope: MemoryScope) -> Path:
        with current_store(self.root) as status:
            self._sync_generation(status.generation_id)
            return self._ensure_scope_unlocked(scope)

    def _ensure_scope_unlocked(self, scope: MemoryScope) -> Path:
        root = self._prepare_scope_lock(scope)
        root_key = str(root)

        def raise_walk_error(error: OSError) -> None:
            raise error

        for directory, directories, files in os.walk(
            root,
            onerror=raise_walk_error,
            followlinks=False,
        ):
            for name in [*directories, *files]:
                path = Path(directory) / name
                if path.is_symlink():
                    raise StorageError(f"symlink is not allowed in a storage scope: {path}")
        if root_key in self._ensured_scopes:
            return root
        root_existed = self.root.exists()
        root_was_empty = root_existed and self.root.is_dir() and not any(self.root.iterdir())
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not root_existed or root_was_empty or (self.root / "tenants").exists():
            _chmod_private(self.root, 0o700)
        for rel in ("assets/sha256", "ledgers", "indexes"):
            self._ensure_scope_directory(root, rel)
        for name in LEDGER_NAMES:
            path = self.ledger_path(scope, name)
            if not path.exists():
                self._atomic_write(path, "")
        self._ensured_scopes.add(root_key)
        return root

    def ensure_scope_directory(self, scope: MemoryScope, relative: str) -> Path:
        return self._ensure_scope_directory(self.scope_root(scope), relative)

    def _prepare_scope_lock(self, scope: MemoryScope) -> Path:
        root_boundary = self.root.expanduser().resolve(strict=False)
        current = self.root
        for part in (
            "tenants",
            scope.tenant_id,
            "users",
            scope.user_id,
            "namespaces",
            scope.namespace,
        ):
            current /= part
            if current.is_symlink():
                raise StorageError(f"symlink is not allowed in a storage scope: {current}")
            _ensure_private_directory(current)
            try:
                current.resolve(strict=False).relative_to(root_boundary)
            except ValueError as exc:
                raise StorageError(f"storage scope escapes its root: {current}") from exc
        self._ensure_scope_directory(current, "ledgers")
        return current

    def _ensure_scope_directory(self, scope_root: Path, relative: str) -> Path:
        value = Path(relative)
        if value.is_absolute() or not value.parts or ".." in value.parts:
            raise StorageError(f"unsafe storage directory: {relative}")
        boundary = scope_root.resolve(strict=False)
        current = scope_root
        for part in value.parts:
            current /= part
            if current.is_symlink():
                raise StorageError(f"symlink is not allowed in a storage scope: {current}")
            _ensure_private_directory(current)
            try:
                current.resolve(strict=False).relative_to(boundary)
            except ValueError as exc:
                raise StorageError(f"storage directory escapes its scope: {current}") from exc
        return current

    def ledger_path(self, scope: MemoryScope, name: str) -> Path:
        if name not in LEDGER_NAMES:
            raise ValueError(f"Unknown ledger: {name}")
        return self.scope_root(scope) / "ledgers" / f"{name}.jsonl"

    @contextmanager
    def scope_writer(self, scope: MemoryScope) -> Iterator[None]:
        """Serialize a complete high-level mutation for one storage scope."""
        with current_store(self.root) as status:
            self._sync_generation(status.generation_id)
            scope_root = self.scope_root(scope)
            scope_existed = scope_root.exists()
            try:
                root = self._prepare_scope_lock(scope)
            except Exception:
                if not scope_existed:
                    self._remove_empty_scope_scaffold(scope_root)
                raise
            key = str(root.resolve())
            with _THREAD_LOCKS_GUARD:
                thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
            with thread_lock:
                depths = getattr(self._writer_local, "depths", {})
                depth = int(depths.get(key, 0))
                self._writer_local.depths = depths
                if depth:
                    depths[key] = depth + 1
                    try:
                        yield
                    finally:
                        depths[key] -= 1
                    return
                lock_path = root / "ledgers" / ".writer.lock"
                with ExitStack() as stack:
                    try:
                        lock = stack.enter_context(_open_regular_lock(lock_path))
                    except Exception:
                        if not scope_existed:
                            self._remove_empty_scope_scaffold(scope_root)
                        raise
                    if fcntl is not None:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    depths[key] = 1
                    try:
                        self._ensure_scope_unlocked(scope)
                        yield
                    finally:
                        depths.pop(key, None)
                        if fcntl is not None:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _remove_empty_scope_scaffold(self, scope_root: Path) -> None:
        ledgers = scope_root / "ledgers"
        try:
            if os.path.lexists(ledgers):
                if not ledgers.is_dir() or ledgers.is_symlink() or any(ledgers.iterdir()):
                    return
                ledgers.rmdir()
            if not scope_root.is_dir() or scope_root.is_symlink():
                return
            current = scope_root
            while current != self.root:
                current.rmdir()
                current = current.parent
        except OSError:
            return

    def register_asset(
        self,
        scope: MemoryScope,
        data: bytes,
        mime_type: str,
        source: str,
        captured_at: str | None = None,
        occurred_at: str | None = None,
        privacy_policy: dict[str, Any] | None = None,
        original_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Asset, bool]:
        root = self.ensure_scope(scope)
        digest = hashlib.sha256(data).hexdigest()
        asset_id = f"asset_{digest[:24]}"
        deleted = self.deleted_refs(scope)
        if asset_id in deleted or f"asset:{asset_id}" in deleted:
            raise EvidenceValidationError(
                "This exact asset was previously deleted; explicit restore is required before re-ingestion"
            )
        suffix = self._asset_suffix(mime_type, original_name)
        rel = Path("assets") / "sha256" / digest[:2] / f"{digest}{suffix}"
        path = root / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_bytes(path, data)
        now = captured_at or self.now_iso()
        asset = Asset(
            asset_id=asset_id,
            mime_type=mime_type,
            sha256=digest,
            storage_path=str(rel),
            scope=scope.as_record(),
            source=source,
            captured_at=now,
            occurred_at=occurred_at,
            privacy_policy=privacy_policy or {"classification": "private"},
            original_name=original_name,
            byte_size=len(data),
            metadata=metadata or {},
        )
        appended = self.append_unique(scope, "assets", asset.as_record(), "asset_id")
        return asset, appended

    def append_observation(
        self,
        scope: MemoryScope,
        asset_id: str,
        modality: str,
        locator: dict[str, Any],
        content: str,
        producer_type: str,
        producer_model: str | None,
        confidence: float,
        source_origin: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Observation, bool]:
        self.ensure_scope(scope)
        observation_id = self.stable_id(
            "obs",
            asset_id,
            modality,
            self.canonical_json(locator),
            content,
            producer_type,
            producer_model or "",
            source_origin,
        )
        observation = Observation(
            observation_id=observation_id,
            asset_id=asset_id,
            modality=modality,
            locator=locator,
            content=content,
            scope=scope.as_record(),
            producer_type=producer_type,
            producer_model=producer_model,
            confidence=unit_interval(confidence, "confidence"),
            source_origin=source_origin,
            created_at=self.now_iso(),
            metadata=metadata or {},
        )
        appended = self.append_unique(scope, "observations", observation.as_record(), "observation_id")
        return observation, appended

    def append_episode(
        self,
        scope: MemoryScope,
        observation_ids: list[str],
        summary: str,
        source_origin: str,
        start_at: str | None = None,
        end_at: str | None = None,
        participants: list[str] | None = None,
        scene: str | None = None,
        topics: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Episode, bool]:
        if not observation_ids:
            raise EvidenceValidationError("Episode requires at least one Observation")
        known_observations = self.observations(scope)
        missing = [observation_id for observation_id in observation_ids if observation_id not in known_observations]
        if missing:
            raise EvidenceValidationError(f"Episode references unknown observations: {', '.join(missing)}")
        now = self.now_iso()
        fingerprint = idempotency_key or self.canonical_json(
            {
                "observations": observation_ids,
                "summary": summary,
                "source_origin": source_origin,
                "scene": scene,
            }
        )
        episode_id = self.stable_id("episode", *scope.as_record().values(), fingerprint)
        episode = Episode(
            episode_id=episode_id,
            start_at=start_at or now,
            end_at=end_at or start_at or now,
            participants=participants or [],
            summary=summary,
            observation_ids=list(dict.fromkeys(observation_ids)),
            scope=scope.as_record(),
            scene=scene,
            topics=topics or [],
            source_origin=source_origin,
            created_at=now,
            metadata=metadata or {},
        )
        appended = self.append_unique(scope, "episodes", episode.as_record(), "episode_id")
        if not appended:
            existing = next(
                item
                for item in self.read_ledger(scope, "episodes")
                if item.get("episode_id") == episode_id
            )
            episode = Episode(**existing)
        return episode, appended

    def append_candidate(self, scope: MemoryScope, candidate: MemoryCandidate) -> bool:
        self.ensure_scope(scope)
        return self.append_unique(scope, "candidates", candidate.as_record(), "candidate_id")

    def append_candidate_state(
        self,
        scope: MemoryScope,
        candidate: dict[str, Any],
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        state = dict(candidate)
        state["status"] = status
        state["state_changed_at"] = self.now_iso()
        if metadata:
            state.setdefault("metadata", {}).update(metadata)
        self.append(scope, "candidates", state)

    def append_record(self, scope: MemoryScope, record: MemoryRecord) -> bool:
        self.ensure_scope(scope)
        return self.append_unique(scope, "records", record.as_record(), "record_id")

    def append_revision(self, scope: MemoryScope, revision: Revision) -> bool:
        self.ensure_scope(scope)
        return self.append_unique(scope, "revisions", revision.as_record(), "idempotency_key")

    def append_deletion(
        self,
        scope: MemoryScope,
        target_ref: str,
        actor: str,
        reason: str,
        hard: bool = False,
    ) -> dict[str, Any]:
        deletion = {
            "deletion_id": self.stable_id("delete", target_ref, actor, reason),
            "target_ref": target_ref,
            "actor": actor,
            "reason": reason,
            "hard": hard,
            "created_at": self.now_iso(),
            "scope": scope.as_record(),
        }
        self.append_unique(scope, "deletions", deletion, "deletion_id")
        return deletion

    def read_ledger(self, scope: MemoryScope, name: str) -> list[dict[str, Any]]:
        path = self.ledger_path(scope, name)
        if not path.exists():
            return []
        stat = path.stat()
        cached = self._ledger_cache.get(str(path))
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return [dict(item) for item in cached[2]]
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Corrupt ledger {path.name} at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Ledger {path.name} line {line_number} is not an object")
            records.append(value)
        self._ledger_cache[str(path)] = (stat.st_mtime_ns, stat.st_size, tuple(records))
        return [dict(item) for item in records]

    def current_candidates(self, scope: MemoryScope) -> dict[str, dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        for candidate in self.read_ledger(scope, "candidates"):
            current[candidate["candidate_id"]] = candidate
        return current

    def current_records(self, scope: MemoryScope, include_inactive: bool = True) -> list[dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        for record in self.read_ledger(scope, "records"):
            current.setdefault(record["record_id"], record)
        for revision in self.read_ledger(scope, "revisions"):
            previous = revision.get("previous_record")
            if isinstance(previous, dict) and previous.get("record_id"):
                current[previous["record_id"]] = previous
            record = revision.get("record")
            if isinstance(record, dict) and record.get("record_id"):
                current[record["record_id"]] = record
        access_strengths = {
            event["record_id"]: float(event["access_strength"])
            for event in self.read_ledger(scope, "access")
            if event.get("record_id") and "access_strength" in event
        }
        projected = [
            {**record, "access_strength": access_strengths.get(record["record_id"], record.get("access_strength", 1.0))}
            for record in current.values()
        ]
        records = sorted(
            projected,
            key=lambda item: (item.get("valid_from", ""), item.get("record_id", "")),
        )
        if include_inactive:
            return records
        return [record for record in records if record.get("status") == "active"]

    def effective_records(self, scope: MemoryScope, include_inactive: bool = True) -> list[dict[str, Any]]:
        """Return the usable record state after applying evidence integrity checks.

        The revision ledger remains append-only audit history. If an external
        filesystem change invalidates evidence without going through
        ``forget()``, dependent live records are projected as deleted so they
        cannot be listed, reinforced, corrected, or decayed as active facts.
        """
        effective: list[dict[str, Any]] = []
        for record in self.current_records(scope, include_inactive=True):
            projected = dict(record)
            if record.get("status") in {"active", "archived"}:
                evidence_refs = list(record.get("evidence_refs", []))
                if not evidence_refs or not all(self.validate_evidence(scope, ref) for ref in evidence_refs):
                    projected["ledger_status"] = record.get("status")
                    projected["status"] = "deleted"
                    projected["invalidation_reason"] = "invalid_evidence"
            effective.append(projected)
        if include_inactive:
            return effective
        return [record for record in effective if record.get("status") == "active"]

    def append_access(
        self,
        scope: MemoryScope,
        record_id: str,
        access_strength: float,
        operation: str,
        reason: str,
    ) -> dict[str, Any]:
        event = {
            "event_id": self.stable_id(
                "access",
                record_id,
                operation,
                reason,
                self.now_iso(),
            ),
            "record_id": record_id,
            "access_strength": max(0.0, finite_number(access_strength, "access_strength")),
            "operation": operation,
            "reason": reason,
            "created_at": self.now_iso(),
            "scope": scope.as_record(),
        }
        self.append(scope, "access", event)
        return event

    def observations(self, scope: MemoryScope) -> dict[str, dict[str, Any]]:
        return self._indexed_ledger(scope, "observations", "observation_id")

    def assets(self, scope: MemoryScope) -> dict[str, dict[str, Any]]:
        return self._indexed_ledger(scope, "assets", "asset_id")

    def episodes(self, scope: MemoryScope) -> dict[str, dict[str, Any]]:
        deleted = self.deleted_refs(scope)
        return {
            item["episode_id"]: item
            for item in self.read_ledger(scope, "episodes")
            if f"episode:{item['episode_id']}" not in deleted and item["episode_id"] not in deleted
        }

    def validate_evidence(self, scope: MemoryScope, evidence_ref: str) -> bool:
        if not isinstance(evidence_ref, str) or not evidence_ref:
            return False
        deleted = self.deleted_refs(scope)
        if evidence_ref in deleted:
            return False

        prefix, separator, identifier = evidence_ref.partition(":")
        if separator and prefix in {"asset", "observation", "episode"}:
            if prefix == "asset":
                return self._valid_asset_ref(scope, identifier, deleted)
            if prefix == "observation":
                return self._valid_observation_ref(scope, identifier, deleted)
            return self._valid_episode_ref(scope, identifier, deleted)
        if evidence_ref.startswith("asset_"):
            return self._valid_asset_ref(scope, evidence_ref, deleted)
        if evidence_ref.startswith("obs_"):
            return self._valid_observation_ref(scope, evidence_ref, deleted)
        if evidence_ref.startswith("episode_"):
            return self._valid_episode_ref(scope, evidence_ref, deleted)

        path_ref, anchor_separator, evidence_id = evidence_ref.partition("#")
        path = self._safe_scope_path(scope, path_ref)
        if path is None or not path.is_file() or not path_ref.startswith("evidence/"):
            return False
        records = self._read_legacy_evidence(path)
        if anchor_separator:
            return any(
                item.get("id") == evidence_id and self._matches_scope(item, scope)
                for item in records
            )
        return False

    def valid_observation_ids(self, scope: MemoryScope) -> set[str]:
        deleted = self.deleted_refs(scope)
        assets = self.assets(scope)
        valid_assets = {
            asset_id
            for asset_id, asset in assets.items()
            if self._valid_asset_record(scope, asset, deleted)
        }
        return {
            observation_id
            for observation_id, observation in self.observations(scope).items()
            if observation_id not in deleted
            and f"observation:{observation_id}" not in deleted
            and observation.get("asset_id") in valid_assets
        }

    def anchor_legacy_ref(
        self,
        scope: MemoryScope,
        evidence_ref: str,
        source: str | None = None,
    ) -> str:
        if evidence_ref.startswith(("asset:", "observation:", "episode:")) or "#" in evidence_ref:
            return evidence_ref
        path = self._safe_scope_path(scope, evidence_ref)
        if path is None or not path.is_file() or not evidence_ref.startswith("evidence/"):
            return evidence_ref
        records = [item for item in self._read_legacy_evidence(path) if self._matches_scope(item, scope)]
        if source is not None:
            source_matches = [item for item in records if item.get("source") == source]
            if source_matches:
                records = source_matches
        if len(records) != 1:
            raise EvidenceValidationError(
                "Path-only evidence is ambiguous; use EvidenceResult.evidence_anchor"
            )
        evidence_id = records[0].get("id")
        if not evidence_id:
            raise EvidenceValidationError("Legacy evidence record has no event ID")
        return f"{evidence_ref}#{evidence_id}"

    def assert_evidence(self, scope: MemoryScope, evidence_refs: Iterable[str]) -> None:
        refs = list(evidence_refs)
        invalid = [ref for ref in refs if not self.validate_evidence(scope, ref)]
        if not refs or invalid:
            detail = ", ".join(invalid) if invalid else "no evidence refs"
            raise EvidenceValidationError(f"Memory candidate has invalid evidence: {detail}")

    def deleted_refs(self, scope: MemoryScope) -> set[str]:
        return {item["target_ref"] for item in self.read_ledger(scope, "deletions")}

    def append_unique(
        self,
        scope: MemoryScope,
        ledger: str,
        record: dict[str, Any],
        key: str,
    ) -> bool:
        path = self.ledger_path(scope, ledger)
        self.ensure_scope(scope)
        with self._ledger_lock(path):
            stat = path.stat()
            cache_key = (str(path), key)
            cached = self._unique_key_cache.get(cache_key)
            if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                values = set(cached[2])
            else:
                values = {item.get(key) for item in self._read_jsonl_unlocked(path)}
            if record.get(key) in values:
                return False
            self._append_jsonl_unlocked(path, record)
            values.add(record.get(key))
            updated = path.stat()
            self._unique_key_cache[cache_key] = (updated.st_mtime_ns, updated.st_size, values)
            self._invalidate_ledger_cache(path)
        return True

    def append(self, scope: MemoryScope, ledger: str, record: dict[str, Any]) -> None:
        path = self.ledger_path(scope, ledger)
        self.ensure_scope(scope)
        with self._ledger_lock(path):
            self._append_jsonl_unlocked(path, record)
            self._invalidate_ledger_cache(path)

    def now_iso(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    @staticmethod
    def stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
        return f"{prefix}_{digest}"

    @staticmethod
    def canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _valid_asset_ref(self, scope: MemoryScope, asset_id: str, deleted: set[str]) -> bool:
        if asset_id in deleted or f"asset:{asset_id}" in deleted:
            return False
        asset = self.assets(scope).get(asset_id)
        if asset is None:
            return False
        return self._valid_asset_record(scope, asset, deleted)

    def _valid_asset_record(
        self,
        scope: MemoryScope,
        asset: dict[str, Any],
        deleted: set[str],
    ) -> bool:
        asset_id = str(asset.get("asset_id", ""))
        if asset_id in deleted or f"asset:{asset_id}" in deleted:
            return False
        path = self._safe_scope_path(scope, asset.get("storage_path", ""))
        if path is None or not path.is_file():
            return False
        stat = path.stat()
        cache_key = (str(path), stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size)
        cached = self._asset_validation_cache.get(cache_key)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        valid = digest.hexdigest() == asset.get("sha256") and stat.st_size == int(asset.get("byte_size", -1))
        self._asset_validation_cache[cache_key] = valid
        return valid

    def _valid_observation_ref(self, scope: MemoryScope, observation_id: str, deleted: set[str]) -> bool:
        if observation_id in deleted or f"observation:{observation_id}" in deleted:
            return False
        observation = self.observations(scope).get(observation_id)
        return observation is not None and self._valid_asset_ref(scope, observation["asset_id"], deleted)

    def _valid_episode_ref(self, scope: MemoryScope, episode_id: str, deleted: set[str]) -> bool:
        if episode_id in deleted or f"episode:{episode_id}" in deleted:
            return False
        episode = self._indexed_ledger(scope, "episodes", "episode_id").get(episode_id)
        if episode is None or not episode.get("observation_ids"):
            return False
        observations = self.observations(scope)
        assets = self.assets(scope)
        for observation_id in episode["observation_ids"]:
            if observation_id in deleted or f"observation:{observation_id}" in deleted:
                return False
            observation = observations.get(observation_id)
            if observation is None:
                return False
            asset = assets.get(observation.get("asset_id"))
            if asset is None or not self._valid_asset_record(scope, asset, deleted):
                return False
        return True

    def _indexed_ledger(
        self,
        scope: MemoryScope,
        ledger: str,
        key: str,
    ) -> dict[str, dict[str, Any]]:
        path = self.ledger_path(scope, ledger)
        if not path.exists():
            return {}
        stat = path.stat()
        cache_key = (str(path), key)
        cached = self._ledger_index_cache.get(cache_key)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return {identifier: dict(item) for identifier, item in cached[2].items()}
        indexed = {
            str(item[key]): item
            for item in self.read_ledger(scope, ledger)
            if key in item
        }
        self._ledger_index_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, indexed)
        return {identifier: dict(item) for identifier, item in indexed.items()}

    def _invalidate_ledger_cache(self, path: Path) -> None:
        path_value = str(path)
        self._ledger_cache.pop(path_value, None)
        for cache_key in [item for item in self._ledger_index_cache if item[0] == path_value]:
            self._ledger_index_cache.pop(cache_key, None)

    def _sync_generation(self, generation_id: str | None) -> None:
        if generation_id == self._schema_generation:
            return
        self._asset_validation_cache.clear()
        self._ledger_cache.clear()
        self._ledger_index_cache.clear()
        self._unique_key_cache.clear()
        self._ensured_scopes.clear()
        self._schema_generation = generation_id

    def _safe_scope_path(self, scope: MemoryScope, relative: str) -> Path | None:
        if not relative or Path(relative).is_absolute():
            return None
        root = self.scope_root(scope).resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _matches_scope(item: dict[str, Any], scope: MemoryScope) -> bool:
        return all(
            item.get(key) == value
            for key, value in scope.as_record().items()
            if key in {"tenant_id", "user_id", "namespace"}
        )

    @staticmethod
    def _read_legacy_evidence(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                return []
            if isinstance(value, dict):
                records.append(value)
        return records

    @staticmethod
    def _asset_suffix(mime_type: str, original_name: str | None) -> str:
        if original_name:
            suffix = Path(original_name).suffix.lower()
            if suffix and len(suffix) <= 12:
                return suffix
        preferred = {
            "text/plain": ".txt",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "audio/wav": ".wav",
            "audio/mpeg": ".mp3",
            "video/mp4": ".mp4",
        }
        return preferred.get(mime_type) or mimetypes.guess_extension(mime_type) or ".bin"

    @contextmanager
    def _ledger_lock(self, path: Path) -> Iterator[None]:
        lock_path = path.with_suffix(path.suffix + ".lock")
        _ensure_private_directory(lock_path.parent)
        with _open_regular_lock(lock_path) as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_jsonl_unlocked(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").split("\n"):
            if line.strip():
                result.append(json.loads(line))
        return result

    @staticmethod
    def _append_jsonl_unlocked(path: Path, record: dict[str, Any]) -> None:
        _ensure_private_directory(path.parent)
        line = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            if path.is_symlink():
                raise StorageError(f"ledger path must not be a symlink: {path}") from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StorageError(f"ledger path must be a regular file: {path}")
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            remaining = memoryview(line.encode("utf-8"))
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("JSONL append made no progress")
                remaining = remaining[written:]
        finally:
            os.close(descriptor)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        _ensure_private_directory(path.parent)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp.write(content)
            temp_path = Path(tmp.name)
        _chmod_private(temp_path, 0o600)
        temp_path.replace(path)

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        _ensure_private_directory(path.parent)
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as tmp:
            tmp.write(content)
            temp_path = Path(tmp.name)
        _chmod_private(temp_path, 0o600)
        temp_path.replace(path)
