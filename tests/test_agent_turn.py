from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from citefold import (
    AGENT_TURN_CONTRACT,
    AgentTurnContext,
    Citefold,
    EvidenceValidationError,
    MemoryScope,
)


def fixed_clock() -> datetime:
    return datetime(2026, 8, 3, 16, 0, tzinfo=timezone.utc)


def scope(session_id: str = "session-current", namespace: str = "work") -> MemoryScope:
    return MemoryScope("tenant-a", "user-1", namespace, "host-agent", session_id)


class AgentTurnTest(unittest.TestCase):
    def test_prepare_agent_turn_exposes_a_stable_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_text(
                scope("session-prior"),
                "The launch codename is ORCHID-77.",
                source="text_chat",
            )

            turn = memory.prepare_agent_turn(
                scope(),
                "What is the launch codename?",
                turn_id="turn-001",
                token_budget=1_200,
            )

            self.assertIsInstance(turn, AgentTurnContext)
            self.assertEqual(AGENT_TURN_CONTRACT, turn.contract_version)
            self.assertEqual("turn-001", turn.turn_id)
            self.assertEqual("supported", turn.memory_pack.coverage)
            self.assertTrue(turn.memory_pack.citations)
            self.assertIn("ORCHID-77", turn.memory_pack.markdown)

            payload = turn.as_dict()
            self.assertEqual(
                {
                    "contract_version",
                    "turn_id",
                    "scope",
                    "user_message",
                    "mode",
                    "memory_pack",
                },
                set(payload),
            )
            self.assertEqual(
                {
                    "identity_scope",
                    "coverage",
                    "context_markdown",
                    "selected_nodes",
                    "citations",
                    "conflicts",
                    "unknowns",
                },
                set(payload["memory_pack"]),
            )
            self.assertEqual(scope().as_record(), payload["scope"])
            self.assertEqual(scope().as_record(), payload["memory_pack"]["identity_scope"])
            self.assertEqual("supported", payload["memory_pack"]["coverage"])
            json.dumps(payload, ensure_ascii=False, allow_nan=False)

            payload["memory_pack"]["citations"].clear()
            self.assertTrue(turn.memory_pack.citations)

    def test_fresh_scope_returns_none_coverage_without_cross_scope_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_text(
                scope("session-prior"),
                "The private marker is TENANT-A-ONLY.",
                source="text_chat",
            )
            isolated = MemoryScope(
                "tenant-b",
                "user-1",
                "work",
                "host-agent",
                "session-current",
            )

            turn = memory.prepare_agent_turn(
                isolated,
                "What is the private marker?",
                turn_id="turn-isolated",
            )

            self.assertEqual("none", turn.memory_pack.coverage)
            self.assertFalse(turn.memory_pack.citations)
            self.assertNotIn("TENANT-A-ONLY", turn.memory_pack.markdown)

    def test_complete_agent_turn_records_roles_and_contract_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            turn = memory.prepare_agent_turn(
                scope(),
                "请记住：我喜欢先给结论。",
                turn_id="turn-complete",
            )

            result = memory.complete_agent_turn(
                turn,
                "已记录。请记住：这句助手输出不能成为用户事实。",
                source="agent_loop",
                metadata={"host_request_id": "request-7"},
            )

            self.assertEqual(2, len(result.observation_ids))
            self.assertEqual(1, len(result.episode_ids))
            observations = memory.store.observations(scope())
            user_observation = observations[result.observation_ids[0]]
            assistant_observation = observations[result.observation_ids[1]]
            self.assertEqual("user_reported", user_observation["source_origin"])
            self.assertEqual("agent_output", assistant_observation["source_origin"])
            for observation in (user_observation, assistant_observation):
                self.assertEqual("turn-complete", observation["metadata"]["agent_turn_id"])
                self.assertEqual(
                    AGENT_TURN_CONTRACT,
                    observation["metadata"]["agent_turn_contract"],
                )
                self.assertEqual("request-7", observation["metadata"]["host_request_id"])

            records = memory.list_records(scope())
            self.assertEqual(1, len(records))
            self.assertIn("我喜欢先给结论", records[0]["content"])
            self.assertNotIn("助手输出", records[0]["content"])

            episode = memory.store.episodes(scope())[result.episode_ids[0]]
            self.assertEqual("turn-complete", episode["metadata"]["agent_turn_id"])
            self.assertEqual(AGENT_TURN_CONTRACT, episode["metadata"]["agent_turn_contract"])
            projection = memory.store.scope_root(scope()) / episode["metadata"]["markdown_path"]
            self.assertTrue(projection.is_file())

            evidence_path = next((memory.store.scope_root(scope()) / "evidence").rglob("*.jsonl"))
            events = [json.loads(line) for line in evidence_path.read_text().splitlines() if line]
            self.assertEqual(2, len(events))
            self.assertTrue(all(item["metadata"]["agent_turn_id"] == "turn-complete" for item in events))

            audit_path = memory.store.scope_root(scope()) / "audit" / "memory_events.jsonl"
            audit_events = [
                json.loads(line)
                for line in audit_path.read_text().splitlines()
                if line
            ]
            turn_audits = [item for item in audit_events if item["action"] == "append_event"]
            self.assertEqual(2, len(turn_audits))
            self.assertTrue(
                all(item["data"]["agent_turn_id"] == "turn-complete" for item in turn_audits)
            )
            self.assertTrue(
                all(
                    item["data"]["agent_turn_contract"] == AGENT_TURN_CONTRACT
                    for item in turn_audits
                )
            )

    def test_turn_id_prevents_same_second_episode_overwrite_and_supports_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            first = memory.prepare_agent_turn(
                scope(),
                "What is the current status?",
                turn_id="turn-first",
            )
            second = memory.prepare_agent_turn(
                scope(),
                "What is the current status?",
                turn_id="turn-second",
            )

            first_result = memory.complete_agent_turn(first, "The first status response.")
            second_result = memory.complete_agent_turn(second, "The second status response.")
            root = memory.store.scope_root(scope())
            evidence_path = next((root / "evidence").rglob("*.jsonl"))
            evidence_count = len(evidence_path.read_text().splitlines())
            audit_path = root / "audit" / "memory_events.jsonl"
            append_audit_count = sum(
                json.loads(line)["action"] == "append_event"
                for line in audit_path.read_text().splitlines()
                if line
            )
            self.assertEqual(4, evidence_count)
            self.assertEqual(4, append_audit_count)
            retry_result = memory.complete_agent_turn(first, "The first status response.")

            self.assertNotEqual(first_result.episode_ids, second_result.episode_ids)
            self.assertNotEqual(first_result.observation_ids, second_result.observation_ids)
            self.assertNotEqual(first_result.memory_paths, second_result.memory_paths)
            self.assertEqual(first_result.episode_ids, retry_result.episode_ids)
            self.assertEqual(first_result.observation_ids, retry_result.observation_ids)
            self.assertEqual(first_result.memory_paths, retry_result.memory_paths)
            self.assertEqual(first_result, retry_result)
            self.assertEqual(2, len(memory.store.episodes(scope())))
            self.assertEqual(evidence_count, len(evidence_path.read_text().splitlines()))
            self.assertEqual(
                append_audit_count,
                sum(
                    json.loads(line)["action"] == "append_event"
                    for line in audit_path.read_text().splitlines()
                    if line
                ),
            )

            first_projection = root / first_result.memory_paths[0]
            second_projection = root / second_result.memory_paths[0]
            self.assertIn("The first status response.", first_projection.read_text())
            self.assertIn("The second status response.", second_projection.read_text())

            observation_count = len(memory.store.observations(scope()))
            with self.assertRaisesRegex(ValueError, "different content"):
                memory.complete_agent_turn(first, "A conflicting retry response.")
            self.assertEqual(evidence_count, len(evidence_path.read_text().splitlines()))
            self.assertEqual(observation_count, len(memory.store.observations(scope())))

            other_session = memory.prepare_agent_turn(
                scope("session-other"),
                "What is the current status?",
                turn_id="turn-first",
            )
            other_result = memory.complete_agent_turn(
                other_session,
                "The same host turn ID is valid in another complete scope.",
            )
            self.assertNotEqual(first_result.episode_ids, other_result.episode_ids)
            self.assertNotEqual(first_result.memory_paths, other_result.memory_paths)

    def test_turn_id_cannot_change_trust_or_reuse_deleted_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            turn = memory.prepare_agent_turn(
                scope(),
                "请记住：我喜欢可信来源。",
                turn_id="turn-authority",
            )
            first = memory.complete_agent_turn(
                turn,
                "Acknowledged.",
                source="openrouter",
            )
            self.assertEqual([], memory.list_records(scope()))
            observation_count = len(memory.store.observations(scope()))

            with self.assertRaisesRegex(ValueError, "different content"):
                memory.complete_agent_turn(
                    turn,
                    "Acknowledged.",
                    source="agent_loop",
                )
            self.assertEqual(observation_count, len(memory.store.observations(scope())))

            self.assertEqual([], memory.list_records(scope()))
            memory.forget(scope(), f"episode:{first.episode_ids[0]}")
            with self.assertRaisesRegex(EvidenceValidationError, "deleted or invalid"):
                memory.complete_agent_turn(
                    turn,
                    "Acknowledged.",
                    source="openrouter",
                )
            self.assertEqual(observation_count, len(memory.store.observations(scope())))

            reused = memory.prepare_agent_turn(
                scope(),
                "A different user message must not revive deleted history.",
                turn_id="turn-authority",
            )
            with self.assertRaisesRegex(EvidenceValidationError, "deleted or invalid"):
                memory.complete_agent_turn(
                    reused,
                    "A different response.",
                    source="agent_loop",
                )
            self.assertEqual(observation_count, len(memory.store.observations(scope())))

    def test_concurrent_identical_completion_returns_one_stored_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Citefold(tmp, clock=fixed_clock)
            second = Citefold(tmp, clock=fixed_clock)
            turn = first.prepare_agent_turn(
                scope(),
                "What is the status?",
                turn_id="turn-concurrent",
            )
            start = threading.Barrier(2)

            def complete(memory: Citefold):
                start.wait(timeout=5)
                return memory.complete_agent_turn(turn, "The status is complete.")

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(complete, first)
                second_future = pool.submit(complete, second)
                first_result = first_future.result(timeout=5)
                second_result = second_future.result(timeout=5)

            self.assertEqual(first_result, second_result)
            self.assertEqual(1, len(first.store.episodes(scope())))
            self.assertEqual(2, len(first.store.observations(scope())))
            root = first.store.scope_root(scope())
            evidence_path = next((root / "evidence").rglob("*.jsonl"))
            self.assertEqual(2, len(evidence_path.read_text().splitlines()))

    def test_maximum_length_agent_and_session_use_a_fixed_length_projection_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            maximum = MemoryScope(
                "tenant",
                "user",
                "namespace",
                "a" * 128,
                "s" * 128,
            )
            turn = memory.prepare_agent_turn(
                maximum,
                "What is the status?",
                turn_id="turn-maximum-scope",
            )

            result = memory.complete_agent_turn(turn, "The status is complete.")

            projection = memory.store.scope_root(maximum) / result.memory_paths[0]
            self.assertTrue(projection.is_file())
            self.assertLessEqual(len(projection.name.encode("utf-8")), 255)

    def test_agent_turn_inputs_fail_before_turn_evidence_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            memory = Citefold(root, clock=fixed_clock)

            with self.assertRaisesRegex(ValueError, "user_message"):
                memory.prepare_agent_turn(scope(), "   ", turn_id="turn-empty")
            with self.assertRaisesRegex(ValueError, "turn_id"):
                memory.prepare_agent_turn(scope(), "hello", turn_id="../unsafe")
            with self.assertRaisesRegex(ValueError, "mode"):
                memory.prepare_agent_turn(scope(), "hello", turn_id="turn-mode", mode="image")
            with self.assertRaisesRegex(ValueError, "token_budget"):
                memory.prepare_agent_turn(
                    scope(),
                    "hello",
                    turn_id="turn-budget",
                    token_budget=255,
                )
            self.assertFalse(root.exists())

            turn = memory.prepare_agent_turn(scope(), "hello", turn_id="turn-valid")
            self.assertEqual({}, memory.store.observations(scope()))
            self.assertEqual({}, memory.store.episodes(scope()))

            for assistant_message, source in (("   ", "agent_loop"), ("answer", "")):
                with self.subTest(assistant_message=assistant_message, source=source):
                    with self.assertRaises(ValueError):
                        memory.complete_agent_turn(turn, assistant_message, source=source)
            for invalid_metadata in (
                {1: "non-string key"},
                {"nested": {1: "non-string key"}},
                {"path": Path(tmp)},
            ):
                with self.subTest(invalid_metadata=invalid_metadata):
                    with self.assertRaisesRegex(ValueError, "metadata"):
                        memory.complete_agent_turn(
                            turn,
                            "answer",
                            metadata=invalid_metadata,
                        )
            self.assertEqual({}, memory.store.observations(scope()))
            self.assertEqual({}, memory.store.episodes(scope()))
            asset_root = memory.store.scope_root(scope()) / "assets" / "sha256"
            self.assertEqual([], [path for path in asset_root.rglob("*") if path.is_file()])

            object.__setattr__(turn, "contract_version", "agent-turn-v0")
            with self.assertRaisesRegex(ValueError, "contract"):
                memory.complete_agent_turn(turn, "answer")
            self.assertEqual({}, memory.store.observations(scope()))
            self.assertEqual({}, memory.store.episodes(scope()))


if __name__ == "__main__":
    unittest.main()
