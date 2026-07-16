import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from citefold import MemoryScope, Citefold


def fixed_clock() -> datetime:
    return datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def scope() -> MemoryScope:
    return MemoryScope("tenant-a", "user-1", "personal", "recall-agent", "session-1")


def scope_root(tmp: str) -> Path:
    return Path(tmp) / "tenants" / "tenant-a" / "users" / "user-1" / "namespaces" / "personal"


class FakeEmbeddingClient:
    models = SimpleNamespace(observation="fake-media", embedding="fake-embedding")

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, inputs):
        self.calls += 1
        vectors = []
        for text in inputs:
            normalized = text.lower()
            if "orchid" in normalized or "secret flower" in normalized:
                vectors.append([1.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0])
        return vectors


class FailingQueryEmbeddingClient(FakeEmbeddingClient):
    def embed(self, inputs):
        if len(inputs) == 1 and inputs[0].startswith("What"):
            raise RuntimeError("provider unavailable")
        return super().embed(inputs)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class HybridRecallTest(unittest.TestCase):
    def test_explicit_reminder_can_recall_assistant_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_chat(
                scope(),
                [
                    {"role": "assistant", "content": "The average HAMT framerate improvement was 20 percent."},
                    {"role": "user", "content": "Please turn the supplied sections into an academic review."},
                ],
            )

            pack = memory.recall(
                scope(),
                "Can you remind me what was the average HAMT framerate improvement?",
            )
            prospective = memory.recall(
                scope(),
                "Remind me to check the HAMT framerate improvement tomorrow.",
            )

            self.assertIn("20 percent", pack.markdown)
            self.assertEqual("agent_output", pack.citations[0]["source_origin"])
            self.assertNotIn("20 percent", prospective.markdown)

    def test_recommendation_can_use_assistant_text_only_as_episode_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_chat(
                scope(),
                [
                    {
                        "role": "user",
                        "content": "My equipment is a Sony A7R IV with a Godox V1. USER-CONTEXT-MARKER.",
                    },
                    {
                        "role": "assistant",
                        "content": (
                            "I can suggest accessories that complement your photography setup. "
                            "ASSISTANT-NAV-MARKER."
                        ),
                    },
                ],
            )

            pack = memory.recall(
                scope(),
                "Can you suggest accessories that complement my photography setup?",
            )

            self.assertIn("USER-CONTEXT-MARKER", pack.markdown)
            self.assertNotIn("ASSISTANT-NAV-MARKER", pack.markdown)
            self.assertTrue(pack.citations)
            self.assertTrue(all(item["source_origin"] == "user_reported" for item in pack.citations))

    def test_last_weekday_query_prefers_timestamped_user_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = MutableClock(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc))
            memory = Citefold(tmp, clock=clock)
            memory.ingest_text(
                scope(),
                "I recently acquired an heirloom; my aunt gave it to me today. TEMPORAL-GOLD-MARKER.",
                source="text_chat",
            )
            for index in range(6):
                clock.value = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc) + timedelta(days=index)
                memory.ingest_text(
                    MemoryScope(
                        "tenant-a",
                        "user-1",
                        "personal",
                        "recall-agent",
                        f"distractor-{index}",
                    ),
                    f"I received jewelry from coworker {index} while organizing the collection.",
                    source="text_chat",
                )
            clock.value = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

            pack = memory.recall(scope(), "I received a piece of jewelry last Saturday from whom?")

            self.assertIn("TEMPORAL-GOLD-MARKER", pack.markdown)

    def test_sqlite_index_is_derived_and_rebuild_restores_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_chat(
                scope(),
                [{"role": "user", "content": "The launch codename is ORCHID-77."}],
            )
            before = memory.recall(scope(), "What is the launch codename?")
            index_path = scope_root(tmp) / "indexes" / "memory.sqlite3"
            self.assertTrue(index_path.exists())

            index_path.unlink()
            self.assertFalse(index_path.exists())
            stats = memory.rebuild(scope())
            after = memory.recall(scope(), "What is the launch codename?")

            self.assertTrue(index_path.exists())
            self.assertGreater(stats["documents"], 0)
            self.assertIn("ORCHID-77", before.markdown)
            self.assertIn("ORCHID-77", after.markdown)

    def test_embedding_rank_can_supply_episode_fallback_without_becoming_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            embeddings = FakeEmbeddingClient()
            memory = Citefold(tmp, clock=fixed_clock, openrouter=embeddings)
            ingested = memory.ingest_chat(
                scope(),
                [{"role": "user", "content": "The launch codename is ORCHID-77."}],
            )
            memory.ingest_chat(
                MemoryScope("tenant-a", "user-1", "personal", "recall-agent", "session-2"),
                [{"role": "user", "content": "The catering order contains noodles."}],
            )

            stats = memory.rebuild(scope(), embeddings=True)
            pack = memory.recall(scope(), "Which secret flower identifies the launch?")

            self.assertEqual(stats["documents"], stats["embeddings"])
            self.assertGreaterEqual(embeddings.calls, 2)
            self.assertIn("ORCHID-77", pack.markdown)
            self.assertEqual("supported", pack.coverage)
            self.assertEqual(1, len(pack.citations))
            self.assertEqual(ingested.observation_ids[0], pack.citations[0]["observation_id"])
            self.assertTrue(any("embedding" in node.reason.lower() or "rrf" in node.reason.lower() for node in pack.selected_nodes))

    def test_rebuild_never_indexes_records_with_deleted_or_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            invalid = memory.submit_candidate(
                scope(),
                source_agent="external-agent",
                memory_type="profile",
                content="UNSUPPORTED-MARKER-XYZ",
                evidence_refs=["evidence/missing.jsonl"],
                confidence=0.9,
            )
            self.assertEqual("pending", invalid.status)

            stats = memory.rebuild(scope())

            self.assertEqual(0, stats["trusted_records"])
            self.assertNotIn("UNSUPPORTED-MARKER-XYZ", memory.recall(scope(), "What did the external agent assert?").markdown)

    def test_embedding_failure_falls_back_to_local_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            embeddings = FailingQueryEmbeddingClient()
            memory = Citefold(tmp, clock=fixed_clock, openrouter=embeddings)
            memory.ingest_text(scope(), "The launch codename is ORCHID-77.", source="text_chat")
            memory.rebuild(scope(), embeddings=True)

            pack = memory.recall(scope(), "What is the launch codename?")

            self.assertIn("ORCHID-77", pack.markdown)

    def test_automatic_local_rebuild_preserves_matching_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            embeddings = FakeEmbeddingClient()
            memory = Citefold(tmp, clock=fixed_clock, openrouter=embeddings)
            memory.ingest_text(scope(), "The launch codename is ORCHID-77.", source="text_chat")
            memory.rebuild(scope(), embeddings=True)
            before = memory.hybrid.search_embeddings(scope(), "secret flower", embeddings, kind="episode")
            asset_id = next(iter(memory.store.assets(scope())))
            memory.store.append_observation(
                scope(), asset_id, "text", {"char_start": 0, "char_end": 0}, "", "test", None, 1.0, "external_content"
            )
            memory._refresh_indexes(scope())
            after = memory.hybrid.search_embeddings(scope(), "secret flower", embeddings, kind="episode")

            self.assertEqual(before, after)

    def test_fts_and_lexical_retrieval_share_meaningful_query_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Citefold(tmp, clock=fixed_clock)
            memory.ingest_text(scope(), "I bake sourdough on Sundays.", source="text_chat")
            memory.ingest_text(
                MemoryScope("tenant-a", "user-1", "personal", "recall-agent", "session-2"),
                "Did did did: this note is only stopword noise.",
                source="text_chat",
            )

            with patch.object(memory.hybrid, "search_fts", wraps=memory.hybrid.search_fts) as search:
                pack = memory.recall(scope(), "What did I bake?")

            self.assertIn("sourdough", pack.markdown)
            self.assertEqual(["bake"], search.call_args.kwargs["query_terms"])


if __name__ == "__main__":
    unittest.main()
