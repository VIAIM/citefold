import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.longmemeval_citefold_adapter import (
    DatasetVerificationError,
    build_citefold_context,
    verify_dataset,
)


def tiny_item() -> dict:
    return {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What degree did I graduate with?",
        "question_date": "2023/05/30 (Tue) 23:40",
        "answer": "Business Administration",
        "answer_session_ids": ["answer_session"],
        "haystack_dates": [
            "2023/05/20 (Sat) 02:21",
            "2023/05/21 (Sun) 03:24",
        ],
        "haystack_session_ids": [
            "distractor_session",
            "answer_session",
        ],
        "haystack_sessions": [
            [
                {"role": "user", "content": "Can you recommend a workout plan?"},
                {"role": "assistant", "content": "A brisk walking routine is a good start."},
            ],
            [
                {
                    "role": "user",
                    "content": "I graduated with a Business Administration degree.",
                    "has_answer": True,
                },
                {"role": "assistant", "content": "I'll remember your background."},
            ],
        ],
    }


class LongMemEvalCitefoldAdapterTest(unittest.TestCase):
    def test_verify_dataset_checks_hash_size_and_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = Path(tmp) / "dataset.json"
            manifest_path = Path(tmp) / "manifest.json"
            dataset_path.write_text(json.dumps([tiny_item()]), encoding="utf-8")
            import hashlib

            manifest_path.write_text(
                json.dumps(
                    {
                        "dataset": {
                            "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                            "bytes": dataset_path.stat().st_size,
                            "questions": 1,
                            "question_types": {"single-session-user": 1},
                            "abstention_questions": 0,
                        }
                    }
                ),
                encoding="utf-8",
            )

            identity = verify_dataset(dataset_path, manifest_path)

        self.assertEqual(1, identity["questions"])
        self.assertEqual("dataset.json", identity["path"])
        self.assertEqual("single-session-user", next(iter(identity["question_types"])))

    def test_verify_dataset_rejects_changed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = Path(tmp) / "dataset.json"
            manifest_path = Path(tmp) / "manifest.json"
            dataset_path.write_text(json.dumps([tiny_item()]), encoding="utf-8")
            manifest_path.write_text(
                json.dumps(
                    {
                        "dataset": {
                            "sha256": "0" * 64,
                            "bytes": dataset_path.stat().st_size,
                            "questions": 1,
                            "question_types": {"single-session-user": 1},
                            "abstention_questions": 0,
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(DatasetVerificationError):
                verify_dataset(dataset_path, manifest_path)

    def test_adapter_removes_gold_labels_and_returns_auditable_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build_citefold_context(
                item=tiny_item(),
                root=Path(tmp),
                token_budget=2200,
            )

            evidence_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in Path(tmp).rglob("*.jsonl")
            )

        self.assertNotIn("has_answer", result.context)
        self.assertNotIn("has_answer", evidence_text)
        self.assertEqual(2, result.trace["sessions_ingested"])
        self.assertEqual(4, result.trace["turns_ingested"])
        self.assertIn("selected_nodes", result.trace)
        self.assertIn("answer_session", result.trace["selected_session_ids"])
        self.assertIn("Business Administration", result.context)
        self.assertNotIn("answer", result.trace)
        self.assertNotIn("answer_session_ids", result.trace)


if __name__ == "__main__":
    unittest.main()
