import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from citefold import MemoryScope, Citefold, ScopeError


VALID_SCOPE = {
    "tenant_id": "tenant-a",
    "user_id": "user-1",
    "namespace": "personal",
    "agent_id": "agent-text",
    "session_id": "session-1",
}


def fixed_clock() -> datetime:
    return datetime(2026, 6, 3, 10, 30, tzinfo=timezone.utc)


def memory_scope() -> MemoryScope:
    return MemoryScope(**VALID_SCOPE)


def scope_root(tmp: str) -> Path:
    return (
        Path(tmp).resolve()
        / "tenants"
        / "tenant-a"
        / "users"
        / "user-1"
        / "namespaces"
        / "personal"
    )


class MemoryScopeTest(unittest.TestCase):
    def test_scope_requires_all_identity_fields(self) -> None:
        for field in VALID_SCOPE:
            with self.subTest(field=field):
                values = dict(VALID_SCOPE)
                values[field] = " "

                with self.assertRaises(ScopeError):
                    MemoryScope(**values)

        invalid_values = {
            "tenant_id": None,
            "user_id": 123,
            "namespace": [],
            "agent_id": {},
            "session_id": False,
        }
        for field, invalid_value in invalid_values.items():
            with self.subTest(field=field, invalid_value=invalid_value):
                values = dict(VALID_SCOPE)
                values[field] = invalid_value

                with self.assertRaises(ScopeError):
                    MemoryScope(**values)

        scope = MemoryScope(**VALID_SCOPE)
        self.assertEqual(VALID_SCOPE, scope.as_record())

    def test_scope_rejects_path_unsafe_identity_values(self) -> None:
        invalid_values = [
            ".",
            "..",
            " tenant-a",
            "tenant-a ",
            "tenant/a",
            "tenant\\a",
            "tenant a",
            "租户",
        ]

        for field in VALID_SCOPE:
            for invalid_value in invalid_values:
                with self.subTest(field=field, invalid_value=invalid_value):
                    values = dict(VALID_SCOPE)
                    values[field] = invalid_value

                    with self.assertRaises(ScopeError):
                        MemoryScope(**values)

        for field in VALID_SCOPE:
            with self.subTest(field=field, invalid_value="overlong"):
                values = dict(VALID_SCOPE)
                values[field] = "a" * 129
                with self.assertRaisesRegex(ScopeError, "at most 128"):
                    MemoryScope(**values)

    def test_append_event_uses_tenant_user_namespace_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            scope = memory_scope()

            result = memory.append_event(
                scope=scope,
                source="text_chat",
                payload={"text": "hello"},
                metadata={"channel": "desktop"},
            )

            expected_path = (
                scope_root(tmp)
                / "evidence"
                / "2026-06"
                / "2026-06-03.jsonl"
            )
            self.assertEqual(expected_path, result.path)
            self.assertEqual(Path("evidence") / "2026-06" / "2026-06-03.jsonl", Path(result.evidence_ref))

            record = json.loads(expected_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual("tenant-a", record["tenant_id"])
            self.assertEqual("user-1", record["user_id"])
            self.assertEqual("personal", record["namespace"])
            self.assertEqual("agent-text", record["agent_id"])
            self.assertEqual("session-1", record["session_id"])
            self.assertEqual("text_chat", record["source"])
            self.assertEqual({"text": "hello"}, record["payload"])
            self.assertEqual({"channel": "desktop"}, record["metadata"])

    def test_ingest_chat_materializes_generator_messages_for_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            messages = iter(
                [
                    {
                        "role": "user",
                        "content": "请记住：技术方案我喜欢简洁。",
                    }
                ]
            )

            memory.ingest_chat(scope=memory_scope(), messages=messages, source="text_chat")

            episode = next((scope_root(tmp) / "episodes" / "2026-06").glob("*-chat.md"))
            self.assertIn("技术方案我喜欢简洁", episode.read_text(encoding="utf-8"))

    def test_candidate_and_audit_records_include_scope_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            scope = memory_scope()
            evidence = memory.append_event(
                scope=scope,
                source="crm_agent",
                payload={"text": "CRM Agent 发现：客户 Alex 偏好周五下午跟进。"},
            )

            memory.submit_candidate(
                scope=scope,
                source_agent="crm_agent",
                memory_type="people",
                content="Alex 偏好周五下午跟进。",
                evidence_refs=[evidence.evidence_ref],
                confidence=0.82,
            )

            root = scope_root(tmp)
            candidate = json.loads((root / "indexes" / "candidates.json").read_text(encoding="utf-8"))[0]
            audit_records = [
                json.loads(line)
                for line in (root / "audit" / "memory_events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            for key, value in scope.as_record().items():
                self.assertEqual(value, candidate[key])
                self.assertEqual(value, audit_records[0]["data"][key])
                self.assertEqual(value, audit_records[1]["data"][key])

    def test_recall_is_isolated_by_tenant_user_and_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            tenant_a_personal = MemoryScope(
                tenant_id="tenant-a",
                user_id="user-1",
                namespace="personal",
                agent_id="text-agent",
                session_id="session-a",
            )
            tenant_b_personal = MemoryScope(
                tenant_id="tenant-b",
                user_id="user-1",
                namespace="personal",
                agent_id="text-agent",
                session_id="session-b",
            )
            tenant_a_other_user = MemoryScope(
                tenant_id="tenant-a",
                user_id="user-2",
                namespace="personal",
                agent_id="text-agent",
                session_id="session-other-user",
            )
            tenant_a_work = MemoryScope(
                tenant_id="tenant-a",
                user_id="user-1",
                namespace="work",
                agent_id="text-agent",
                session_id="session-c",
            )

            memory.ingest_text(
                scope=tenant_a_personal,
                text="请记住：我个人偏好周末不要安排工作会议。",
                source="text_chat",
            )
            memory.ingest_text(
                scope=tenant_a_work,
                text="请记住：工作方案我喜欢先给风险清单。",
                source="text_chat",
            )

            tenant_a_personal_pack = memory.recall(tenant_a_personal, "我有什么偏好？").markdown
            tenant_a_work_pack = memory.recall(tenant_a_work, "我有什么偏好？").markdown
            tenant_b_pack = memory.recall(tenant_b_personal, "我有什么偏好？").markdown
            tenant_a_other_user_pack = memory.recall(tenant_a_other_user, "我有什么偏好？").markdown

            self.assertIn("周末不要安排工作会议", tenant_a_personal_pack)
            self.assertNotIn("工作方案我喜欢先给风险清单", tenant_a_personal_pack)
            self.assertIn("工作方案我喜欢先给风险清单", tenant_a_work_pack)
            self.assertNotIn("周末不要安排工作会议", tenant_a_work_pack)
            self.assertNotIn("周末不要安排工作会议", tenant_b_pack)
            self.assertNotIn("工作方案我喜欢先给风险清单", tenant_b_pack)
            self.assertNotIn("周末不要安排工作会议", tenant_a_other_user_pack)
            self.assertNotIn("工作方案我喜欢先给风险清单", tenant_a_other_user_pack)

    def test_memory_pack_includes_identity_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            scope = memory_scope()

            memory.ingest_text(
                scope=scope,
                text="请记住：我喜欢先结论后细节。",
                source="text_chat",
            )

            pack = memory.recall(scope, "回答风格有什么偏好？")

            self.assertEqual(scope.as_record(), pack.identity_scope)
            self.assertIn("## Identity Scope", pack.markdown)
            self.assertIn("- tenant_id: tenant-a", pack.markdown)
            self.assertIn("- namespace: personal", pack.markdown)


if __name__ == "__main__":
    unittest.main()
