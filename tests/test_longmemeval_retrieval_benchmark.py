import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.longmemeval_retrieval_benchmark import (
    bm25_rank,
    format_markdown,
    load_longmemeval,
    run_retrieval_benchmark,
)


def tiny_longmemeval_item() -> dict:
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
                {
                    "role": "user",
                    "content": "Can you recommend a workout plan for increasing step count?",
                },
                {
                    "role": "assistant",
                    "content": "A brisk walking routine is a good start.",
                },
            ],
            [
                {
                    "role": "user",
                    "content": "I graduated with a Business Administration degree and now work in finance.",
                },
                {
                    "role": "assistant",
                    "content": "I'll remember your Business Administration background.",
                },
            ],
        ],
    }


class LongMemEvalRetrievalBenchmarkTest(unittest.TestCase):
    def test_load_longmemeval_accepts_json_list_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "longmemeval.json"
            path.write_text(json.dumps([tiny_longmemeval_item(), tiny_longmemeval_item()]), encoding="utf-8")

            loaded = load_longmemeval(path, limit=1)

        self.assertEqual(1, len(loaded))
        self.assertEqual("q1", loaded[0]["question_id"])

    def test_bm25_ranks_answer_session_first_on_tiny_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "longmemeval.json"
            path.write_text(json.dumps([tiny_longmemeval_item()]), encoding="utf-8")

            result = run_retrieval_benchmark(path, k_values=[1, 5])

        self.assertEqual(1.0, result["overall"]["recall_any"]["1"])
        self.assertEqual(1.0, result["overall"]["recall_all"]["1"])
        self.assertEqual(1.0, result["overall"]["mrr"])
        self.assertEqual(1.0, result["overall"]["ndcg_any"]["1"])
        self.assertEqual(64, len(result["dataset"]["sha256"]))

    def test_markdown_report_describes_retrieval_only_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "longmemeval.json"
            path.write_text(json.dumps([tiny_longmemeval_item()]), encoding="utf-8")
            result = run_retrieval_benchmark(path, k_values=[1, 5])

        markdown = format_markdown(result)

        self.assertIn("# LongMemEval-S Cleaned Retrieval Benchmark Report", markdown)
        self.assertIn("retrieval-stage only", markdown)
        self.assertIn("recall_any@1", markdown)
        self.assertIn("ndcg_any@1", markdown)

    def test_empty_query_keeps_input_order_with_zero_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "longmemeval.json"
            path.write_text(json.dumps([tiny_longmemeval_item()]), encoding="utf-8")
            item = load_longmemeval(path)[0]
            result = run_retrieval_benchmark(path, k_values=[1], include_dates=False)

        self.assertEqual(1, result["overall"]["questions"])
        self.assertGreaterEqual(len(item["haystack_sessions"]), 2)

    def test_retrieval_excludes_abstention_questions_by_default(self) -> None:
        answerable = tiny_longmemeval_item()
        abstention = dict(tiny_longmemeval_item())
        abstention["question_id"] = "q2_abs"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "longmemeval.json"
            path.write_text(json.dumps([answerable, abstention]), encoding="utf-8")

            result = run_retrieval_benchmark(path, k_values=[1])

        self.assertEqual(1, result["overall"]["questions"])
        self.assertEqual(1, result["dataset"]["excluded_abstention_questions"])


if __name__ == "__main__":
    unittest.main()
