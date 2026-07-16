import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from citefold import MemoryScope, Citefold
from citefold.openrouter import OpenRouterClient, OpenRouterRequestError


def fixed_clock() -> datetime:
    return datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def scope() -> MemoryScope:
    return MemoryScope("tenant-a", "user-1", "personal", "memory-agent", "session-1")


def root_for(tmp: str) -> Path:
    return Path(tmp) / "tenants" / "tenant-a" / "users" / "user-1" / "namespaces" / "personal"


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class MemoryLifecycleTest(unittest.TestCase):
    def test_openrouter_consolidation_failure_is_a_sanitized_pending_batch(self) -> None:
        transport_calls = 0

        def failing_transport(url, payload, headers, timeout):
            nonlocal transport_calls
            transport_calls += 1
            self.assertEqual(
                {"zdr": True, "data_collection": "deny", "require_parameters": True},
                payload["provider"],
            )
            if transport_calls > 1:
                return {
                    "id": "recovered-generation",
                    "model": "qwen/qwen3.7-plus",
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": '{"candidates":[]}'}}
                    ],
                    "usage": {},
                }
            raise OpenRouterRequestError(
                "no ZDR endpoint; prompt=prompt-sentinel; key=redacted-key-sentinel"
            )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "redacted-key-sentinel"}):
                client = OpenRouterClient(transport=failing_transport)
            memory = Citefold(tmp, clock=fixed_clock, openrouter=client)
            ingested = memory.ingest_text(
                scope(),
                "raw-observation-sentinel",
                source="meeting_note",
            )

            result = memory.consolidate(scope(), episode_ids=ingested.episode_ids)

            self.assertEqual([], result)
            batches = jsonl(root_for(tmp) / "ledgers" / "consolidations.jsonl")
            self.assertEqual(1, len(batches))
            self.assertEqual("pending", batches[0]["status"])
            self.assertEqual("OpenRouter consolidation request failed", batches[0]["reason"])
            self.assertEqual("OpenRouterRequestError", batches[0]["error_type"])
            serialized = json.dumps(batches, ensure_ascii=False)
            self.assertNotIn("prompt-sentinel", serialized)
            self.assertNotIn("redacted-key-sentinel", serialized)
            self.assertNotIn("raw-observation-sentinel", serialized)

            model_calls = jsonl(root_for(tmp) / "ledgers" / "model_calls.jsonl")
            self.assertEqual("failure", model_calls[0]["outcome"])
            self.assertNotIn("prompt-sentinel", json.dumps(model_calls, ensure_ascii=False))
            self.assertNotIn("redacted-key-sentinel", json.dumps(model_calls, ensure_ascii=False))

            self.assertEqual([], memory.consolidate(scope(), episode_ids=ingested.episode_ids))
            self.assertEqual([], memory.consolidate(scope(), episode_ids=ingested.episode_ids))
            batches = jsonl(root_for(tmp) / "ledgers" / "consolidations.jsonl")
            self.assertEqual(["pending", "completed"], [batch["status"] for batch in batches])
            self.assertEqual(2, transport_calls)

    def test_failed_correction_does_not_close_the_previous_active_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            original = memory.ingest_text(scope(), "请记住：我喜欢上午开会。", source="text_chat")

            with patch.object(memory.store, "append_revision", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    memory.correct(scope(), original.record_ids[0], "改为下午开会。")

            records = memory.list_records(scope(), include_inactive=True)
            self.assertEqual(1, len(records))
            self.assertEqual("active", records[0]["status"])
            self.assertEqual(original.record_ids[0], records[0]["record_id"])

    def test_episode_consolidation_is_model_candidate_only_and_batch_idempotent(self) -> None:
        class FakeConsolidationModel:
            def __init__(self) -> None:
                self.calls = 0

            def generate_candidates(self, observations, active_records, input_observation_ids):
                self.calls += 1
                return {
                    "candidates": [
                        {
                            "memory_type": "semantic",
                            "content": "项目代号是 Orchid。",
                            "evidence_refs": [f"observation:{input_observation_ids[0]}"],
                            "confidence": 0.91,
                            "risk": "low",
                            "sensitivity": "private",
                            "salience": 0.8,
                            "proposed_operation": "ADD",
                            "target_record_id": None,
                            "claim_key": "semantic:project-code:orchid",
                        }
                    ]
                }

        with tempfile.TemporaryDirectory() as tmp:
            model = FakeConsolidationModel()
            memory = Citefold(tmp, clock=fixed_clock, openrouter=model)
            ingested = memory.ingest_text(scope(), "会议确认项目代号是 Orchid。", source="meeting_note")

            first = memory.consolidate(scope(), episode_ids=ingested.episode_ids)
            second = memory.consolidate(scope(), episode_ids=ingested.episode_ids)

            self.assertEqual(1, model.calls)
            self.assertEqual(first, second)
            self.assertEqual("pending", first[0].status)
            self.assertEqual([], memory.list_records(scope()))
            batches = jsonl(root_for(tmp) / "ledgers" / "consolidations.jsonl")
            self.assertEqual(1, len(batches))
            self.assertEqual("completed", batches[0]["status"])

    def test_duplicate_ingest_is_idempotent_for_episode_and_trusted_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            text = "请记住：我喜欢先看结论。"

            first = memory.ingest_text(scope(), text, source="text_chat")
            second = memory.ingest_text(scope(), text, source="text_chat")

            base = root_for(tmp) / "ledgers"
            active = memory.list_records(scope())
            self.assertEqual(first.asset_ids, second.asset_ids)
            self.assertEqual(first.observation_ids, second.observation_ids)
            self.assertEqual(first.episode_ids, second.episode_ids)
            self.assertEqual(1, len(jsonl(base / "assets.jsonl")))
            self.assertEqual(1, len(jsonl(base / "observations.jsonl")))
            self.assertEqual(1, len(jsonl(base / "episodes.jsonl")))
            self.assertEqual(1, len(active))
            self.assertEqual(1, len(jsonl(base / "revisions.jsonl")))

    def test_user_correction_creates_a_new_version_and_supersedes_old_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            original = memory.ingest_text(
                scope(),
                "请记住：我喜欢上午开会。",
                source="text_chat",
            )

            corrected = memory.correct(
                scope(),
                original.record_ids[0],
                "我不喜欢上午开会，改为下午。",
                reason="用户明确纠正偏好",
            )

            records = memory.list_records(scope(), include_inactive=True)
            old = next(item for item in records if item["record_id"] == original.record_ids[0])
            new = next(item for item in records if item["record_id"] == corrected.record_id)
            self.assertEqual("superseded", old["status"])
            self.assertIsNotNone(old["valid_to"])
            self.assertEqual("active", new["status"])
            self.assertEqual(old["record_id"], new["supersedes_id"])
            self.assertEqual(2, new["version"])
            self.assertIn("下午", (root_for(tmp) / "profile" / "preferences.md").read_text(encoding="utf-8"))
            self.assertNotIn("喜欢上午开会。", (root_for(tmp) / "profile" / "preferences.md").read_text(encoding="utf-8"))

    def test_unresolved_claims_remain_active_and_are_exposed_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_text(scope(), "请记住：我喜欢上午开会。", source="text_chat")
            memory.ingest_text(scope(), "请记住：我喜欢下午开会。", source="text_chat")

            pack = memory.recall(scope(), "我喜欢什么时候开会？有什么偏好？")

            self.assertEqual(2, len(memory.list_records(scope())))
            self.assertEqual(1, len(pack.conflicts))
            self.assertIn("上午", pack.markdown)
            self.assertIn("下午", pack.markdown)
            self.assertIn("## Conflicts", pack.markdown)

    def test_all_four_long_term_types_go_through_pending_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            evidence = memory.append_event(scope(), "import", {"text": "用户提供的可信材料"})
            proposals = [
                {"memory_type": "episodic", "content": "周一完成了发布复盘。"},
                {"memory_type": "semantic", "content": "项目代号是 Orchid。"},
                {"memory_type": "prospective", "content": "周五前提交报价。"},
                {"memory_type": "procedural", "content": "发布前先运行只读检查并等待人工批准。"},
            ]

            candidates = memory.consolidate(
                scope(),
                candidates=[
                    {
                        **proposal,
                        "evidence_refs": [evidence.evidence_ref],
                        "source_origin": "model_generated",
                        "confidence": 0.9,
                    }
                    for proposal in proposals
                ],
            )

            self.assertEqual({"pending"}, {candidate.status for candidate in candidates})
            self.assertEqual([], memory.list_records(scope()))
            for candidate in candidates:
                memory.approve_candidate(scope(), candidate.candidate_id)
            self.assertEqual(
                {"episodic", "semantic", "prospective", "procedural"},
                {record["memory_type"] for record in memory.list_records(scope())},
            )
            procedure = next(record for record in memory.list_records(scope()) if record["memory_type"] == "procedural")
            self.assertFalse(procedure["metadata"]["executable"])
            self.assertFalse(procedure["metadata"]["grants_permissions"])

    def test_consolidation_operations_are_revisioned_without_in_place_fact_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            evidence = memory.append_event(scope(), "note", {"text": "Orchid evidence"})

            add = memory.consolidate(
                scope(),
                candidates=[
                    {
                        "memory_type": "semantic",
                        "content": "项目代号是 Orchid。",
                        "evidence_refs": [evidence.evidence_ref],
                        "source_origin": "model_generated",
                        "confidence": 0.9,
                        "claim_key": "semantic:project-code",
                        "proposed_operation": "ADD",
                    }
                ],
            )[0]
            memory.approve_candidate(scope(), add.candidate_id)
            first = memory.list_records(scope())[0]

            reinforce = memory.consolidate(
                scope(),
                candidates=[
                    {
                        "memory_type": "semantic",
                        "content": "项目代号是 Orchid。",
                        "evidence_refs": [evidence.evidence_ref],
                        "source_origin": "model_generated",
                        "confidence": 0.99,
                        "claim_key": "semantic:project-code",
                        "proposed_operation": "REINFORCE",
                        "target_record_id": first["record_id"],
                    }
                ],
            )[0]
            memory.approve_candidate(scope(), reinforce.candidate_id)
            reinforced = memory.list_records(scope())[0]
            self.assertEqual(first["record_id"], reinforced["record_id"])
            self.assertEqual(first["confidence"], reinforced["confidence"])
            self.assertEqual(1, reinforced["metadata"]["reinforcement_count"])

            ignored = memory.consolidate(
                scope(),
                candidates=[
                    {
                        "memory_type": "semantic",
                        "content": "无关噪声",
                        "evidence_refs": [evidence.evidence_ref],
                        "source_origin": "model_generated",
                        "confidence": 0.1,
                        "proposed_operation": "IGNORE",
                    }
                ],
            )[0]
            memory.approve_candidate(scope(), ignored.candidate_id)
            self.assertEqual(1, len(memory.list_records(scope())))

            operations = [item["operation"] for item in jsonl(root_for(tmp) / "ledgers" / "revisions.jsonl")]
            self.assertEqual(["ADD", "REINFORCE"], operations)


if __name__ == "__main__":
    unittest.main()
