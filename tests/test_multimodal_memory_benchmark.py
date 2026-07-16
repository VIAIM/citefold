import tempfile
import unittest
from pathlib import Path

from benchmarks.multimodal_memory_benchmark import (
    BenchmarkConfig,
    EXPECTED_CATEGORIES,
    format_markdown,
    load_fixture,
    run_benchmark,
    write_outputs,
)


class MultimodalMemoryBenchmarkTest(unittest.TestCase):
    def test_fixture_has_exactly_the_ten_required_categories(self) -> None:
        fixture = load_fixture()

        self.assertEqual(10, len(fixture["cases"]))
        self.assertEqual(EXPECTED_CATEGORIES, {case["category"] for case in fixture["cases"]})
        self.assertEqual(10, len({case["case_id"] for case in fixture["cases"]}))

    def test_memory_pack_improves_task_success_without_safety_regressions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_benchmark(BenchmarkConfig(root=Path(tmp) / "memory-root"))

        self.assertEqual("multimodal_memory_pack_v1", result["benchmark"])
        self.assertEqual(10, result["fixture"]["case_count"])
        self.assertEqual(0, result["parameters"]["network_calls"])
        self.assertEqual(0, result["parameters"]["model_calls"])
        self.assertGreater(result["memory_lift"]["task_success_rate"], 0.0)
        self.assertGreater(
            result["baselines"]["memory_pack"]["task_success_rate"],
            result["baselines"]["no_memory"]["task_success_rate"],
        )
        self.assertEqual(0, result["quality"]["unsupported_count"])
        self.assertEqual(0, result["quality"]["forbidden_count"])
        self.assertEqual(0, result["quality"]["scope_leakage_count"])
        self.assertEqual(0, result["quality"]["runtime_assertion_failure_count"])

    def test_case_logs_prove_multimodal_safety_and_lifecycle_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_benchmark(BenchmarkConfig(root=Path(tmp) / "memory-root"))

        logs = {item["case_id"]: item for item in result["probe_logs"]}
        self.assertEqual(["image"], logs["image_text"]["memory_pack_summary"]["citation_modalities"])
        self.assertEqual(
            ["audio", "video_frame"],
            logs["video_audio_visual"]["memory_pack_summary"]["citation_modalities"],
        )
        self.assertEqual(0, logs["low_confidence_asr"]["memory_pack_summary"]["active_record_count"])
        self.assertEqual([], logs["preference_correction"]["memory_pack"]["forbidden"])
        self.assertGreaterEqual(logs["unresolved_conflict"]["memory_pack_summary"]["conflict_count"], 1)
        self.assertEqual("none", logs["no_evidence"]["memory_pack_summary"]["coverage"])
        self.assertEqual(1, logs["media_prompt_injection"]["memory_pack_summary"]["sources_heading_count"])
        self.assertTrue(logs["deletion_cascade"]["memory_pack_summary"]["hard_delete_cascade_passed"])
        self.assertEqual(0, logs["deletion_cascade"]["memory_pack_summary"]["citation_count"])

    def test_report_writer_emits_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_benchmark(BenchmarkConfig(root=root / "memory-root"))
            json_path = root / "result.json"
            markdown_path = root / "result.md"

            write_outputs(result, json_path, markdown_path)

            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertTrue(json_path.is_file())
            self.assertEqual(format_markdown(result), markdown)
            self.assertIn("# Multimodal MemoryPack vs No Memory Benchmark", markdown)
            self.assertIn("| video_audio_visual |", markdown)
            self.assertIn("Memory lift", markdown)


if __name__ == "__main__":
    unittest.main()
