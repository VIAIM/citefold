import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from citefold import MemoryScope, Citefold


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def scope() -> MemoryScope:
    return MemoryScope("tenant-a", "user-1", "personal", "retention-agent", "session-1")


def scope_root(tmp: str) -> Path:
    return Path(tmp) / "tenants" / "tenant-a" / "users" / "user-1" / "namespaces" / "personal"


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class MemoryRetentionTest(unittest.TestCase):
    def test_forget_observation_tombstones_evidence_and_cascades_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp)
            ingested = memory.ingest_text(scope(), "请记住：我喜欢先看风险。", source="text_chat")
            evidence_ref = f"observation:{ingested.observation_ids[0]}"

            outcome = memory.forget(scope(), evidence_ref, reason="用户要求删除")

            self.assertFalse(memory.validate_evidence(scope(), evidence_ref))
            self.assertEqual(1, outcome["invalidated_records"])
            self.assertEqual([], memory.list_records(scope()))
            all_records = memory.list_records(scope(), include_inactive=True)
            self.assertEqual("deleted", all_records[0]["status"])
            self.assertNotIn("先看风险", memory.recall(scope(), "我有什么偏好？").markdown)
            self.assertEqual("none", memory.recall(scope(), "我有什么偏好？").coverage)
            self.assertEqual(evidence_ref, jsonl(scope_root(tmp) / "ledgers" / "deletions.jsonl")[0]["target_ref"])

    def test_archive_leaves_history_but_requires_explicit_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp)
            ingested = memory.ingest_text(scope(), "请记住：我喜欢下午开会。", source="text_chat")
            record_id = ingested.record_ids[0]

            memory.archive(scope(), record_id, reason="旧偏好")
            normal = memory.recall(scope(), "我喜欢什么时候开会？有什么偏好？")
            archived = memory.recall(
                scope(),
                "我喜欢什么时候开会？有什么偏好？",
                include_archived=True,
            )

            self.assertNotIn("下午开会", normal.markdown)
            self.assertIn("下午开会", archived.markdown)
            self.assertEqual("archived", memory.list_records(scope(), include_inactive=True)[0]["status"])

    def test_decay_changes_access_strength_not_confidence_and_exempts_open_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = MutableClock()
            memory = Citefold(tmp, clock=clock)
            preference = memory.ingest_text(scope(), "请记住：我喜欢简短回答。", source="text_chat")
            task = memory.ingest_text(scope(), "提醒我周五提交报价。", source="text_chat")
            before = {item["record_id"]: item for item in memory.list_records(scope())}
            clock.value = clock.value + timedelta(days=180)

            changed = memory.decay(scope())
            after = {item["record_id"]: item for item in memory.list_records(scope())}

            preference_id = preference.record_ids[0]
            task_id = task.record_ids[0]
            self.assertIn(preference_id, changed)
            self.assertLess(after[preference_id]["access_strength"], before[preference_id]["access_strength"])
            self.assertEqual(before[preference_id]["confidence"], after[preference_id]["confidence"])
            self.assertEqual(1.0, after[task_id]["access_strength"])

    def test_decay_is_idempotent_and_only_applies_new_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = MutableClock()
            memory = Citefold(tmp, clock=clock)
            preference = memory.ingest_text(scope(), "请记住：我喜欢简短回答。", source="text_chat")
            record_id = preference.record_ids[0]
            confidence = memory.list_records(scope())[0]["confidence"]
            clock.value = clock.value + timedelta(days=180)

            first_changed = memory.decay(scope())
            first = memory.list_records(scope())[0]
            repeated_changed = memory.decay(scope())
            repeated = memory.list_records(scope())[0]
            clock.value = clock.value + timedelta(days=90)
            next_changed = memory.decay(scope())
            after_next_period = memory.list_records(scope())[0]

            self.assertIn(record_id, first_changed)
            self.assertEqual([], repeated_changed)
            self.assertEqual(first["access_strength"], repeated["access_strength"])
            self.assertIn(record_id, next_changed)
            self.assertAlmostEqual(
                first["access_strength"] * 0.5,
                after_next_period["access_strength"],
            )
            self.assertEqual(confidence, after_next_period["confidence"])

    def test_pin_prevents_decay_and_unpin_restores_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = MutableClock()
            memory = Citefold(tmp, clock=clock)
            ingested = memory.ingest_text(scope(), "请记住：我喜欢简短回答。", source="text_chat")
            record_id = ingested.record_ids[0]

            pinned = memory.pin(scope(), record_id, reason="用户要求保留")
            clock.value = clock.value + timedelta(days=180)

            self.assertTrue(pinned.pinned)
            self.assertEqual([], memory.decay(scope()))
            self.assertEqual(1.0, memory.list_records(scope())[0]["access_strength"])

            unpinned = memory.unpin(scope(), record_id, reason="用户恢复正常衰减")

            self.assertFalse(unpinned.pinned)
            self.assertEqual([], memory.decay(scope()))
            self.assertEqual(1.0, memory.list_records(scope())[0]["access_strength"])

            clock.value = clock.value + timedelta(days=90)
            changed = memory.decay(scope())

            self.assertEqual([record_id], changed)
            self.assertAlmostEqual(0.5, memory.list_records(scope())[0]["access_strength"])

    def test_pin_transitions_are_idempotent_auditable_and_inherited_by_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp)
            ingested = memory.ingest_text(scope(), "请记住：我喜欢先看结论。", source="text_chat")
            record_id = ingested.record_ids[0]

            memory.pin(scope(), record_id, reason="  explicit pin  ")
            memory.pin(scope(), record_id)
            memory.unpin(scope(), record_id)
            memory.unpin(scope(), record_id)
            repinned = memory.pin(scope(), record_id)

            revisions = jsonl(scope_root(tmp) / "ledgers" / "revisions.jsonl")
            self.assertEqual(["ADD", "PIN", "UNPIN", "PIN"], [item["operation"] for item in revisions])
            self.assertEqual("explicit pin", revisions[1]["reason"])
            self.assertFalse(revisions[1]["previous_record"]["pinned"])
            self.assertTrue(revisions[1]["record"]["pinned"])
            self.assertTrue(repinned.pinned)

            audit = [
                item
                for item in jsonl(scope_root(tmp) / "audit" / "memory_events.jsonl")
                if item["action"] in {"pin", "unpin"}
            ]
            self.assertEqual([True, False, True, False, True], [item["data"]["changed"] for item in audit])
            self.assertEqual("explicit pin", audit[0]["data"]["reason"])

            corrected = memory.correct(scope(), record_id, "我改为喜欢先看风险。")
            self.assertTrue(corrected.pinned)
            with self.assertRaises(KeyError):
                memory.pin(scope(), record_id)

            memory.archive(scope(), corrected.record_id)
            with self.assertRaises(KeyError):
                memory.unpin(scope(), corrected.record_id)

            other_scope = MemoryScope(
                "tenant-b", "user-1", "personal", "retention-agent", "session-1"
            )
            with self.assertRaises(KeyError):
                memory.pin(other_scope, corrected.record_id)

    def test_pin_does_not_prevent_evidence_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp)
            ingested = memory.ingest_text(scope(), "请记住：我喜欢先看风险。", source="text_chat")
            memory.pin(scope(), ingested.record_ids[0])

            memory.forget(scope(), f"observation:{ingested.observation_ids[0]}")

            self.assertEqual([], memory.list_records(scope()))
            self.assertEqual("deleted", memory.list_records(scope(), include_inactive=True)[0]["status"])

    def test_hard_delete_asset_removes_bytes_and_invalidates_derived_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_root = Path(tmp) / "memory"
            memory = Citefold(memory_root)
            image = Path(tmp) / "private.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nprivate")
            ingested = memory.ingest_image(
                scope(),
                image,
                "upload",
                observations=[{"content": "机密白板", "confidence": 0.9, "locator": {}}],
            )
            asset = memory.store.assets(scope())[ingested.asset_ids[0]]
            stored_path = scope_root(memory_root) / asset["storage_path"]

            memory.forget(scope(), f"asset:{ingested.asset_ids[0]}", hard=True, reason="用户硬删除")

            self.assertFalse(stored_path.exists())
            self.assertFalse(memory.validate_evidence(scope(), f"observation:{ingested.observation_ids[0]}"))
            self.assertEqual("none", memory.recall(scope(), "机密白板").coverage)

    def test_hard_delete_observation_removes_its_asset_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp)
            ingested = memory.ingest_text(scope(), "private observation", source="text_chat")
            asset = memory.store.assets(scope())[ingested.asset_ids[0]]
            stored_path = scope_root(tmp) / asset["storage_path"]

            outcome = memory.forget(
                scope(),
                f"observation:{ingested.observation_ids[0]}",
                hard=True,
            )

            self.assertFalse(stored_path.exists())
            self.assertEqual([asset["storage_path"]], outcome["deleted_asset_paths"])

    def test_hard_delete_episode_removes_all_referenced_asset_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp)
            ingested = memory.ingest_chat(
                scope(),
                [
                    {"role": "user", "content": "first private turn"},
                    {"role": "assistant", "content": "second private turn"},
                ],
            )
            assets = memory.store.assets(scope())
            stored_paths = {
                asset["storage_path"]: scope_root(tmp) / asset["storage_path"]
                for asset in assets.values()
            }

            outcome = memory.forget(
                scope(),
                f"episode:{ingested.episode_ids[0]}",
                hard=True,
            )

            self.assertTrue(all(not path.exists() for path in stored_paths.values()))
            self.assertEqual(sorted(stored_paths), sorted(outcome["deleted_asset_paths"]))

    def test_archiving_person_memory_removes_stale_materialized_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp)
            evidence = memory.append_event(scope(), "crm_agent", {"text": "Alex prefers email"})
            candidate = memory.submit_candidate(
                scope(), "crm_agent", "people", "Alex 偏好邮件联系。", [evidence.evidence_ref], 0.9
            )
            memory.approve_candidate(scope(), candidate.candidate_id)
            record_id = memory.list_records(scope())[0]["record_id"]
            person_path = scope_root(tmp) / "people" / "alex.md"
            self.assertTrue(person_path.exists())

            memory.archive(scope(), record_id)

            self.assertFalse(person_path.exists())
            entities = json.loads((scope_root(tmp) / "indexes" / "entities.json").read_text())
            self.assertNotIn("Alex", entities["people"])

    def test_forget_episode_tombstones_observations_and_derived_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp)
            ingested = memory.ingest_text(scope(), "请记住：我喜欢先看风险。", source="text_chat")
            episode_ref = f"episode:{ingested.episode_ids[0]}"
            observation_ref = f"observation:{ingested.observation_ids[0]}"

            outcome = memory.forget(scope(), episode_ref, reason="用户删除整段会话")

            self.assertEqual(1, outcome["invalidated_records"])
            self.assertFalse(memory.validate_evidence(scope(), episode_ref))
            self.assertFalse(memory.validate_evidence(scope(), observation_ref))
            self.assertEqual([], memory.list_records(scope()))

    def test_decay_lowers_default_recall_order_and_spares_recent_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = MutableClock()
            memory = Citefold(tmp, clock=clock)
            old = memory.ingest_text(scope(), "请记住：我喜欢先看风险。", source="text_chat")
            clock.value = clock.value + timedelta(days=180)
            recent = memory.ingest_text(scope(), "请记住：我喜欢先看结论。", source="text_chat")

            changed = memory.decay(scope())
            pack = memory.recall(scope(), "我有什么偏好？", token_budget=512)

            self.assertIn(old.record_ids[0], changed)
            self.assertNotIn(recent.record_ids[0], changed)
            self.assertLess(pack.markdown.index("先看结论"), pack.markdown.index("先看风险"))


if __name__ == "__main__":
    unittest.main()
