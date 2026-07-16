import tempfile
import unittest
from pathlib import Path

from benchmarks.officelife_memory_benchmark import (
    BenchmarkConfig,
    Probe,
    format_markdown,
    run_benchmark,
    score_context,
)


class OfficeLifeMemoryBenchmarkTest(unittest.TestCase):
    def test_benchmark_reports_memory_lift_against_no_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_benchmark(
                BenchmarkConfig(
                    root=Path(tmp) / "memory-root",
                    tenants=1,
                    users_per_tenant=1,
                )
            )

        self.assertEqual("officelife_memory_bench", result["benchmark"])
        self.assertIn("no_memory", result["baselines"])
        self.assertIn("memory_pack", result["baselines"])
        self.assertGreater(result["memory_lift"]["task_success_rate"], 0)
        self.assertGreater(
            result["baselines"]["memory_pack"]["task_success_rate"],
            result["baselines"]["no_memory"]["task_success_rate"],
        )
        self.assertIn("preference_recall", result["by_category"])
        self.assertIn("no_evidence", result["by_category"])
        self.assertIn("scope_leakage_count", result["quality"])

    def test_score_context_tracks_expected_and_forbidden_markers(self) -> None:
        probe = Probe(
            probe_id="probe-1",
            category="no_evidence",
            query="我的护照号码是什么？",
            expected_markers=[],
            forbidden_markers=["private-marker"],
            requires_memory=False,
        )

        clean = score_context("", probe, selected_node_count=0)
        polluted = score_context("private-marker", probe, selected_node_count=1)

        self.assertTrue(clean["passed"])
        self.assertFalse(polluted["passed"])
        self.assertEqual(["private-marker"], polluted["forbidden_hits"])

    def test_markdown_report_includes_baseline_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_benchmark(
                BenchmarkConfig(
                    root=Path(tmp) / "memory-root",
                    tenants=1,
                    users_per_tenant=1,
                )
            )

        markdown = format_markdown(result)

        self.assertIn("# OfficeLifeMemoryBench Report", markdown)
        self.assertIn("| no_memory |", markdown)
        self.assertIn("| memory_pack |", markdown)
        self.assertIn("Memory Lift", markdown)


if __name__ == "__main__":
    unittest.main()
