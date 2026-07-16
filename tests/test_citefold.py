import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from citefold import MemoryScope, Citefold


def fixed_clock() -> datetime:
    return datetime(2026, 6, 3, 10, 30, tzinfo=timezone.utc)


def text_scope() -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant-a",
        user_id="user-1",
        namespace="personal",
        agent_id="text-agent",
        session_id="text-session",
    )


def voice_scope() -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant-a",
        user_id="user-1",
        namespace="personal",
        agent_id="voice-agent",
        session_id="voice-session",
    )


def crm_scope() -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant-a",
        user_id="user-1",
        namespace="personal",
        agent_id="crm-agent",
        session_id="crm-session",
    )


def scope_root(tmp: str) -> Path:
    return (
        Path(tmp)
        / "tenants"
        / "tenant-a"
        / "users"
        / "user-1"
        / "namespaces"
        / "personal"
    )


class CitefoldTest(unittest.TestCase):
    def test_chat_episodes_are_unique_per_session_on_same_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            first_scope = text_scope()
            second_scope = MemoryScope(
                tenant_id=first_scope.tenant_id,
                user_id=first_scope.user_id,
                namespace=first_scope.namespace,
                agent_id=first_scope.agent_id,
                session_id="text-session-2",
            )

            first = memory.ingest_chat(first_scope, [{"role": "user", "content": "First same-day episode"}])
            second = memory.ingest_chat(second_scope, [{"role": "user", "content": "Second same-day episode"}])
            episode_paths = sorted((scope_root(tmp) / "episodes").rglob("*-chat.md"))
            episode_contents = [path.read_text(encoding="utf-8") for path in episode_paths]

        self.assertEqual(2, len(episode_paths))
        self.assertNotEqual(first.memory_paths, second.memory_paths)
        self.assertTrue(any("First same-day episode" in content for content in episode_contents))
        self.assertTrue(any("Second same-day episode" in content for content in episode_contents))

    def test_recall_selects_lexically_relevant_episode_without_keyword_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_chat(
                text_scope(),
                [{"role": "user", "content": "I graduated with a Business Administration degree."}],
            )

            pack = memory.recall(text_scope(), "What degree did I graduate with?")

        self.assertIn("Business Administration", pack.markdown)
        self.assertTrue(any(node.node_id.startswith("episodes.") for node in pack.selected_nodes))

    def test_episode_excerpt_keeps_relevant_line_under_small_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            messages = [
                {"role": "user", "content": f"Routine unrelated status line {index}."}
                for index in range(40)
            ]
            messages.append({"role": "user", "content": "The launch codeword is ORCHID-91."})
            memory.ingest_chat(text_scope(), messages)

            pack = memory.recall(text_scope(), "What is the launch codeword?", token_budget=500)

        self.assertIn("ORCHID-91", pack.markdown)
        self.assertLess(len(pack.markdown), 1400)

    def test_episode_excerpt_keeps_relevant_tail_of_long_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_chat(
                text_scope(),
                [
                    {"role": "user", "content": "Recommend a launch venue."},
                    {
                        "role": "assistant",
                        "content": "Background. " * 200 + "My final recommendation is Harbor Hall.",
                    },
                ],
            )

            pack = memory.recall(
                text_scope(),
                "What launch venue did you recommend?",
                token_budget=500,
            )

        self.assertIn("Harbor Hall", pack.markdown)
        self.assertLess(len(pack.markdown), 1600)

    def test_recall_without_matching_evidence_does_not_inject_default_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_chat(
                text_scope(),
                [{"role": "user", "content": "I graduated with a Business Administration degree."}],
            )

            pack = memory.recall(text_scope(), "What is my passport number?")

        self.assertEqual([], pack.selected_nodes)
        self.assertNotIn("Business Administration", pack.markdown)

    def test_text_chat_commits_preferences_tasks_and_memory_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            scope = text_scope()

            result = memory.ingest_chat(
                scope=scope,
                messages=[
                    {
                        "role": "user",
                        "content": "请记住：技术方案我喜欢简洁，先结论后细节。",
                    },
                    {
                        "role": "user",
                        "content": "提醒我明天上午10点给王明发周报，等李雷回复预算表后再跟进。",
                    },
                ],
                source="text_chat",
            )

            self.assertGreaterEqual(len(result.committed), 2)
            user_root = scope_root(tmp)
            self.assertIn("技术方案我喜欢简洁", (user_root / "profile" / "preferences.md").read_text())
            self.assertIn("给王明发周报", (user_root / "tasks" / "commitments.md").read_text())
            self.assertIn("李雷回复预算表", (user_root / "tasks" / "waiting_on.md").read_text())

            pack = memory.recall(scope, "我现在有什么待办？回答风格有什么偏好？")

            self.assertIn("MemoryPack", pack.markdown)
            self.assertEqual(scope.as_record(), pack.identity_scope)
            self.assertIn("技术方案我喜欢简洁", pack.markdown)
            self.assertIn("给王明发周报", pack.markdown)
            self.assertIn("李雷回复预算表", pack.markdown)
            self.assertTrue(any(node.node_id.startswith("tasks.") for node in pack.selected_nodes))
            self.assertTrue(any(node.node_id.startswith("profile.") for node in pack.selected_nodes))

            evidence_lines = (user_root / "evidence" / "2026-06" / "2026-06-03.jsonl").read_text().splitlines()
            self.assertEqual(2, len(evidence_lines))
            self.assertEqual("text_chat", json.loads(evidence_lines[0])["source"])

            tree = json.loads((user_root / "indexes" / "memory_tree.json").read_text())
            self.assertEqual("root", tree["node_id"])
            self.assertIn("tasks", {child["node_id"] for child in tree["children"]})

            generated_names = [path.name.lower() for path in user_root.rglob("*")]
            self.assertFalse(any("vector" in name or "embedding" in name for name in generated_names))

    def test_voice_realtime_buffer_does_not_pollute_long_term_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            scope = voice_scope()

            memory.ingest_text(
                scope=scope,
                text="实时语音片段：可能听错了，披萨项目不是偏好，只是在讨论午餐。",
                source="voice_transcript",
                mode="voice",
                final=False,
            )

            user_root = scope_root(tmp)
            self.assertIn("披萨项目", (user_root / "active" / "recent_voice_buffer.md").read_text())
            self.assertNotIn("披萨项目", (user_root / "profile" / "preferences.md").read_text())

            memory.ingest_text(
                scope=scope,
                text="语音会话结束：会议结论是周五前完成客户方案。提醒我周五上午9点检查客户方案。",
                source="voice_transcript",
                mode="voice",
                final=True,
            )

            voice_episode = next((user_root / "episodes" / "2026-06").glob("*-voice.md"))
            self.assertIn("周五前完成客户方案", voice_episode.read_text())
            self.assertIn("周五上午9点检查客户方案", (user_root / "tasks" / "commitments.md").read_text())

            pack = memory.recall(scope, "客户方案有什么安排？", mode="voice")
            self.assertIn("周五上午9点检查客户方案", pack.markdown)
            self.assertNotIn("recent_voice_buffer.md", pack.markdown)

    def test_final_voice_supersedes_only_same_session_partial_working_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            current = voice_scope()
            other = MemoryScope(
                tenant_id=current.tenant_id,
                user_id=current.user_id,
                namespace=current.namespace,
                agent_id=current.agent_id,
                session_id="other-voice-session",
            )
            partial_text = "Unfinished transcript: launch venue might be Central Park."
            final_text = "Final transcript: the launch venue is Harbor Hall."
            other_partial = "Other session partial: lighting plan remains blue."

            partial = memory.ingest_text(
                current,
                partial_text,
                source="voice_transcript",
                mode="voice",
                final=False,
            )
            other_result = memory.ingest_text(
                other,
                other_partial,
                source="voice_transcript",
                mode="voice",
                final=False,
            )
            finalized = memory.ingest_text(
                current,
                final_text,
                source="voice_transcript",
                mode="voice",
                final=True,
            )

            projection = (scope_root(tmp) / "active" / "recent_voice_buffer.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual("", memory._canonical_active_voice_text(current))
            self.assertNotIn(partial_text, projection)
            self.assertNotIn(final_text, projection)
            self.assertIn(other_partial, memory._canonical_active_voice_text(other))
            tombstones = memory.store.read_ledger(current, "deletions")
            self.assertEqual(1, len(tombstones))
            self.assertIn(partial.observation_ids[0], tombstones[0]["target_ref"])
            self.assertNotIn(other_result.observation_ids[0], tombstones[0]["target_ref"])
            self.assertTrue(
                memory.validate_evidence(current, f"observation:{partial.observation_ids[0]}")
            )

            pack = memory.recall(current, "What is the final launch venue?", mode="voice")
            self.assertIn(final_text, pack.markdown)
            self.assertNotIn(partial_text, pack.markdown)
            self.assertFalse(
                any(node.path == "active/recent_voice_buffer.md" for node in pack.selected_nodes)
            )
            self.assertTrue(
                any(citation["ref"] == f"observation:{finalized.observation_ids[0]}" for citation in pack.citations)
            )
            self.assertIn(final_text, (scope_root(tmp) / finalized.memory_paths[0]).read_text(encoding="utf-8"))

            other_pack = memory.recall(other, "What remains in the lighting plan?", mode="voice")
            self.assertIn(other_partial, other_pack.markdown)

    def test_third_party_agent_candidates_require_approval_before_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            scope = crm_scope()
            evidence = memory.append_event(
                scope=scope,
                source="crm_agent",
                payload={"text": "CRM Agent 发现：客户 Alex 偏好周五下午跟进。"},
            )

            candidate = memory.submit_candidate(
                scope=scope,
                source_agent="crm_agent",
                memory_type="people",
                content="Alex 偏好周五下午跟进。",
                evidence_refs=[evidence.evidence_ref],
                confidence=0.82,
            )

            user_root = scope_root(tmp)
            self.assertIn(candidate.candidate_id, (user_root / "candidates" / "pending_agent_updates.md").read_text())
            self.assertNotIn("Alex 偏好周五下午跟进", memory.recall(scope, "Alex 怎么跟进？").markdown)

            memory.approve_candidate(scope, candidate.candidate_id)
            pack = memory.recall(scope, "Alex 怎么跟进？")

            self.assertIn("Alex 偏好周五下午跟进", pack.markdown)
            self.assertIn("Source: crm_agent", (user_root / "people" / "alex.md").read_text())
            self.assertIn(candidate.candidate_id, (user_root / "audit" / "memory_events.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
