import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from benchmarks.officelife_track_b_benchmark import (
    DATASET_SCHEMA_VERSION,
    EVALUATION_SCHEMA_VERSION,
    EXECUTION_PROFILE_VERSION,
    MANIFEST_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    SCENARIO_FAMILIES,
    SURFACES,
    evaluate_computed_gates,
    format_summary_markdown,
    parse_args,
    preflight_dataset,
    resolve_arm_success,
    summarize_evaluation,
)


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def build_dataset(*, qualifying: bool) -> dict:
    user_count = 30 if qualifying else 1
    event_count = 50 if qualifying else 2
    task_count = 10 if qualifying else 2
    surfaces = sorted(SURFACES)
    families = sorted(SCENARIO_FAMILIES)
    users = []
    for user_index in range(user_count):
        user_id = f"user-{user_index:02d}"
        events = []
        for event_index in range(event_count):
            if event_index == event_count - 1:
                occurred_at = BASE_TIME + timedelta(days=14)
            else:
                occurred_at = BASE_TIME + timedelta(hours=event_index)
            events.append(
                {
                    "event_id": f"ev-{user_index:02d}-{event_index:03d}",
                    "occurred_at": iso(occurred_at),
                    "available_at": iso(occurred_at),
                    "surface": surfaces[event_index % len(surfaces)],
                    "memory_bearing": True,
                    "finalized": True,
                    "asr_final": True,
                    "raw_content": f"PRIVATE-HISTORY-{user_index}-{event_index}",
                }
            )
        tasks = []
        for task_index in range(task_count):
            family = families[task_index % len(families)]
            relevant = [] if family in {"no_evidence", "deletion"} else [events[0]["event_id"]]
            superseded = [events[1]["event_id"]] if family == "stale_or_superseded" else []
            deleted = [events[-1]["event_id"]] if family == "deletion" else []
            tasks.append(
                {
                    "task_id": f"task-{user_index:02d}-{task_index:02d}",
                    "task_at": iso(BASE_TIME + timedelta(days=15, minutes=task_index)),
                    "history_cutoff": iso(BASE_TIME + timedelta(days=15, minutes=task_index)),
                    "surface": surfaces[task_index % len(surfaces)],
                    "scenario_family": family,
                    "memory_requirement": "absent" if family == "no_evidence" else "required",
                    "scoring_mode": "deterministic" if task_index % 2 == 0 else "human",
                    "relevant_event_ids": relevant,
                    "superseded_event_ids": superseded,
                    "deleted_event_ids": deleted,
                    "allowed_scope": {
                        "tenant_id": "tenant-a",
                        "user_id": user_id,
                        "namespace": "personal",
                    },
                    "raw_prompt": f"PRIVATE-PROMPT-{user_index}-{task_index}",
                }
            )
        users.append(
            {
                "user_id": user_id,
                "split": "hidden_test",
                "events": events,
                "tasks": tasks,
            }
        )
    return {"schema_version": DATASET_SCHEMA_VERSION, "users": users}


def write_bundle(
    root: Path,
    dataset: dict,
    *,
    release_eligible: bool,
    attestations: bool = True,
) -> tuple[Path, Path]:
    dataset_path = root / "dataset.json"
    manifest_path = root / "manifest.json"
    dataset_path.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_sha256": digest,
        "protocol_version": PROTOCOL_VERSION,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "revision": "sealed-test-revision",
        "release_eligible": release_eligible,
        "attestations": {
            "consent": attestations,
            "deidentified": attestations,
            "prohibited_identifiers_removed": attestations,
            "independent_hidden_test": attestations,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return dataset_path, manifest_path


def arm_result(scoring_mode: str, success: bool, *, memory_pack: bool) -> dict:
    result = {
        "completed": True,
        "efficiency": {
            "ingest_latency_ms": 1.0,
            "recall_latency_ms": 20.0 if memory_pack else 0.0,
            "context_chars": 500.0 if memory_pack else 0.0,
            "provider_tokens": 100.0,
            "reader_cost": 0.001,
            "total_cost": 0.0015,
        },
    }
    if scoring_mode == "deterministic":
        result["deterministic_pass"] = success
    else:
        result["rater_labels"] = [success, success]
    if memory_pack:
        result["safety"] = {
            "unsupported_memory": False,
            "stale_or_superseded": False,
            "cross_scope_leakage_count": 0,
            "deletion_violation_count": 0,
            "partial_asr_durable_commit_count": 0,
        }
    return result


def build_evaluation(dataset: dict, dataset_path: Path) -> dict:
    task_results = []
    for user in dataset["users"]:
        for task in user["tasks"]:
            no_memory_success = task["scenario_family"] == "no_evidence"
            memory_pack_success = True
            no_memory = arm_result(task["scoring_mode"], no_memory_success, memory_pack=False)
            memory_pack = arm_result(task["scoring_mode"], memory_pack_success, memory_pack=True)
            memory_pack["cited_event_ids"] = list(task["relevant_event_ids"])
            task_results.append(
                {
                    "task_id": task["task_id"],
                    "user_id": user["user_id"],
                    "arms": {
                        "no_memory": no_memory,
                        "memory_pack": memory_pack,
                    },
                    "raw_answer": "PRIVATE-ANSWER-SHOULD-NOT-LEAK",
                }
            )
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "split": "hidden_test",
        "run_metadata": {
            "run_id": "run-test-001",
            "code_commit": "abcdef1",
            "citefold_version": "0.2.0-test",
            "agent_turn_contract": "agent-turn-v1",
            "model": "provider/pinned-model",
            "actual_model": "provider/pinned-model-2026-08-04",
            "provider": "test-provider",
            "prompt_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "randomization_seed": 17,
            "fixed_retry_count": 2,
            "blinded": True,
            "prompt": "PRIVATE-PROMPT-SHOULD-NOT-LEAK",
            "output_path": "/Users/example/private/run.json",
        },
        "latency_profile": {
            "stored_events": 1000,
            "processes": 1,
            "threads": 1,
            "concurrency": 1,
            "network_calls": 0,
            "model_calls": 0,
            "warmup_iterations": 100,
            "measured_iterations": 1000,
            "fixture_sha256": "c" * 64,
            "query_sha256": "d" * 64,
            "recall_latency_ms": [300.0] * 950 + [301.0] * 50,
        },
        "task_results": task_results,
    }


def write_evaluation(root: Path, evaluation: dict) -> Path:
    path = root / "evaluation.json"
    path.write_text(json.dumps(evaluation, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


class OfficeLifeTrackBPreflightTest(unittest.TestCase):
    def test_non_release_smoke_passes_validation_but_not_minimum_dataset_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_path, manifest_path = write_bundle(
                root,
                build_dataset(qualifying=False),
                release_eligible=False,
            )

            report = preflight_dataset(dataset_path, manifest_path)
            strict = preflight_dataset(dataset_path, manifest_path, strict=True)

        self.assertTrue(report["validation"]["passed"])
        self.assertTrue(report["passed"])
        self.assertTrue(report["private"])
        self.assertFalse(report["minimum_dataset_gates"]["passed"])
        self.assertFalse(strict["passed"])
        self.assertEqual(14.0, report["counts"]["per_user"]["user-00"]["history_span_days"])

    def test_exact_minimum_dataset_boundaries_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_path, manifest_path = write_bundle(
                root,
                build_dataset(qualifying=True),
                release_eligible=True,
            )

            report = preflight_dataset(dataset_path, manifest_path, strict=True)

        self.assertTrue(report["passed"], report["validation"]["errors"])
        self.assertEqual(30, report["counts"]["users"])
        self.assertEqual(300, report["counts"]["tasks"])
        self.assertTrue(all(value >= 50 for value in report["counts"]["surfaces"].values()))
        self.assertTrue(all(value > 0 for value in report["counts"]["scenario_families"].values()))

    def test_manifest_must_bind_the_frozen_protocol_and_execution_profile(self) -> None:
        for field_name in ("protocol_version", "execution_profile_version"):
            with self.subTest(field_name=field_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    dataset_path, manifest_path = write_bundle(
                        root,
                        build_dataset(qualifying=True),
                        release_eligible=True,
                    )
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest[field_name] = "banana"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    report = preflight_dataset(dataset_path, manifest_path, strict=True)

                self.assertFalse(report["validation"]["passed"])
                self.assertFalse(report["passed"])
                self.assertTrue(
                    any(field_name in error for error in report["validation"]["errors"])
                )

    def test_realtime_voice_requires_explicit_final_asr_state(self) -> None:
        dataset = build_dataset(qualifying=True)
        voice_event = next(
            event
            for event in dataset["users"][0]["events"]
            if event["surface"] == "realtime_voice"
        )
        voice_event["asr_final"] = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=True)
            report = preflight_dataset(dataset_path, manifest_path, strict=True)

        self.assertFalse(report["validation"]["passed"])
        self.assertFalse(report["passed"])
        self.assertTrue(
            any("true or false for realtime_voice" in error for error in report["validation"]["errors"])
        )

    def test_elapsed_history_span_must_reach_fourteen_full_days(self) -> None:
        dataset = build_dataset(qualifying=True)
        dataset["users"][0]["events"][-1]["occurred_at"] = iso(
            BASE_TIME + timedelta(days=14) - timedelta(seconds=1)
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=True)
            report = preflight_dataset(dataset_path, manifest_path, strict=True)

        self.assertFalse(
            report["minimum_dataset_gates"]["checks"]["per_user_history_span"]["passed"]
        )

    def test_hash_future_reference_and_cross_split_user_are_rejected(self) -> None:
        with self.subTest("hash"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset = build_dataset(qualifying=False)
                dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=False)
                dataset_path.write_text(dataset_path.read_text() + " ", encoding="utf-8")
                report = preflight_dataset(dataset_path, manifest_path)
            self.assertFalse(report["validation"]["passed"])
            self.assertTrue(any("does not match" in error for error in report["validation"]["errors"]))

        with self.subTest("future-reference"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset = build_dataset(qualifying=False)
                dataset["users"][0]["events"][0]["occurred_at"] = iso(BASE_TIME + timedelta(days=16))
                dataset["users"][0]["events"][0]["available_at"] = iso(BASE_TIME + timedelta(days=16))
                dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=False)
                report = preflight_dataset(dataset_path, manifest_path)
            self.assertFalse(report["validation"]["passed"])
            self.assertTrue(any("outside eligible history" in error for error in report["validation"]["errors"]))

        with self.subTest("cross-split"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset = build_dataset(qualifying=False)
                duplicate = copy.deepcopy(dataset["users"][0])
                duplicate["split"] = "development"
                dataset["users"].append(duplicate)
                dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=False)
                report = preflight_dataset(dataset_path, manifest_path)
            self.assertFalse(report["validation"]["passed"])
            self.assertTrue(any("appears in both" in error for error in report["validation"]["errors"]))

        with self.subTest("allowed-scope-user"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset = build_dataset(qualifying=False)
                dataset["users"][0]["tasks"][0]["allowed_scope"]["user_id"] = "different-user"
                dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=False)
                report = preflight_dataset(dataset_path, manifest_path)
            self.assertFalse(report["validation"]["passed"])
            self.assertTrue(any("must match" in error for error in report["validation"]["errors"]))

    def test_future_and_non_memory_events_cannot_satisfy_history_gates(self) -> None:
        dataset = build_dataset(qualifying=False)
        user = dataset["users"][0]
        user["events"].append(
            {
                "event_id": "ev-partial",
                "occurred_at": iso(BASE_TIME + timedelta(days=1)),
                "available_at": iso(BASE_TIME + timedelta(days=1)),
                "surface": "realtime_voice",
                "memory_bearing": True,
                "finalized": True,
                "asr_final": False,
            }
        )
        user["events"].append(
            {
                "event_id": "ev-unfinalized",
                "occurred_at": iso(BASE_TIME + timedelta(days=2)),
                "available_at": iso(BASE_TIME + timedelta(days=2)),
                "surface": "text_chat",
                "memory_bearing": True,
                "finalized": False,
                "asr_final": None,
            }
        )
        for index in range(60):
            user["events"].append(
                {
                    "event_id": f"ev-future-{index:02d}",
                    "occurred_at": iso(BASE_TIME + timedelta(days=1, minutes=index)),
                    "available_at": iso(BASE_TIME + timedelta(days=30, minutes=index)),
                    "surface": "text_chat",
                    "memory_bearing": True,
                    "finalized": True,
                    "asr_final": None,
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=False)
            report = preflight_dataset(dataset_path, manifest_path)

        self.assertTrue(report["validation"]["passed"])
        self.assertEqual(2, report["counts"]["per_user"]["user-00"]["memory_bearing_events"])


class OfficeLifeTrackBScoringTest(unittest.TestCase):
    def test_human_rater_resolution_requires_third_rater_only_for_disagreement(self) -> None:
        self.assertTrue(resolve_arm_success({"completed": True, "rater_labels": [True, True]}, "human"))
        self.assertFalse(
            resolve_arm_success(
                {"completed": True, "rater_labels": [True, False, False]},
                "human",
            )
        )
        with self.assertRaisesRegex(ValueError, "third rater is required"):
            resolve_arm_success({"completed": True, "rater_labels": [True, False]}, "human")
        with self.assertRaisesRegex(ValueError, "only valid when"):
            resolve_arm_success(
                {"completed": True, "rater_labels": [True, True, False]},
                "human",
            )
        self.assertIsNone(resolve_arm_success({"completed": False, "success": False}, "human"))
        with self.assertRaisesRegex(ValueError, "require two or three"):
            resolve_arm_success({"completed": True, "success": False}, "human")
        with self.assertRaisesRegex(ValueError, "require a boolean deterministic_pass"):
            resolve_arm_success({"completed": True, "success": False}, "deterministic")
        self.assertFalse(
            resolve_arm_success(
                {
                    "completed": True,
                    "rater_labels": [True, True],
                    "deterministic_pass": False,
                    "success": False,
                },
                "human",
            )
        )

    def test_complete_calculation_is_deterministic_and_nonclaimable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = build_dataset(qualifying=True)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=True)
            evaluation_path = write_evaluation(root, build_evaluation(dataset, dataset_path))

            first = summarize_evaluation(
                dataset_path,
                manifest_path,
                evaluation_path,
                bootstrap_samples=100,
                bootstrap_seed=71,
            )
            second = summarize_evaluation(
                dataset_path,
                manifest_path,
                evaluation_path,
                bootstrap_samples=100,
                bootstrap_seed=71,
            )
            frozen = summarize_evaluation(
                dataset_path,
                manifest_path,
                evaluation_path,
            )

        self.assertTrue(first["coverage"]["infrastructure_complete"])
        self.assertEqual(300, first["coverage"]["scored_pairs"])
        self.assertEqual(
            first["metrics"]["clustered_bootstrap_ci95"],
            second["metrics"]["clustered_bootstrap_ci95"],
        )
        self.assertGreater(first["metrics"]["user_macro_mean_lift"], 0.0)
        self.assertFalse(first["metrics"]["citations"]["protocol_metric_available"])
        self.assertIsNone(first["metrics"]["citations"]["precision"])
        self.assertIsNone(first["metrics"]["citations"]["source_coverage"])
        self.assertEqual(
            1.0,
            first["metrics"]["citations"]["diagnostic"]["event_id_overlap_precision"],
        )
        self.assertEqual(
            1.0,
            first["metrics"]["citations"]["diagnostic"]["relevant_event_id_coverage"],
        )
        self.assertEqual("nonclaimable-diagnostic-summary", first["artifact_scope"])
        self.assertTrue(first["private_input_required"])
        self.assertEqual(PROTOCOL_VERSION, first["protocol_version"])
        self.assertEqual(EXECUTION_PROFILE_VERSION, first["execution_profile_version"])
        self.assertEqual(300.0, first["latency_profile"]["p95_ms"])
        self.assertFalse(first["bootstrap"]["frozen_profile"])
        self.assertEqual("diagnostic", first["bootstrap"]["profile"])
        self.assertEqual("not_evaluable", first["computed_gates"]["status"])
        self.assertIsNone(first["computed_gates"]["all_passed"])
        self.assertTrue(frozen["bootstrap"]["frozen_profile"])
        self.assertEqual("passed", frozen["computed_gates"]["status"])
        self.assertTrue(frozen["computed_gates"]["all_passed"])
        self.assertFalse(frozen["qualification_complete"])
        self.assertEqual("incomplete", frozen["qualification_status"])
        self.assertFalse(frozen["claimable"])
        markdown = format_summary_markdown(frozen)
        self.assertNotIn("Claimable: true", markdown)
        self.assertIn("cannot complete Track B qualification", markdown)
        args = parse_args(["summarize", "dataset.json", "manifest.json", "evaluation.json"])
        self.assertFalse(args.enforce_computed_gates)
        self.assertFalse(hasattr(args, "enforce_release_gates"))
        enforced = parse_args(
            [
                "summarize",
                "dataset.json",
                "manifest.json",
                "evaluation.json",
                "--enforce-computed-gates",
            ]
        )
        self.assertTrue(enforced.enforce_computed_gates)
        self.assertEqual(300, first["metrics"]["rater_agreement"]["overall"]["primary_pairs"])
        self.assertEqual(1.0, first["metrics"]["rater_agreement"]["overall"]["agreement_rate"])
        self.assertEqual(
            "custodian-supplied adjudication",
            first["metrics"]["rater_agreement"]["source"],
        )
        self.assertEqual(SCENARIO_FAMILIES, set(first["slices"]["by_scenario_family"]))
        self.assertEqual(SURFACES, set(first["slices"]["by_surface"]))
        self.assertNotIn("by_user_cluster", first["slices"])
        self.assertEqual(30, first["user_distribution"]["user_count"])
        self.assertEqual(30, first["user_distribution"]["task_count"]["count"])
        self.assertTrue(any("not implemented" in caveat for caveat in first["caveats"]))
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("PRIVATE-HISTORY", serialized)
        self.assertNotIn("PRIVATE-PROMPT", serialized)
        self.assertNotIn("PRIVATE-ANSWER", serialized)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("user-00", serialized)
        self.assertNotIn("task-00", serialized)

    def test_completed_false_is_excluded_and_makes_gates_not_evaluable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = build_dataset(qualifying=True)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=True)
            evaluation = build_evaluation(dataset, dataset_path)
            evaluation["task_results"][0]["arms"]["memory_pack"] = {
                "completed": False,
                "success": False,
            }
            evaluation_path = write_evaluation(root, evaluation)
            summary = summarize_evaluation(
                dataset_path,
                manifest_path,
                evaluation_path,
            )

        self.assertFalse(summary["coverage"]["infrastructure_complete"])
        self.assertEqual(299, summary["coverage"]["scored_pairs"])
        self.assertEqual(299, summary["metrics"]["overall"]["count"])
        self.assertEqual("not_evaluable", summary["computed_gates"]["status"])
        self.assertIsNone(summary["computed_gates"]["all_passed"])
        self.assertEqual(
            "not_evaluable",
            summary["computed_gates"]["checks"]["infrastructure_complete"]["status"],
        )
        for name, gate in summary["computed_gates"]["checks"].items():
            if name != "minimum_dataset_gates":
                self.assertNotEqual("failed", gate["status"], name)

    def test_completed_product_timeout_is_scored_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = build_dataset(qualifying=True)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=True)
            evaluation = build_evaluation(dataset, dataset_path)
            timeout_arm = evaluation["task_results"][0]["arms"]["memory_pack"]
            timeout_arm["deterministic_pass"] = False
            timeout_arm["success"] = False
            evaluation_path = write_evaluation(root, evaluation)
            summary = summarize_evaluation(
                dataset_path,
                manifest_path,
                evaluation_path,
            )

        self.assertTrue(summary["coverage"]["infrastructure_complete"])
        self.assertEqual(300, summary["coverage"]["scored_pairs"])
        self.assertLess(summary["metrics"]["overall"]["memory_pack_task_success_rate"], 1.0)
        self.assertEqual("passed", summary["computed_gates"]["status"])

    def test_missing_task_or_arm_makes_coverage_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = build_dataset(qualifying=True)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=True)
            evaluation = build_evaluation(dataset, dataset_path)
            evaluation["task_results"].pop()
            evaluation["task_results"][0]["arms"].pop("no_memory")
            evaluation_path = write_evaluation(root, evaluation)
            summary = summarize_evaluation(
                dataset_path,
                manifest_path,
                evaluation_path,
            )

        self.assertFalse(summary["coverage"]["infrastructure_complete"])
        self.assertEqual(1, summary["coverage"]["missing_task_count"])
        self.assertGreaterEqual(summary["coverage"]["evaluation_error_count"], 1)
        self.assertEqual("not_evaluable", summary["computed_gates"]["status"])
        self.assertIsNone(summary["computed_gates"]["all_passed"])

    def test_missing_run_controls_and_short_latency_make_gates_not_evaluable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = build_dataset(qualifying=True)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=True)
            evaluation = build_evaluation(dataset, dataset_path)
            evaluation["run_metadata"].pop("actual_model")
            evaluation["latency_profile"]["recall_latency_ms"] = [10.0]
            evaluation["latency_profile"]["measured_iterations"] = 1
            evaluation_path = write_evaluation(root, evaluation)
            summary = summarize_evaluation(
                dataset_path,
                manifest_path,
                evaluation_path,
            )

        self.assertFalse(summary["coverage"]["run_metadata_complete"])
        self.assertFalse(summary["coverage"]["infrastructure_complete"])
        self.assertFalse(summary["latency_profile"]["gate_passed"])
        self.assertEqual(1, summary["latency_profile"]["sample_count"])
        self.assertEqual("not_evaluable", summary["computed_gates"]["status"])
        self.assertIsNone(summary["computed_gates"]["all_passed"])

    def test_computed_gate_boundaries_are_frozen(self) -> None:
        metrics = {
            "overall": {"task_success_lift": 0.10},
            "memory_required": {"task_success_lift": 0.15},
            "no_evidence": {"task_success_lift": -0.01},
            "clustered_bootstrap_ci95": [0.000001, 0.2],
            "safety": {
                "unsupported_memory_rate": 0.02,
                "stale_or_superseded_rate": 0.02,
                "cross_scope_leakage_count": 0,
                "deletion_violation_count": 0,
                "partial_asr_durable_commit_count": 0,
            },
        }
        latency = {"complete": True, "gate_passed": True, "p95_ms": 300.0}

        passed = evaluate_computed_gates(
            metrics,
            preflight_passed=True,
            infrastructure_complete=True,
            bootstrap_profile_frozen=True,
            latency_profile=latency,
        )
        self.assertTrue(passed["all_passed"])
        self.assertEqual("passed", passed["status"])

        metrics["clustered_bootstrap_ci95"] = [0.0, 0.2]
        failed = evaluate_computed_gates(
            metrics,
            preflight_passed=True,
            infrastructure_complete=True,
            bootstrap_profile_frozen=True,
            latency_profile=latency,
        )
        self.assertFalse(failed["checks"]["clustered_ci_lower"]["passed"])
        self.assertEqual("failed", failed["status"])

        metrics["clustered_bootstrap_ci95"] = [0.01, 0.2]
        metrics["safety"]["deletion_violation_count"] = 1
        failed = evaluate_computed_gates(
            metrics,
            preflight_passed=True,
            infrastructure_complete=True,
            bootstrap_profile_frozen=True,
            latency_profile=latency,
        )
        self.assertFalse(failed["checks"]["deletion_violation_count"]["passed"])

        diagnostic = evaluate_computed_gates(
            metrics,
            preflight_passed=True,
            infrastructure_complete=True,
            bootstrap_profile_frozen=False,
            latency_profile=latency,
        )
        self.assertEqual("not_evaluable", diagnostic["status"])
        self.assertIsNone(diagnostic["all_passed"])
        self.assertTrue(
            all(
                gate["status"] != "failed"
                for name, gate in diagnostic["checks"].items()
                if name != "minimum_dataset_gates"
            )
        )

        incomplete_latency = evaluate_computed_gates(
            metrics,
            preflight_passed=True,
            infrastructure_complete=True,
            bootstrap_profile_frozen=True,
            latency_profile={"complete": False, "gate_passed": False, "p95_ms": None},
        )
        self.assertEqual("not_evaluable", incomplete_latency["status"])
        self.assertEqual(
            "not_evaluable",
            incomplete_latency["checks"]["latency_profile_complete"]["status"],
        )

    def test_no_evidence_slice_uses_memory_requirement_not_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = build_dataset(qualifying=True)
            task = dataset["users"][0]["tasks"][0]
            self.assertNotEqual("no_evidence", task["scenario_family"])
            task["memory_requirement"] = "absent"
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=True)
            evaluation_path = write_evaluation(root, build_evaluation(dataset, dataset_path))
            summary = summarize_evaluation(
                dataset_path,
                manifest_path,
                evaluation_path,
                bootstrap_samples=20,
            )

        self.assertEqual(31, summary["metrics"]["no_evidence"]["count"])

    def test_public_identifiers_reject_paths_and_control_characters(self) -> None:
        with self.subTest("manifest-revision"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_path, manifest_path = write_bundle(
                    root,
                    build_dataset(qualifying=False),
                    release_eligible=False,
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["revision"] = "/Users/private/revision"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                report = preflight_dataset(dataset_path, manifest_path)
            self.assertFalse(report["validation"]["passed"])

        with self.subTest("run-id"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset = build_dataset(qualifying=False)
                dataset_path, manifest_path = write_bundle(
                    root,
                    dataset,
                    release_eligible=False,
                )
                evaluation = build_evaluation(dataset, dataset_path)
                evaluation["run_metadata"]["run_id"] = "run\nprivate"
                evaluation_path = write_evaluation(root, evaluation)
                summary = summarize_evaluation(
                    dataset_path,
                    manifest_path,
                    evaluation_path,
                    bootstrap_samples=2,
                )
            self.assertFalse(summary["coverage"]["run_metadata_complete"])
            self.assertNotIn("run_id", summary["run_metadata"])

        with self.subTest("model-path"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset = build_dataset(qualifying=False)
                dataset_path, manifest_path = write_bundle(
                    root,
                    dataset,
                    release_eligible=False,
                )
                evaluation = build_evaluation(dataset, dataset_path)
                evaluation["run_metadata"]["actual_model"] = "../../private/model"
                evaluation_path = write_evaluation(root, evaluation)
                summary = summarize_evaluation(
                    dataset_path,
                    manifest_path,
                    evaluation_path,
                    bootstrap_samples=2,
                )
            self.assertFalse(summary["coverage"]["run_metadata_complete"])
            self.assertNotIn("actual_model", summary["run_metadata"])

        for unsafe_model in (
            "contact alice@example.com",
            "sk-proj-secret",
            "Volumes/Secret/model",
        ):
            with self.subTest(unsafe_model=unsafe_model):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    dataset = build_dataset(qualifying=False)
                    dataset_path, manifest_path = write_bundle(
                        root,
                        dataset,
                        release_eligible=False,
                    )
                    evaluation = build_evaluation(dataset, dataset_path)
                    evaluation["run_metadata"]["actual_model"] = unsafe_model
                    evaluation_path = write_evaluation(root, evaluation)
                    summary = summarize_evaluation(
                        dataset_path,
                        manifest_path,
                        evaluation_path,
                        bootstrap_samples=2,
                    )

                self.assertFalse(summary["coverage"]["run_metadata_complete"])
                self.assertNotIn("actual_model", summary["run_metadata"])

    def test_evaluation_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = build_dataset(qualifying=False)
            dataset_path, manifest_path = write_bundle(root, dataset, release_eligible=False)
            evaluation = build_evaluation(dataset, dataset_path)
            evaluation["dataset_sha256"] = "0" * 64
            evaluation_path = write_evaluation(root, evaluation)
            with self.assertRaisesRegex(ValueError, "does not match"):
                summarize_evaluation(
                    dataset_path,
                    manifest_path,
                    evaluation_path,
                    bootstrap_samples=2,
                )


if __name__ == "__main__":
    unittest.main()
