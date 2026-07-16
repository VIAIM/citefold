import tempfile
import unittest
from pathlib import Path

from benchmarks.citefold_benchmark import (
    BenchmarkConfig,
    format_markdown,
    run_benchmark,
)


class CitefoldBenchmarkTest(unittest.TestCase):
    def test_benchmark_reports_timings_quality_and_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_benchmark(
                BenchmarkConfig(
                    root=Path(tmp) / "memory-root",
                    tenants=1,
                    users_per_tenant=2,
                    events_per_user=2,
                    candidates_per_user=1,
                    runs=1,
                    isolation_samples=2,
                )
            )

        self.assertEqual("citefold_phase1", result["benchmark"])
        self.assertEqual(2, result["totals"]["scopes"])
        self.assertEqual(4, result["totals"]["ingest_chat_calls"])
        self.assertIn("ingest_chat", result["timings_ms"])
        self.assertIn("recall_text", result["timings_ms"])
        self.assertGreater(result["footprint"]["files_avg"], 0)
        self.assertEqual(0, result["quality"]["missing_expected_marker_count"])
        self.assertEqual(0, result["quality"]["isolation_violation_count"])

    def test_markdown_report_includes_methodology_caveat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_benchmark(
                BenchmarkConfig(
                    root=Path(tmp) / "memory-root",
                    tenants=1,
                    users_per_tenant=1,
                    events_per_user=1,
                    candidates_per_user=1,
                    runs=1,
                    isolation_samples=1,
                )
            )

        markdown = format_markdown(result)

        self.assertIn("# Citefold Phase 1 Benchmark Report", markdown)
        self.assertIn("Do not compare these numbers with LoCoMo", markdown)
        self.assertIn("| ingest_chat |", markdown)


if __name__ == "__main__":
    unittest.main()
