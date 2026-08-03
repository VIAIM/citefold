import hashlib
import json
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from unittest import mock

import citefold.storage as storage_module

from citefold import (
    BackupValidationError,
    Citefold,
    MemoryScope,
    MigrationPreflightError,
    MigrationRecoveryRequired,
    MigrationRequiredError,
    UnrecognizedStoreError,
    UnsupportedSchemaVersionError,
    backup_store,
    inspect_store,
    migrate_store,
    plan_migration,
    restore_store,
    verify_backup,
)


FIXTURE = Path(__file__).parent / "fixtures" / "v0_1_store"


def copy_v0_1_store(parent: Path) -> tuple[Path, dict]:
    root = parent / "memory"
    shutil.copytree(FIXTURE / "store", root)
    expected = json.loads((FIXTURE / "FIXTURE.json").read_text(encoding="utf-8"))
    return root, expected


def file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def scope_from(value: dict) -> MemoryScope:
    return MemoryScope(
        value["tenant_id"],
        value["user_id"],
        value["namespace"],
        value["agent_id"],
        value["session_id"],
    )


def backup_fingerprint(entries: list[dict]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item["path"]):
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_backup_archive(
    archive_path: Path,
    entries: list[dict],
    members: dict[str, bytes],
    *,
    schema_version: int = 2,
) -> None:
    manifest = {
        "format": "citefold.backup",
        "backup_format_version": 1,
        "source_schema_version": schema_version,
        "source_store_id": "test-store",
        "created_at": "2026-08-03T00:00:00+00:00",
        "fingerprint": backup_fingerprint(entries),
        "files": entries,
    }
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("CITEFOLD_BACKUP_MANIFEST.json", json.dumps(manifest))
        for name, content in members.items():
            archive.writestr(name, content)


class StorageMigrationTest(unittest.TestCase):
    def test_inspect_and_dry_run_recognize_real_v0_1_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _expected = copy_v0_1_store(Path(tmp))
            before = file_hashes(root)

            status = inspect_store(root)
            plan = plan_migration(root)

            self.assertEqual("legacy", status.state)
            self.assertEqual(1, status.schema_version)
            self.assertTrue(status.migration_required)
            self.assertTrue(plan.ready)
            self.assertEqual(1, plan.source_version)
            self.assertEqual(2, plan.target_version)
            self.assertGreater(plan.file_count, 20)
            self.assertEqual(before, file_hashes(root))
            self.assertFalse((root / "citefold-store.json").exists())
            self.assertFalse(plan.backup_path.exists())

    def test_legacy_future_and_unknown_stores_fail_closed_for_normal_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            legacy, expected = copy_v0_1_store(parent)
            with self.assertRaisesRegex(MigrationRequiredError, "migrate"):
                Citefold(legacy).list_records(scope_from(expected["scope"]))

            future = parent / "future"
            future.mkdir()
            (future / "citefold-store.json").write_text(
                json.dumps({"format": "citefold.local-store", "schema_version": 999}),
                encoding="utf-8",
            )
            with self.assertRaises(UnsupportedSchemaVersionError):
                Citefold(future).list_records(scope_from(expected["scope"]))

            unknown = parent / "unknown"
            unknown.mkdir()
            (unknown / "unrelated.txt").write_text("not a Citefold store", encoding="utf-8")
            with self.assertRaises(UnrecognizedStoreError):
                Citefold(unknown).list_records(scope_from(expected["scope"]))

    def test_current_store_with_missing_canonical_ledger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            memory = Citefold(root)
            result = memory.ingest_text(scope, "请记住：我喜欢红色。", source="test")
            evidence_ref = f"observation:{result.observation_ids[0]}"
            memory.forget(scope, evidence_ref, hard=False)
            deletions = memory.store.ledger_path(scope, "deletions")
            deletions.unlink()

            status = inspect_store(root)

            self.assertEqual("invalid", status.state)
            self.assertIn("incomplete", status.issue or "")
            with self.assertRaises(UnrecognizedStoreError):
                Citefold(root).validate_evidence(scope, evidence_ref)
            self.assertFalse(deletions.exists())

    def test_current_store_with_an_empty_existing_scope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            memory = Citefold(root)
            memory.ingest_text(scope, "initialize the store", source="test")
            existing_scope = memory.store.scope_root(scope)
            shutil.rmtree(existing_scope)
            (existing_scope / "ledgers").mkdir(parents=True)

            status = inspect_store(root)

            self.assertEqual("invalid", status.state)
            self.assertIn("incomplete", status.issue or "")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink contract")
    def test_current_store_with_symlinked_canonical_ledger_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            memory = Citefold(root)
            memory.ingest_text(scope, "initialize the store", source="test")
            observations = memory.store.ledger_path(scope, "observations")
            outside = parent / "outside.jsonl"
            outside.write_text(observations.read_text(encoding="utf-8"), encoding="utf-8")
            observations.unlink()
            observations.symlink_to(outside)

            status = inspect_store(root)

            self.assertEqual("invalid", status.state)
            self.assertIn("incomplete", status.issue or "")

    def test_migration_preserves_every_v0_1_file_and_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, expected = copy_v0_1_store(Path(tmp))
            before = file_hashes(root)

            migrated = migrate_store(root)

            self.assertEqual("migrated", migrated.status)
            self.assertEqual(1, migrated.source_version)
            self.assertEqual(2, migrated.target_version)
            self.assertTrue(migrated.backup_path.is_file())
            self.assertTrue(verify_backup(migrated.backup_path).verified)
            self.assertEqual(before, {key: file_hashes(root)[key] for key in before})
            self.assertEqual("current", inspect_store(root).state)
            self.assertFalse((root / "migration-state.json").exists())

            memory = Citefold(root)
            scope = scope_from(expected["scope"])
            voice_scope = scope_from(expected["voice_scope"])
            records = {item["record_id"] for item in memory.list_records(scope)}
            self.assertIn(expected["preference_record_id"], records)
            self.assertTrue(memory.validate_evidence(scope, expected["legacy_evidence_anchor"]))
            self.assertFalse(
                memory.validate_evidence(
                    scope,
                    f"observation:{expected['deleted_observation_id']}",
                )
            )
            self.assertTrue(
                memory.validate_evidence(
                    voice_scope,
                    f"observation:{expected['partial_observation_id']}",
                )
            )
            self.assertTrue(
                memory.validate_evidence(
                    voice_scope,
                    f"observation:{expected['final_observation_id']}",
                )
            )
            self.assertIn("先看风险", memory.recall(scope, "我有什么偏好？").markdown)
            self.assertIn("Alex 偏好周五下午跟进", memory.recall(scope, "Alex 怎么跟进？").markdown)
            voice_pack = memory.recall(voice_scope, "客户方案有什么安排？", mode="voice")
            self.assertIn("周五上午9点检查客户方案", voice_pack.markdown)
            self.assertNotIn("披萨项目", voice_pack.markdown)

            repeated = migrate_store(root)
            self.assertEqual("up_to_date", repeated.status)
            self.assertIsNone(repeated.backup_path)

    def test_new_store_gets_current_manifest_on_first_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")

            memory = Citefold(root)
            self.assertEqual("uninitialized", inspect_store(root).state)
            memory.ingest_text(scope, "请记住：我喜欢简短回答。", source="test")

            status = inspect_store(root)
            self.assertEqual("current", status.state)
            self.assertEqual(2, status.schema_version)
            self.assertTrue(status.generation_id)

    def test_backup_restore_replaces_store_and_invalidates_live_instance_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            memory = Citefold(root)
            first = memory.ingest_text(scope, "请记住：我喜欢先看风险。", source="test")
            backup = backup_store(root)
            second = memory.ingest_text(scope, "请记住：我喜欢先看结论。", source="test")
            self.assertEqual(2, len(memory.list_records(scope)))

            restored = restore_store(root, backup.archive, replace=True)

            self.assertEqual("restored", restored.status)
            self.assertIsNotNone(restored.displaced_root)
            self.assertTrue(restored.displaced_root.is_dir())
            record_ids = {item["record_id"] for item in memory.list_records(scope)}
            self.assertIn(first.record_ids[0], record_ids)
            self.assertNotIn(second.record_ids[0], record_ids)

    def test_scope_initialization_retries_if_restore_removes_it_before_shared_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            memory = Citefold(root)
            baseline_scope = MemoryScope("tenant", "user", "baseline", "agent", "session")
            target_scope = MemoryScope("tenant", "user", "target", "agent", "session")
            memory.ingest_text(baseline_scope, "baseline memory", source="test")
            backup = backup_store(root)
            real_initialize = memory.store._initialize_scope_if_missing
            restored = False

            def initialize_then_restore(candidate: MemoryScope) -> None:
                nonlocal restored
                real_initialize(candidate)
                if candidate == target_scope and not restored:
                    restored = True
                    restore_store(root, backup.archive, replace=True)

            with mock.patch.object(
                memory.store,
                "_initialize_scope_if_missing",
                side_effect=initialize_then_restore,
            ):
                result = memory.ingest_text(
                    target_scope,
                    "请记住：我喜欢恢复后先看风险。",
                    source="test",
                )

            self.assertTrue(restored)
            self.assertIn(result.record_ids[0], {item["record_id"] for item in memory.list_records(target_scope)})
            self.assertEqual(10, len(list((memory.store.scope_root(target_scope) / "ledgers").glob("*.jsonl"))))

    @unittest.skipUnless(os.name == "posix", "POSIX advisory locking is required")
    def test_backup_waits_for_normal_operation_in_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            backup_path = parent / "memory.zip"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            Citefold(root).ingest_text(scope, "initialize the current store", source="test")

            child_script = """
import sys
from pathlib import Path

from citefold import Citefold, MemoryScope

root = Path(sys.argv[1])
scope = MemoryScope("tenant", "user", "personal", "agent", "session")
with Citefold(root).store.scope_writer(scope):
    print("ready", flush=True)
    if sys.stdin.readline() != "release\\n":
        raise RuntimeError("parent did not release the shared root guard")
"""
            child = subprocess.Popen(
                [sys.executable, "-u", "-c", child_script, str(root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            released = False
            try:
                assert child.stdout is not None
                readable, _, _ = select.select([child.stdout], [], [], 5)
                self.assertTrue(readable, "child did not acquire the shared root guard")
                self.assertEqual("ready", child.stdout.readline().strip())

                assert storage_module.fcntl is not None
                real_flock = storage_module.fcntl.flock
                exclusive_attempted = threading.Event()

                def observed_flock(descriptor, operation):
                    if operation == storage_module.fcntl.LOCK_EX:
                        exclusive_attempted.set()
                    return real_flock(descriptor, operation)

                with mock.patch.object(storage_module.fcntl, "flock", side_effect=observed_flock):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        backup_future = pool.submit(backup_store, root, backup_path)
                        try:
                            self.assertTrue(
                                exclusive_attempted.wait(5),
                                "backup did not attempt the exclusive root guard",
                            )
                            with self.assertRaises(FutureTimeoutError):
                                backup_future.result(timeout=0.25)
                            self.assertFalse(backup_future.done())
                            self.assertFalse(backup_path.exists())
                        finally:
                            assert child.stdin is not None
                            child.stdin.write("release\n")
                            child.stdin.flush()
                            released = True
                        backup = backup_future.result(timeout=5)

                assert child.stderr is not None
                self.assertEqual(0, child.wait(timeout=5), child.stderr.read())
                self.assertEqual(backup_path.resolve(), backup.archive)
                self.assertTrue(verify_backup(backup.archive).verified)
            finally:
                if child.poll() is None:
                    if not released and child.stdin is not None:
                        try:
                            child.stdin.write("release\n")
                            child.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.terminate()
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=2)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_restore_recovers_after_a_crash_between_directory_swaps(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            memory = Citefold(root)
            first = memory.ingest_text(scope, "请记住：我喜欢先看风险。", source="test")
            backup = backup_store(root)
            second = memory.ingest_text(scope, "请记住：我喜欢先看结论。", source="test")
            real_replace = Path.replace

            def crash_after_displacing(source: Path, target: Path) -> Path:
                result = real_replace(source, target)
                if source == root.resolve() and ".memory.displaced-" in Path(target).name:
                    raise SimulatedCrash("injected crash after displacing the root")
                return result

            with mock.patch.object(Path, "replace", new=crash_after_displacing):
                with self.assertRaisesRegex(SimulatedCrash, "after displacing"):
                    restore_store(root, backup.archive, replace=True)

            self.assertEqual("recovery_required", inspect_store(root).state)
            with self.assertRaises(MigrationRecoveryRequired):
                memory.list_records(scope)

            backup.archive.unlink()
            restored = restore_store(root, backup.archive, replace=True)
            record_ids = {item["record_id"] for item in memory.list_records(scope)}
            self.assertEqual("restored", restored.status)
            self.assertIn(first.record_ids[0], record_ids)
            self.assertNotIn(second.record_ids[0], record_ids)
            self.assertEqual("current", inspect_store(root).state)

    def test_migration_never_rolls_back_a_concurrent_legacy_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = copy_v0_1_store(Path(tmp))
            access = next(root.rglob("ledgers/access.jsonl"))
            marker = {
                "scope": {
                    "tenant_id": "tenant-a",
                    "user_id": "user-1",
                    "namespace": "personal",
                },
                "legacy_concurrent_marker": True,
            }
            real_backup = storage_module._backup_store_unlocked

            def backup_then_append(*args, **kwargs):
                result = real_backup(*args, **kwargs)
                with access.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(marker, sort_keys=True) + "\n")
                return result

            with mock.patch.object(
                storage_module,
                "_backup_store_unlocked",
                side_effect=backup_then_append,
            ):
                with self.assertRaisesRegex(MigrationPreflightError, "changed|fingerprint"):
                    migrate_store(root)

            self.assertIn(marker, [json.loads(line) for line in access.read_text().splitlines() if line])
            self.assertEqual("legacy", inspect_store(root).state)
            self.assertFalse((root / "citefold-store.json").exists())
            self.assertFalse((root / "migration-state.json").exists())

    def test_interrupted_additive_migration_recovers_without_replacing_legacy_data(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, _ = copy_v0_1_store(Path(tmp))
            before = file_hashes(root)
            real_atomic_write = storage_module._atomic_write

            def crash_before_manifest(path: Path, content: str) -> None:
                if path.name == "citefold-store.json" and (root / "migration-state.json").exists():
                    raise SimulatedCrash("injected crash before manifest commit")
                real_atomic_write(path, content)

            with mock.patch.object(storage_module, "_atomic_write", side_effect=crash_before_manifest):
                with self.assertRaisesRegex(SimulatedCrash, "before manifest"):
                    migrate_store(root)

            self.assertEqual("recovery_required", inspect_store(root).state)
            self.assertEqual(before, {key: file_hashes(root)[key] for key in before})

            migrated = migrate_store(root)

            self.assertEqual("migrated", migrated.status)
            self.assertEqual("current", inspect_store(root).state)
            self.assertEqual(before, {key: file_hashes(root)[key] for key in before})

    def test_manifest_commit_is_completed_after_interrupted_state_cleanup(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, _ = copy_v0_1_store(Path(tmp))
            before = file_hashes(root)
            real_unlink = storage_module._durable_unlink

            def crash_before_state_unlink(path: Path) -> None:
                if path.name == "migration-state.json":
                    raise SimulatedCrash("injected crash after manifest commit")
                real_unlink(path)

            with mock.patch.object(storage_module, "_durable_unlink", side_effect=crash_before_state_unlink):
                with self.assertRaisesRegex(SimulatedCrash, "after manifest"):
                    migrate_store(root)

            self.assertEqual("recovery_required", inspect_store(root).state)
            self.assertTrue((root / "citefold-store.json").is_file())

            recovered = migrate_store(root)

            self.assertEqual("up_to_date", recovered.status)
            self.assertEqual("current", inspect_store(root).state)
            self.assertEqual(before, {key: file_hashes(root)[key] for key in before})

    def test_preflight_blocks_corruption_scope_mismatch_and_backup_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)

            corrupt, _ = copy_v0_1_store(parent / "corrupt-case")
            observations = next(corrupt.rglob("ledgers/observations.jsonl"))
            observations.write_text(observations.read_text(encoding="utf-8") + "{bad\n", encoding="utf-8")
            self.assertFalse(plan_migration(corrupt).ready)
            with self.assertRaises(MigrationPreflightError):
                migrate_store(corrupt)
            self.assertFalse((corrupt / "citefold-store.json").exists())

            mismatch, _ = copy_v0_1_store(parent / "scope-case")
            assets = next(mismatch.rglob("ledgers/assets.jsonl"))
            lines = assets.read_text(encoding="utf-8").splitlines()
            value = json.loads(lines[0])
            value["scope"]["tenant_id"] = "other-tenant"
            lines[0] = json.dumps(value, ensure_ascii=False)
            assets.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertFalse(plan_migration(mismatch).ready)

            inside, _ = copy_v0_1_store(parent / "inside-case")
            plan = plan_migration(inside, backup_path=inside / "backup.zip")
            self.assertFalse(plan.ready)
            self.assertTrue(any("outside" in blocker for blocker in plan.blockers))

    def test_preflight_requires_complete_scope_metadata_and_scoped_legacy_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)

            missing_ledger_scope, _ = copy_v0_1_store(parent / "ledger-scope-case")
            assets = next(missing_ledger_scope.rglob("ledgers/assets.jsonl"))
            asset_lines = assets.read_text(encoding="utf-8").splitlines()
            asset = json.loads(asset_lines[0])
            asset["scope"].pop("tenant_id")
            asset_lines[0] = json.dumps(asset, ensure_ascii=False)
            assets.write_text("\n".join(asset_lines) + "\n", encoding="utf-8")

            ledger_plan = plan_migration(missing_ledger_scope)

            self.assertFalse(ledger_plan.ready)
            self.assertTrue(any("missing scope" in blocker for blocker in ledger_plan.blockers))

            missing_anchor_scope, expected = copy_v0_1_store(parent / "anchor-scope-case")
            anchor_id = expected["legacy_evidence_anchor"].partition("#")[2]
            evidence = next(missing_anchor_scope.rglob("evidence/2026-07/2026-07-01.jsonl"))
            evidence_lines = evidence.read_text(encoding="utf-8").splitlines()
            evidence_records = [json.loads(line) for line in evidence_lines]
            anchored = next(item for item in evidence_records if item.get("id") == anchor_id)
            anchored.pop("tenant_id")
            evidence.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in evidence_records) + "\n",
                encoding="utf-8",
            )

            anchor_plan = plan_migration(missing_anchor_scope)

            self.assertFalse(anchor_plan.ready)
            self.assertTrue(any("missing scope" in blocker for blocker in anchor_plan.blockers))

    def test_tampered_and_traversal_backups_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            Citefold(root).ingest_text(scope, "请记住：我喜欢先看风险。", source="test")
            backup = backup_store(root)

            with zipfile.ZipFile(backup.archive, "a") as archive:
                archive.writestr("store/extra.txt", "tampered")
            with self.assertRaises(BackupValidationError):
                verify_backup(backup.archive)

            malicious = parent / "malicious.zip"
            with zipfile.ZipFile(malicious, "w") as archive:
                archive.writestr("CITEFOLD_BACKUP_MANIFEST.json", "{}")
                archive.writestr("../escape.txt", "escape")
            with self.assertRaises(BackupValidationError):
                restore_store(parent / "restored", malicious)
            self.assertFalse((parent / "escape.txt").exists())

    def test_backup_round_trip_preserves_nonruntime_lock_and_nested_state_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            restored_root = parent / "restored"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            memory = Citefold(root)
            memory.ingest_text(scope, "请记住：这是一次备份测试。", source="test")
            scope_root = memory.store.scope_root(scope)
            secret = scope_root / "assets" / "secret.lock"
            nested_state = scope_root / "assets" / "migration-state.json"
            secret.write_bytes(b"real encrypted asset")
            nested_state.write_bytes(b"not a migration journal")

            backup = backup_store(root)
            with zipfile.ZipFile(backup.archive) as archive:
                names = set(archive.namelist())
            secret_relative = secret.relative_to(memory.store.root).as_posix()
            state_relative = nested_state.relative_to(memory.store.root).as_posix()
            self.assertIn(f"store/{secret_relative}", names)
            self.assertIn(f"store/{state_relative}", names)
            self.assertNotIn(
                f"store/{memory.store.scope_root(scope).relative_to(memory.store.root).as_posix()}"
                "/ledgers/.writer.lock",
                names,
            )
            self.assertNotIn(
                f"store/{memory.store.scope_root(scope).relative_to(memory.store.root).as_posix()}"
                "/ledgers/assets.jsonl.lock",
                names,
            )

            restore_store(restored_root, backup.archive)

            self.assertEqual(b"real encrypted asset", (restored_root / secret_relative).read_bytes())
            self.assertEqual(
                b"not a migration journal",
                (restored_root / state_relative).read_bytes(),
            )

    def test_backup_does_not_overwrite_a_destination_created_during_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            destination = parent / "backup.zip"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            Citefold(root).ingest_text(scope, "initialize the store", source="test")
            real_fsync = storage_module._fsync_file

            def create_competing_destination(path: Path) -> None:
                real_fsync(path)
                destination.write_bytes(b"created by another process")

            with mock.patch.object(
                storage_module,
                "_fsync_file",
                side_effect=create_competing_destination,
            ):
                with self.assertRaises(FileExistsError):
                    backup_store(root, destination)

            self.assertEqual(b"created by another process", destination.read_bytes())

    def test_restore_rejects_self_consistent_non_store_before_replacing_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            memory = Citefold(root)
            result = memory.ingest_text(scope, "请记住：我喜欢先看风险。", source="test")
            before = file_hashes(root)

            content = b"not a Citefold store"
            entry = {
                "path": "unrelated.txt",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            unrelated = parent / "unrelated.zip"
            write_backup_archive(unrelated, [entry], {"store/unrelated.txt": content})
            self.assertTrue(verify_backup(unrelated).verified)

            with self.assertRaisesRegex(BackupValidationError, "schema|store"):
                restore_store(root, unrelated, replace=True)

            self.assertEqual(before, file_hashes(root))
            self.assertIn(result.record_ids[0], {item["record_id"] for item in memory.list_records(scope)})
            self.assertEqual([], list(parent.glob(".memory.displaced-*")))

    def test_backup_rejects_duplicate_and_noncanonical_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            content = b"content"
            canonical = {
                "path": "folder/file.txt",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            duplicate = parent / "duplicate.zip"
            write_backup_archive(
                duplicate,
                [canonical, dict(canonical)],
                {"store/folder/file.txt": content},
            )
            with self.assertRaisesRegex(BackupValidationError, "duplicate"):
                verify_backup(duplicate)

            noncanonical_entry = dict(canonical, path="folder//file.txt")
            noncanonical = parent / "noncanonical.zip"
            write_backup_archive(
                noncanonical,
                [noncanonical_entry],
                {"store/folder/file.txt": content},
            )
            with self.assertRaisesRegex(BackupValidationError, "canonical"):
                verify_backup(noncanonical)

    def test_backup_verification_enforces_bounded_resource_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            memory = Citefold(root)
            memory.ingest_text(scope, "请记住：" + "A" * 100_000, source="test")
            large_media = memory.store.scope_root(scope) / "assets" / "sample.media"
            large_media.write_bytes(os.urandom(2 * 1024 * 1024))
            backup = backup_store(root)
            self.assertTrue(verify_backup(backup.archive).verified)
            with mock.patch.object(
                zipfile.ZipFile,
                "read",
                side_effect=AssertionError("file verification must stream"),
            ):
                self.assertTrue(verify_backup(backup.archive).verified)

            with mock.patch.object(storage_module, "MAX_BACKUP_MANIFEST_BYTES", 64):
                with self.assertRaisesRegex(BackupValidationError, "manifest.*large"):
                    verify_backup(backup.archive)
            with mock.patch.object(
                storage_module,
                "MAX_BACKUP_TOTAL_UNCOMPRESSED_BYTES",
                backup.total_bytes - 1,
            ):
                with self.assertRaisesRegex(BackupValidationError, "uncompressed"):
                    verify_backup(backup.archive)
            with mock.patch.object(storage_module, "MAX_BACKUP_COMPRESSION_RATIO", 1.0):
                with self.assertRaisesRegex(BackupValidationError, "compression ratio"):
                    verify_backup(backup.archive)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits are required")
    def test_backup_does_not_change_existing_parent_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "memory"
            scope = MemoryScope("tenant", "user", "personal", "agent", "session")
            Citefold(root).ingest_text(scope, "请记住：权限测试。", source="test")
            existing = parent / "existing-backups"
            existing.mkdir(mode=0o755)
            os.chmod(existing, 0o755)

            backup_store(root, existing / "memory.zip")

            self.assertEqual(0o755, stat.S_IMODE(existing.stat().st_mode))
            new_parent = parent / "private-backups"
            backup_store(root, new_parent / "memory.zip")
            self.assertEqual(0o700, stat.S_IMODE(new_parent.stat().st_mode))

    def test_nonnumeric_asset_byte_size_is_a_preflight_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = copy_v0_1_store(Path(tmp))
            assets = next(root.rglob("ledgers/assets.jsonl"))
            lines = assets.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["byte_size"] = "not-a-number"
            lines[0] = json.dumps(first, ensure_ascii=False)
            assets.write_text("\n".join(lines) + "\n", encoding="utf-8")

            plan = plan_migration(root)

            self.assertFalse(plan.ready)
            self.assertTrue(any("byte_size" in blocker for blocker in plan.blockers))

    def test_empty_ledgers_directory_is_not_recognized_as_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            (root / "tenants/t/users/u/namespaces/n/ledgers").mkdir(parents=True)

            status = inspect_store(root)
            plan = plan_migration(root)

            self.assertEqual("invalid", status.state)
            self.assertEqual(0, status.scope_count)
            self.assertFalse(plan.ready)


if __name__ == "__main__":
    unittest.main()
