import json
import tempfile
import unittest
from pathlib import Path

from citefold import __version__
from benchmarks.longmemeval_citefold_benchmark import (
    format_markdown,
    run_benchmark,
)
from tests.test_longmemeval_citefold_adapter import tiny_item


class LongMemEvalCitefoldBenchmarkTest(unittest.TestCase):
    def test_runs_public_retrieval_diagnostic_without_answer_leakage(self) -> None:
        answerable = tiny_item()
        abstention = dict(tiny_item())
        abstention["question_id"] = "q2_abs"
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = Path(tmp) / "dataset.json"
            dataset_path.write_text(json.dumps([answerable, abstention]), encoding="utf-8")

            result = run_benchmark(dataset_path, k_values=[1, 5], verify_manifest=False)

        self.assertEqual("citefold", result["system"])
        self.assertEqual(__version__, result["system_version"])
        self.assertEqual(1, result["overall"]["questions"])
        self.assertEqual("dataset.json", result["dataset"]["path"])
        self.assertEqual(1, result["dataset"]["excluded_abstention_questions"])
        self.assertIn("recall_all", result["overall"])
        self.assertIn("ndcg_any", result["overall"])
        self.assertNotIn("answer", result["rows"][0]["trace"])

        markdown = format_markdown(result)
        self.assertIn("Public Retrieval Diagnostic", markdown)
        self.assertIn("not the end-to-end QA score", markdown)


if __name__ == "__main__":
    unittest.main()
