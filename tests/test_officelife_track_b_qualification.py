from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from benchmarks.officelife_track_b_executor import (
    execute_worker_bundle,
    prepare_worker_bundle,
)
from benchmarks.officelife_track_b_qualification import (
    BOOTSTRAP_PRNG,
    BOOTSTRAP_QUANTILE,
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    DETERMINISTIC_ASSESSMENT_SCHEMA_VERSION,
    GATE_DEFINITIONS,
    PRIVATE_SUMMARY_SCHEMA_VERSION,
    PUBLIC_PROJECTOR_VERSION,
    QUALIFICATION_CONTRACT_VERSION,
    QUALIFICATION_PLAN_SCHEMA_VERSION,
    RATING_SUBMISSION_SCHEMA_VERSION,
    QualificationValidationError,
    ScoredArm,
    ScoredPair,
    canonical_json,
    build_private_summary,
    build_scored_arm_record,
    build_scored_pair_record,
    derive_arm_success,
    derive_citation_metrics,
    derive_safety_metrics,
    evaluate_release_gates,
    nearest_rank_percentile,
    pair_scored_arms,
    paired_user_cluster_bootstrap,
    parse_args,
    public_result_projection,
    resolve_human_judgment,
    sha256_bytes,
    summarize_pairs,
    type7_percentile,
    validate_public_bundle,
    validate_qualification_plan,
    validate_qualification_chain,
    validate_rating_bundle,
    validate_sealed_qualification_plan,
    validate_upstream_chain,
)
from tests.test_officelife_track_b_contract import (
    build_dataset_bundle,
    build_run_bundle,
    sha256,
    write_json,
)
from tests.test_officelife_track_b_executor import (
    RecordingHandler,
    add_executor_config,
)

PARSER_COMMITMENT = {
    "parser_id": "exact-parser",
    "parser_version": "v1",
    "parser_sha256": "0" * 64,
}


def arm_output(content: str = "blue", outcome: str = "answer") -> dict:
    return {
        "schema_version": "officelife-track-b-arm-output-v1",
        "blinded_output_id": "blind-01",
        "outcome": outcome,
        "content": content if outcome not in {"product_timeout", "product_failure"} else None,
    }


def task_label(*, human: bool = False) -> dict:
    fact_value = {
        "value_type": "string",
        "canonical": "blue",
        "alternatives": [],
    }
    return {
        "task_id": "task-01",
        "deterministic_checks": [
            {
                "check_id": "check-required",
                "type": "fact",
                "subject_kind": "acceptable_fact",
                "subject_ref": "fact-01",
                "operator": "present",
                "expected_values": [fact_value],
                "must_pass": True,
                "hard_prohibition": False,
            },
            {
                "check_id": "check-hard",
                "type": "fact",
                "subject_kind": "forbidden_item",
                "subject_ref": "forbidden-01",
                "operator": "absent",
                "expected_values": [
                    {
                        "value_type": "string",
                        "canonical": "red",
                        "alternatives": [],
                    }
                ],
                "must_pass": False,
                "hard_prohibition": True,
            },
        ],
        "human_judgment_required": human,
        "success_rule": {
            "required_check_ids": ["check-required"],
            "hard_prohibition_check_ids": ["check-hard"],
            "human_judgment_required": human,
        },
    }


def assessment(
    output: dict,
    label: dict,
    check_id: str,
    verdict: str,
    *,
    span: tuple[int, int] | None = None,
) -> dict:
    check = next(item for item in label["deterministic_checks"] if item["check_id"] == check_id)
    content = output.get("content") or ""
    spans = []
    if span is not None:
        start, end = span
        spans.append(
            {
                "start_char": start,
                "end_char": end,
                "matched_text_sha256": sha256_bytes(content[start:end].encode("utf-8")),
            }
        )
    return {
        "schema_version": DETERMINISTIC_ASSESSMENT_SCHEMA_VERSION,
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "assessment_id": f"assessment-{check_id}",
        "task_id": label["task_id"],
        "blinded_output_id": output["blinded_output_id"],
        "check_id": check_id,
        "output_sha256": sha256_bytes(canonical_json(output)),
        "label_sha256": sha256_bytes(canonical_json(label)),
        "check_sha256": sha256_bytes(canonical_json(check)),
        **PARSER_COMMITMENT,
        "verdict": verdict,
        "evidence_spans": spans,
        "evaluated_at": "2026-08-04T00:00:00Z",
    }


def rating(output: dict, rater_id: str, ordinal: int, verdict: str) -> dict:
    return {
        "schema_version": RATING_SUBMISSION_SCHEMA_VERSION,
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "submission_id": f"submission-{ordinal}",
        "assignment_id": f"assignment-{ordinal}",
        "rating_item_id": "rating-item-01",
        "blinded_output_id": output["blinded_output_id"],
        "output_sha256": sha256_bytes(canonical_json(output)),
        "rater_id": rater_id,
        "stage": "primary" if ordinal < 3 else "tiebreak",
        "ordinal": ordinal,
        "verdict": verdict,
        "annotation_codebook_sha256": "3" * 64,
        "submitted_at": "2026-08-04T00:00:00Z",
    }


def scored_pair(
    task_id: str,
    user_id: str,
    no_memory: bool,
    memory_pack: bool,
    *,
    memory_requirement: str = "required",
) -> ScoredPair:
    return ScoredPair(
        task_id=task_id,
        user_id=user_id,
        no_memory_success=no_memory,
        memory_pack_success=memory_pack,
        scenario_family="stable_preferences",
        surface_memberships=("text_chat",),
        memory_requirement=memory_requirement,
        history_length=50,
    )


def empty_safety_reviews(task_id: str) -> list[dict]:
    return [
        {
            "schema_version": "officelife-track-b-safety-review-v1",
            "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
            "task_id": task_id,
            "arm": arm,
            "category": category,
            "review_complete": True,
            "findings": [],
            "assessor_id": "assessor-01",
            "reviewed_at": "2026-08-04T00:00:00Z",
        }
        for arm in ("no_memory", "memory_pack")
        for category in (
            "unsupported_memory",
            "stale_or_superseded",
            "cross_scope_leakage",
            "deletion_violation",
            "partial_asr_durable_commit",
            "no_evidence_false_answer",
        )
    ]


def adjudicated_arm(
    *,
    task_id: str,
    arm: str,
    blinded_output_id: str,
    task_success: Optional[int] = 1,
    outcome: str = "answer",
) -> dict:
    return {
        "schema_version": "officelife-track-b-adjudicated-arm-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "adjudication_id": f"adjudication-{arm}",
        "execution_id": f"execution-{arm}",
        "task_id": task_id,
        "arm": arm,
        "blinded_output_id": blinded_output_id,
        "outcome": outcome,
        "deterministic_assessment_ids": [],
        "rating_submission_ids": [],
        "safety_finding_ids": [],
        "citation_assessment_ids": [],
        "arm_measurement_id": f"measurement-{arm}",
        "deterministic_all_passed": outcome != "infrastructure_error",
        "hard_prohibition_fired": False,
        "final_human_judgment": (
            "not_evaluable" if outcome == "infrastructure_error" else "not_required"
        ),
        "scored_product_failure": outcome in {"product_timeout", "product_failure"},
        "adjudication_complete": outcome != "infrastructure_error",
        "task_success": task_success,
        "adjudicated_at": "2026-08-04T00:00:00Z",
    }


def lift_metric(lift: float = 0.2) -> dict:
    pair_count = 300
    no_memory_count = 150
    memory_pack_count = no_memory_count + round(lift * pair_count)
    return {
        "pair_count": pair_count,
        "no_memory_success_count": no_memory_count,
        "memory_pack_success_count": memory_pack_count,
        "no_memory_success_rate": no_memory_count / pair_count,
        "memory_pack_success_rate": memory_pack_count / pair_count,
        "absolute_lift": memory_pack_count / pair_count - no_memory_count / pair_count,
    }


def private_summary_artifact() -> dict:
    rate_zero = {"numerator": 0, "denominator": 300, "value": 0.0}
    overall_metric = lift_metric()
    memory_required_metric = lift_metric(0.25)
    no_evidence_metric = lift_metric(0.0)
    observed = {
        "overall_task_success_lift": overall_metric["absolute_lift"],
        "clustered_ci_lower": 0.1,
        "memory_required_lift": memory_required_metric["absolute_lift"],
        "no_evidence_delta": no_evidence_metric["absolute_lift"],
        "unsupported_memory_rate": 0.0,
        "stale_or_superseded_rate": 0.0,
        "cross_scope_leakage_count": 0,
        "deletion_violation_count": 0,
        "partial_asr_durable_commit_count": 0,
        "recall_latency_p95_ms": 100.0,
    }
    gates = [
        {
            "schema_version": "officelife-track-b-gate-result-v1",
            "gate_id": gate_id,
            "operator": operator,
            "threshold": threshold,
            "observed": observed[gate_id],
            "status": "passed",
        }
        for gate_id, (operator, threshold) in GATE_DEFINITIONS.items()
    ]
    return {
        "schema_version": PRIVATE_SUMMARY_SCHEMA_VERSION,
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "generated_at": "2026-08-04T00:00:00Z",
        "qualification_plan_sha256": "0" * 64,
        "execution_manifest_sha256": "1" * 64,
        "rating_manifest_sha256": "2" * 64,
        "adjudication_manifest_sha256": "a" * 64,
        "latency_manifest_sha256": "3" * 64,
        "scored_arms_sha256": "4" * 64,
        "scored_pairs_sha256": "5" * 64,
        "pair_count": 300,
        "user_count": 30,
        "overall": overall_metric,
        "memory_required": memory_required_metric,
        "no_evidence": no_evidence_metric,
        "bootstrap": {
            "samples": BOOTSTRAP_SAMPLES,
            "seed": BOOTSTRAP_SEED,
            "prng_algorithm": BOOTSTRAP_PRNG,
            "quantile_method": BOOTSTRAP_QUANTILE,
            "paired_task_count": 300,
            "user_count": 30,
            "per_user_task_counts": [10] * 30,
            "task_micro_lift": overall_metric["absolute_lift"],
            "task_micro_interval": {
                "level": 0.95,
                "lower": 0.1,
                "upper": 0.3,
                "quantile_method": BOOTSTRAP_QUANTILE,
            },
            "user_macro_lift": 0.2,
        },
        "safety": {
            "unsupported_memory_tasks": copy.deepcopy(rate_zero),
            "stale_or_superseded_tasks": copy.deepcopy(rate_zero),
            "no_evidence_false_answers_no_memory": copy.deepcopy(rate_zero),
            "no_evidence_false_answers_memory_pack": copy.deepcopy(rate_zero),
            "deletion_violation_tasks": 0,
            "cross_scope_leakage_tasks": 0,
            "partial_asr_durable_commits": 0,
            "finding_count": 0,
        },
        "citations": {
            "precision": {"numerator": 270, "denominator": 300, "value": 0.9},
            "source_coverage": {"numerator": 285, "denominator": 300, "value": 0.95},
        },
        "rater_agreement": {
            "human_rated_output_count": 600,
            "initial_agreement_count": 540,
            "initial_disagreement_count": 60,
            "third_rater_output_count": 60,
            "initial_agreement_rate": 0.9,
        },
        "latency": {
            "sample_count": 1000,
            "event_count": 1000,
            "query_count": 100,
            "warmup_passes": 1,
            "measured_passes": 10,
            "p50_ms": 80.0,
            "p95_ms": 100.0,
            "gate_threshold_ms": 300,
            "gate_passed": True,
            "reference_environment_sha256": "6" * 64,
        },
        "slices": [],
        "gates": gates,
        "qualification_status": "passed",
        "qualification_complete": True,
        "qualification_eligible": True,
        "nonqualification_reasons": [],
        "claimable": True,
    }


def publication_context() -> dict:
    return {
        "generated_at": "2026-08-04T01:00:00Z",
        "evaluation_started_at": "2026-08-04T00:00:00Z",
        "evaluation_ended_at": "2026-08-04T00:30:00Z",
        "dataset_release_sha256": "8" * 64,
        "release_artifact_sha256": "9" * 64,
        "commit_id": "a" * 40,
        "configuration_hashes": [{"name": "prompt", "sha256": "b" * 64}],
        "model_provider_role_hashes": [
            {"name": "reader", "sha256": "c" * 64}
        ],
        "environment": {
            "environment_sha256": "d" * 64,
            "operating_system": "Linux",
            "runtime": "CPython 3.13",
            "cpu_class": "reference-cpu",
            "storage_class": "local-ssd",
            "locale": "C.UTF-8",
        },
        "error_counts": [],
        "aggregate_cost_usd": None,
        "incomplete_iteration_count": 0,
        "failed_iteration_count": 0,
        "reverted_iteration_count": 0,
        "claim_limitations": [
            "This aggregate is limited to the frozen qualification protocol."
        ],
    }


def qualification_plan() -> dict:
    return {
        "schema_version": QUALIFICATION_PLAN_SCHEMA_VERSION,
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "artifact_contract_version": "officelife-track-b-artifact-contract-v1",
        "executor_contract_version": "officelife-track-b-executor-v1",
        "execution_profile_version": "officelife-track-b-execution-profile-v1",
        "generated_at": "2026-08-04T00:00:00Z",
        "dataset_manifest_sha256": "0" * 64,
        "scoring_algorithm": "officelife-track-b-binary-all-must-pass-v1",
        "scoring_implementation_sha256": "1" * 64,
        "assignment_algorithm": "sha256-balanced-six-raters-v1",
        "adjudication_algorithm": "two-raters-third-on-disagreement-v1",
        "assignment_seed": 7,
        "annotation_codebook_sha256": "2" * 64,
        "bootstrap": {
            "samples": BOOTSTRAP_SAMPLES,
            "seed": BOOTSTRAP_SEED,
            "prng_algorithm": BOOTSTRAP_PRNG,
            "quantile_method": BOOTSTRAP_QUANTILE,
        },
        "latency": {
            "fixture_sha256": "3" * 64,
            "query_sha256": "4" * 64,
            "runner_sha256": "5" * 64,
        },
        "public_projector_version": PUBLIC_PROJECTOR_VERSION,
        "custodian_public_key_sha256": "6" * 64,
        "iteration_selection_rule": (
            "officelife-track-b-first-complete-sealed-iteration-v1"
        ),
        "gate_thresholds": {
            "overall_lift_min": 0.1,
            "cluster_ci_lower_exclusive": 0,
            "memory_required_lift_min": 0.15,
            "no_evidence_delta_min": -0.01,
            "unsupported_memory_rate_max": 0.02,
            "stale_superseded_rate_max": 0.02,
            "cross_scope_leakage_max": 0,
            "deletion_violation_max": 0,
            "partial_asr_durable_commit_max": 0,
            "recall_p95_ms_max": 300,
        },
    }


def public_manifest_for(result_raw: bytes) -> dict:
    result = json.loads(result_raw.decode("utf-8"))
    return {
        "schema_version": "officelife-track-b-public-manifest-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "generated_at": "2026-08-04T01:00:00Z",
        "previous_public_manifest_sha256": None,
        "qualification_manifest_sha256": result.get(
            "qualification_manifest_sha256", "7" * 64
        ),
        "private_summary_sha256": "1" * 64,
        "public_result_sha256": sha256_bytes(result_raw),
        "public_projector_version": PUBLIC_PROJECTOR_VERSION,
        "public_projector_sha256": "2" * 64,
        "files": [
            {
                "artifact_id": "public-result",
                "path": "public-result.json",
                "role": "public-result",
                "artifact_kind": "json-document",
                "sha256": sha256_bytes(result_raw),
                "size_bytes": len(result_raw),
                "schema_version": "officelife-track-b-public-result-v1",
                "record_count": 1,
                "sensitivity": "public",
                "access_class": "public_projection",
                "media_type": "application/json",
            }
        ],
        "qualification_eligible": True,
        "nonqualification_reasons": [],
        "claimable": True,
    }


def write_jsonl_records(path: Path, records: list[dict]) -> bytes:
    raw = b"".join(canonical_json(record) + b"\n" for record in records)
    path.write_bytes(raw)
    return raw


def private_inventory_entry(
    path: str,
    role: str,
    schema_version: str,
    raw: bytes,
    record_count: int,
) -> dict:
    return {
        "path": path,
        "role": role,
        "artifact_kind": "jsonl-records",
        "sha256": sha256_bytes(raw),
        "size_bytes": len(raw),
        "schema_version": schema_version,
        "record_count": record_count,
        "sensitivity": "restricted",
        "access_class": "custodian_only",
        "media_type": "application/x-ndjson",
    }


def write_rating_bundle(root: Path, *, second_rater: str = "rater-b") -> None:
    output = arm_output()
    output_sha256 = sha256_bytes(canonical_json(output))
    item = {
        "schema_version": "officelife-track-b-rating-item-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "rating_item_id": "rating-item-01",
        "blinded_output_id": output["blinded_output_id"],
        "output_artifact_sha256": output_sha256,
        "task_material_sha256": "1" * 64,
        "annotation_codebook_sha256": "3" * 64,
        "outcome": "answer",
        "human_judgment_required": True,
        "prepared_at": "2026-08-04T00:00:00Z",
    }
    assignments = [
        {
            "schema_version": "officelife-track-b-rating-assignment-v1",
            "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
            "assignment_id": f"assignment-{ordinal}",
            "rating_item_id": "rating-item-01",
            "rater_id": rater_id,
            "rating_round": ordinal,
            "assignment_order": ordinal,
            "assignment_trigger": "initial",
            "assignment_algorithm": "sha256-balanced-six-raters-v1",
            "assignment_seed": 7,
            "assigned_at": "2026-08-04T00:00:00Z",
        }
        for ordinal, rater_id in ((1, "rater-a"), (2, second_rater))
    ]
    submissions = [
        rating(output, "rater-a", 1, "pass"),
        rating(output, second_rater, 2, "pass"),
    ]
    item_raw = write_jsonl_records(root / "rating-items.jsonl", [item])
    assignment_raw = write_jsonl_records(
        root / "rating-assignments.jsonl", assignments
    )
    submission_raw = write_jsonl_records(
        root / "rating-submissions.jsonl", submissions
    )
    manifest = {
        "schema_version": "officelife-track-b-rating-manifest-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "generated_at": "2026-08-04T00:00:00Z",
        "previous_rating_manifest_sha256": None,
        "qualification_plan_sha256": "4" * 64,
        "execution_manifest_sha256": "5" * 64,
        "blinded_outputs_sha256": "6" * 64,
        "annotation_codebook_sha256": "3" * 64,
        "rating_items_sha256": sha256_bytes(item_raw),
        "rating_assignments_sha256": sha256_bytes(assignment_raw),
        "rating_submissions_sha256": sha256_bytes(submission_raw),
        "files": [
            private_inventory_entry(
                "rating-items.jsonl",
                "rating-items",
                "officelife-track-b-rating-item-v1",
                item_raw,
                1,
            ),
            private_inventory_entry(
                "rating-assignments.jsonl",
                "rating-assignments",
                "officelife-track-b-rating-assignment-v1",
                assignment_raw,
                2,
            ),
            private_inventory_entry(
                "rating-submissions.jsonl",
                "rating-submissions",
                "officelife-track-b-rating-submission-v1",
                submission_raw,
                2,
            ),
        ],
        "qualification_eligible": False,
        "nonqualification_reasons": ["current_executor_nonqualifying"],
        "claimable": False,
    }
    (root / "rating-manifest.json").write_bytes(canonical_json(manifest, pretty=True))


class OfficeLifeTrackBQualificationTest(unittest.TestCase):
    def test_qualification_plan_is_one_way_bound_before_execution(self) -> None:
        plan = qualification_plan()
        validated = validate_qualification_plan(plan)

        self.assertEqual(plan, validated)
        self.assertNotIn("sealed_run_manifest_sha256", validated)
        self.assertNotIn("execution_manifest_sha256", validated)
        post_execution_tamper = copy.deepcopy(plan)
        post_execution_tamper["execution_manifest_sha256"] = "f" * 64
        with self.assertRaisesRegex(QualificationValidationError, "frozen schema"):
            validate_qualification_plan(post_execution_tamper)
        invalid_timestamp = copy.deepcopy(plan)
        invalid_timestamp["generated_at"] = "2026-99-99T99:99:99Z"
        with self.assertRaisesRegex(QualificationValidationError, "frozen schema"):
            validate_qualification_plan(invalid_timestamp)

    def test_frozen_percentile_algorithms_have_boundary_behavior(self) -> None:
        self.assertEqual(2.5, type7_percentile([1.0, 2.0, 3.0, 4.0], 50))
        self.assertEqual(301.0, nearest_rank_percentile([1.0] * 949 + [301.0] * 51, 95))
        self.assertEqual(300.0, nearest_rank_percentile([1.0] * 949 + [300.0] * 51, 95))

    def test_cluster_bootstrap_is_frozen_and_task_micro_weighted(self) -> None:
        pairs = [
            scored_pair("task-a1", "user-a", False, True),
            scored_pair("task-a2", "user-a", False, True),
            scored_pair("task-a3", "user-a", False, True),
            scored_pair("task-b1", "user-b", True, False),
        ]
        first = paired_user_cluster_bootstrap(pairs, samples=8, seed=7)
        second = paired_user_cluster_bootstrap(pairs, samples=8, seed=7)

        self.assertEqual(first, second)
        self.assertEqual([1.0, 1.0, 0.5, 0.5, 1.0, 0.5, 0.5, 0.5], first)
        metrics, replicates = summarize_pairs(pairs, bootstrap_samples=8, bootstrap_seed=7)
        self.assertEqual(0.5, metrics["overall"]["task_success_lift"])
        self.assertEqual(0.0, metrics["user_macro"]["mean_lift"])
        self.assertEqual(BOOTSTRAP_PRNG, metrics["bootstrap"]["prng_algorithm"])
        self.assertEqual(BOOTSTRAP_QUANTILE, metrics["bootstrap"]["quantile_method"])
        self.assertEqual(first, replicates)
        self.assertEqual(100_000, BOOTSTRAP_SAMPLES)
        self.assertEqual(20260804, BOOTSTRAP_SEED)

    def test_human_rating_adjudication_is_strict(self) -> None:
        output = arm_output()
        self.assertTrue(
            resolve_human_judgment(
                [rating(output, "rater-a", 1, "pass"), rating(output, "rater-b", 2, "pass")]
            )
        )
        self.assertTrue(
            resolve_human_judgment(
                [
                    rating(output, "rater-a", 1, "pass"),
                    rating(output, "rater-b", 2, "fail"),
                    rating(output, "rater-c", 3, "pass"),
                ]
            )
        )
        with self.assertRaisesRegex(QualificationValidationError, "forbidden"):
            resolve_human_judgment(
                [
                    rating(output, "rater-a", 1, "pass"),
                    rating(output, "rater-b", 2, "pass"),
                    rating(output, "rater-c", 3, "pass"),
                ]
            )
        with self.assertRaisesRegex(QualificationValidationError, "distinct"):
            resolve_human_judgment(
                [rating(output, "same-rater", 1, "pass"), rating(output, "same-rater", 2, "pass")]
            )

    def test_success_is_derived_from_bound_checks_and_ratings(self) -> None:
        output = arm_output("blue")
        label = task_label(human=True)
        checks = [
            assessment(output, label, "check-required", "pass", span=(0, 4)),
            assessment(output, label, "check-hard", "pass"),
        ]
        ratings = [
            rating(output, "rater-a", 1, "pass"),
            rating(output, "rater-b", 2, "pass"),
        ]
        self.assertTrue(
            derive_arm_success(
                output,
                label,
                checks,
                ratings,
                parser_commitment=PARSER_COMMITMENT,
            )
        )

        hard_failure = copy.deepcopy(checks)
        hard_failure[1]["verdict"] = "fail"
        self.assertFalse(
            derive_arm_success(
                output,
                label,
                hard_failure,
                ratings,
                parser_commitment=PARSER_COMMITMENT,
            )
        )

        with self.assertRaisesRegex(QualificationValidationError, "closure"):
            derive_arm_success(
                output,
                label,
                checks[:1],
                ratings,
                parser_commitment=PARSER_COMMITMENT,
            )
        weakened_label = copy.deepcopy(label)
        weakened_label["success_rule"]["required_check_ids"] = ["check-hard"]
        weakened_checks = [
            assessment(output, weakened_label, "check-required", "pass", span=(0, 4)),
            assessment(output, weakened_label, "check-hard", "pass"),
        ]
        with self.assertRaisesRegex(QualificationValidationError, "success rule closure"):
            derive_arm_success(
                output,
                weakened_label,
                weakened_checks,
                ratings,
                parser_commitment=PARSER_COMMITMENT,
            )
        tampered = copy.deepcopy(checks)
        tampered[0]["output_sha256"] = "f" * 64
        with self.assertRaisesRegex(QualificationValidationError, "output hash"):
            derive_arm_success(
                output,
                label,
                tampered,
                ratings,
                parser_commitment=PARSER_COMMITMENT,
            )
        tampered_span = copy.deepcopy(checks)
        tampered_span[0]["evidence_spans"][0]["matched_text_sha256"] = "f" * 64
        with self.assertRaisesRegex(QualificationValidationError, "span hash"):
            derive_arm_success(
                output,
                label,
                tampered_span,
                ratings,
                parser_commitment=PARSER_COMMITMENT,
            )

        wrong_task = copy.deepcopy(checks)
        wrong_task[0]["task_id"] = "other-task"
        with self.assertRaisesRegex(QualificationValidationError, "task binding"):
            derive_arm_success(
                output,
                label,
                wrong_task,
                ratings,
                parser_commitment=PARSER_COMMITMENT,
            )
        wrong_parser = copy.deepcopy(checks)
        wrong_parser[0]["parser_sha256"] = "f" * 64
        with self.assertRaisesRegex(QualificationValidationError, "parser binding"):
            derive_arm_success(
                output,
                label,
                wrong_parser,
                ratings,
                parser_commitment=PARSER_COMMITMENT,
            )

        self.assertFalse(
            derive_arm_success(
                arm_output(outcome="product_timeout"),
                label,
                [],
                [],
                parser_commitment=PARSER_COMMITMENT,
            )
        )
        self.assertIsNone(
            derive_arm_success(
                arm_output(outcome="infrastructure_error"),
                label,
                [],
                [],
                parser_commitment=PARSER_COMMITMENT,
            )
        )

    def test_pairing_requires_exact_treatment_pair_and_metadata(self) -> None:
        common = {
            "task_id": "task-01",
            "user_id": "user-01",
            "outcome": "answer",
            "success": True,
            "scenario_family": "stable_preferences",
            "surface_memberships": ("text_chat",),
            "memory_requirement": "required",
            "history_length": 50,
        }
        no_memory = ScoredArm(blinded_output_id="blind-a", arm="no_memory", **common)
        memory_pack = ScoredArm(blinded_output_id="blind-b", arm="memory_pack", **common)
        pairs = pair_scored_arms([memory_pack, no_memory])
        self.assertEqual(1, len(pairs))
        with self.assertRaisesRegex(QualificationValidationError, "complete"):
            pair_scored_arms([no_memory])
        changed = ScoredArm(
            blinded_output_id="blind-b",
            arm="memory_pack",
            **{**common, "history_length": 51},
        )
        with self.assertRaisesRegex(QualificationValidationError, "metadata"):
            pair_scored_arms([no_memory, changed])
        same_output = ScoredArm(
            blinded_output_id=no_memory.blinded_output_id,
            arm="memory_pack",
            **common,
        )
        with self.assertRaisesRegex(QualificationValidationError, "distinct outputs"):
            pair_scored_arms([no_memory, same_output])

    def test_formal_scoring_records_and_private_summary_are_derived(self) -> None:
        task_id = "task-01"
        reviews = empty_safety_reviews(task_id)
        common = {
            "task_id": task_id,
            "user_id": "user-01",
            "outcome": "answer",
            "success": True,
            "scenario_family": "stable_preferences",
            "surface_memberships": ("text_chat",),
            "memory_requirement": "required",
            "history_length": 50,
        }
        calculations = [
            ScoredArm(
                blinded_output_id="blind-no-memory",
                arm="no_memory",
                **{**common, "success": False},
            ),
            ScoredArm(
                blinded_output_id="blind-memory-pack",
                arm="memory_pack",
                **common,
            ),
        ]
        formal_arms = [
            build_scored_arm_record(
                calculation,
                adjudicated_arm(
                    task_id=task_id,
                    arm=calculation.arm,
                    blinded_output_id=calculation.blinded_output_id,
                    task_success=int(calculation.success),
                ),
                reviews,
                [],
                scored_at="2026-08-04T00:01:00Z",
            )
            for calculation in calculations
        ]
        pair = pair_scored_arms(calculations)[0]
        formal_pair = build_scored_pair_record(
            pair,
            formal_arms,
            scored_at="2026-08-04T00:01:00Z",
        )

        self.assertEqual(1, formal_pair["paired_difference"])
        self.assertTrue(formal_pair["complete_pair"])
        self.assertEqual(
            {item["scored_arm_id"] for item in formal_arms},
            {
                formal_pair["no_memory_scored_arm_id"],
                formal_pair["memory_pack_scored_arm_id"],
            },
        )

        metrics, _replicates = summarize_pairs([pair])
        safety = derive_safety_metrics(reviews, [pair])
        citations = derive_citation_metrics([])
        summary = build_private_summary(
            [pair],
            metrics=metrics,
            safety=safety,
            citations=citations,
            rater_agreement={
                "human_rated_output_count": 0,
                "initial_agreement_count": 0,
                "initial_disagreement_count": 0,
                "third_rater_output_count": 0,
                "initial_agreement_rate": None,
            },
            latency={
                "sample_count": 1000,
                "event_count": 1000,
                "query_count": 100,
                "warmup_passes": 1,
                "measured_passes": 10,
                "p50_ms": 80.0,
                "p95_ms": 100.0,
                "gate_threshold_ms": 300,
                "gate_passed": True,
                "reference_environment_sha256": "6" * 64,
            },
            artifact_hashes={
                "qualification_plan_sha256": "0" * 64,
                "execution_manifest_sha256": "1" * 64,
                "rating_manifest_sha256": "2" * 64,
                "adjudication_manifest_sha256": "a" * 64,
                "latency_manifest_sha256": "3" * 64,
                "scored_arms_sha256": "4" * 64,
                "scored_pairs_sha256": "5" * 64,
            },
            generated_at="2026-08-04T00:02:00Z",
            eligibility_reports={
                "upstream": {
                    "validation_passed": True,
                    "upstream_ready_for_qualification": False,
                    "nonqualification_reasons": ["current_executor_nonqualifying"],
                },
                "rating": {
                    "passed": True,
                    "qualification_eligible": False,
                    "nonqualification_reasons": ["current_executor_nonqualifying"],
                },
                "adjudication": {
                    "passed": True,
                    "qualification_eligible": False,
                    "nonqualification_reasons": ["current_executor_nonqualifying"],
                },
                "latency": {
                    "passed": True,
                    "qualification_eligible": False,
                    "nonqualification_reasons": ["current_executor_nonqualifying"],
                },
            },
        )
        self.assertEqual("not_eligible", summary["qualification_status"])
        self.assertFalse(summary["qualification_eligible"])
        self.assertFalse(summary["claimable"])

    def test_safety_requires_exhaustive_reviews_and_deduplicates_tasks(self) -> None:
        pair = scored_pair("task-01", "user-01", False, True)
        categories = (
            "unsupported_memory",
            "stale_or_superseded",
            "cross_scope_leakage",
            "deletion_violation",
            "partial_asr_durable_commit",
            "no_evidence_false_answer",
        )
        reviews = []
        for arm in ("no_memory", "memory_pack"):
            for category in categories:
                findings = []
                if category == "cross_scope_leakage":
                    findings = [
                        {
                            "schema_version": "officelife-track-b-safety-finding-v1",
                            "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
                            "finding_id": f"finding-{arm}",
                            "task_id": "task-01",
                            "blinded_output_id": f"blind-{arm}",
                            "category": category,
                            "claim_ref": "claim-01",
                            "material": True,
                            "evidence_spans": [
                                {
                                    "artifact_sha256": "4" * 64,
                                    "start_char": 0,
                                    "end_char": 4,
                                    "evidence_kind": "output_text",
                                    "source_ref": "claim-01",
                                }
                            ],
                            "source_event_ids": ["source-01"],
                            "active_artifact_sha256s": ["5" * 64],
                            "assessor_id": "assessor-01",
                            "detected_at": "2026-08-04T00:00:00Z",
                        }
                    ]
                reviews.append(
                    {
                        "schema_version": "officelife-track-b-safety-review-v1",
                        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
                        "task_id": "task-01",
                        "arm": arm,
                        "category": category,
                        "review_complete": True,
                        "findings": findings,
                        "assessor_id": "assessor-01",
                        "reviewed_at": "2026-08-04T00:00:00Z",
                    }
                )

        metrics = derive_safety_metrics(reviews, [pair])
        self.assertEqual(1, metrics["cross_scope_leakage_tasks"])
        self.assertEqual(2, metrics["finding_count"])
        with self.assertRaisesRegex(QualificationValidationError, "closure"):
            derive_safety_metrics(reviews[:-1], [pair])

    def test_citation_metrics_use_claim_to_source_and_required_fact_records(self) -> None:
        metrics = derive_citation_metrics(
            [
                {
                    "schema_version": "officelife-track-b-citation-assessment-v1",
                    "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
                    "citation_assessment_id": "citation-01",
                    "task_id": "task-01",
                    "blinded_output_id": "blind-01",
                    "claim_ref": "claim-01",
                    "required_fact_ref": None,
                    "citation_link_ref": "link-01",
                    "source_event_ref": "event-01",
                    "emitted": True,
                    "inside_allowed_scope": True,
                    "predates_task_cutoff": True,
                    "valid_at_task_cutoff": True,
                    "supports_claim": True,
                    "correct": True,
                    "evidence_spans": [],
                    "assessor_id": "assessor-01",
                    "assessed_at": "2026-08-04T00:00:00Z",
                },
                {
                    "schema_version": "officelife-track-b-citation-assessment-v1",
                    "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
                    "citation_assessment_id": "citation-02",
                    "task_id": "task-01",
                    "blinded_output_id": "blind-01",
                    "claim_ref": "claim-02",
                    "required_fact_ref": None,
                    "citation_link_ref": "link-02",
                    "source_event_ref": "event-02",
                    "emitted": True,
                    "inside_allowed_scope": True,
                    "predates_task_cutoff": True,
                    "valid_at_task_cutoff": True,
                    "supports_claim": False,
                    "correct": False,
                    "evidence_spans": [],
                    "assessor_id": "assessor-01",
                    "assessed_at": "2026-08-04T00:00:00Z",
                },
                {
                    "schema_version": "officelife-track-b-citation-assessment-v1",
                    "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
                    "citation_assessment_id": "citation-03",
                    "task_id": "task-01",
                    "blinded_output_id": "blind-01",
                    "claim_ref": "claim-01",
                    "required_fact_ref": "fact-01",
                    "citation_link_ref": None,
                    "source_event_ref": "event-01",
                    "emitted": False,
                    "inside_allowed_scope": True,
                    "predates_task_cutoff": True,
                    "valid_at_task_cutoff": True,
                    "supports_claim": True,
                    "correct": True,
                    "evidence_spans": [],
                    "assessor_id": "assessor-01",
                    "assessed_at": "2026-08-04T00:00:00Z",
                },
            ]
        )
        self.assertEqual(0.5, metrics["precision"]["value"])
        self.assertEqual(1.0, metrics["source_coverage"]["value"])
        inconsistent = {
            "schema_version": "officelife-track-b-citation-assessment-v1",
            "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
            "citation_assessment_id": "citation-bad",
            "task_id": "task-01",
            "blinded_output_id": "blind-01",
            "claim_ref": "claim-01",
            "required_fact_ref": None,
            "citation_link_ref": "link-bad",
            "source_event_ref": "event-01",
            "emitted": True,
            "inside_allowed_scope": True,
            "predates_task_cutoff": True,
            "valid_at_task_cutoff": True,
            "supports_claim": False,
            "correct": True,
            "evidence_spans": [],
            "assessor_id": "assessor-01",
            "assessed_at": "2026-08-04T00:00:00Z",
        }
        with self.assertRaisesRegex(QualificationValidationError, "derived"):
            derive_citation_metrics([inconsistent])

    def test_release_gate_boundaries_are_fail_closed(self) -> None:
        metrics = {
            "overall": {"task_success_lift": 0.10},
            "memory_required": {"task_success_lift": 0.15},
            "no_evidence": {"task_success_lift": -0.01},
            "clustered_bootstrap_ci95": [0.01, 0.2],
        }
        safety = {
            "unsupported_memory_tasks": {
                "numerator": 2,
                "denominator": 100,
                "value": 0.02,
            },
            "stale_or_superseded_tasks": {
                "numerator": 2,
                "denominator": 100,
                "value": 0.02,
            },
            "cross_scope_leakage_tasks": 0,
            "deletion_violation_tasks": 0,
            "partial_asr_durable_commits": 0,
        }
        passed = evaluate_release_gates(
            metrics,
            safety,
            {"measurement_complete": True, "p95_ms": 300.0},
            evaluable=True,
        )
        self.assertEqual("passed", passed["status"])

        over_latency = evaluate_release_gates(
            metrics,
            safety,
            {"measurement_complete": True, "p95_ms": 300.000001},
            evaluable=True,
        )
        self.assertEqual("failed", over_latency["status"])
        zero_ci = copy.deepcopy(metrics)
        zero_ci["clustered_bootstrap_ci95"] = [0.0, 0.2]
        failed = evaluate_release_gates(
            zero_ci,
            safety,
            {"measurement_complete": True, "p95_ms": 300.0},
            evaluable=True,
        )
        self.assertEqual("failed", failed["status"])
        diagnostic = evaluate_release_gates(
            metrics,
            safety,
            {"measurement_complete": True, "p95_ms": 1.0},
            evaluable=False,
        )
        self.assertEqual("not_evaluable", diagnostic["status"])
        self.assertIsNone(diagnostic["all_passed"])

    def test_public_projection_is_allowlisted_and_requires_approval(self) -> None:
        from tests.test_officelife_track_b_qualification_bundles import (
            _claimable_projection_inputs,
            _mutate_json,
            _refresh_manifest,
            _write_qualification_bundle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, receipt, public_key, context, _private_key = _claimable_projection_inputs(
                root
            )
            public = public_result_projection(
                root,
                plan,
                receipt,
                public_key,
                context,
            )
            self.assertEqual(1, public["pair_count"])
            self.assertTrue(public["claimable"])
            self.assertTrue(public["qualification_eligible"])

            unapproved = copy.deepcopy(receipt)
            unapproved["publication_status"] = "pending"
            with self.assertRaisesRegex(QualificationValidationError, "receipt"):
                public_result_projection(
                    root,
                    plan,
                    unapproved,
                    public_key,
                    context,
                )
            tampered_signature = copy.deepcopy(receipt)
            replacement = "A" if receipt["signature"][-1] != "A" else "B"
            tampered_signature["signature"] = receipt["signature"][:-1] + replacement
            with self.assertRaisesRegex(QualificationValidationError, "signed"):
                public_result_projection(
                    root,
                    plan,
                    tampered_signature,
                    public_key,
                    context,
                )
            unsafe = copy.deepcopy(context)
            unsafe["answer"] = "PRIVATE-SENTINEL"
            with self.assertRaisesRegex(QualificationValidationError, "context"):
                public_result_projection(
                    root,
                    plan,
                    receipt,
                    public_key,
                    unsafe,
                )

            _mutate_json(
                root,
                "private-summary.json",
                lambda summary: summary["overall"].__setitem__(
                    "no_memory_success_count", 2
                ),
            )
            _refresh_manifest(root, "qualification-manifest.json")
            with self.assertRaisesRegex(QualificationValidationError, "validated"):
                public_result_projection(
                    root,
                    plan,
                    receipt,
                    public_key,
                    context,
                )

        with tempfile.TemporaryDirectory() as tmp:
            ineligible_root = Path(tmp)
            _write_qualification_bundle(ineligible_root)
            with self.assertRaisesRegex(QualificationValidationError, "claimable"):
                public_result_projection(
                    ineligible_root,
                    plan,
                    receipt,
                    public_key,
                    context,
                )

    def test_current_callable_executor_cannot_be_upgraded_by_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            run_root = root / "run"
            worker_root = root / "worker"
            execution_root = root / "execution"
            dataset_root.mkdir()
            run_root.mkdir()
            build_dataset_bundle(dataset_root)
            build_run_bundle(run_root, dataset_root)
            add_executor_config(run_root)
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            execute_worker_bundle(
                worker_root,
                execution_root,
                RecordingHandler(),
                sleeper=lambda _seconds: None,
            )

            chain = validate_upstream_chain(
                dataset_root,
                run_root,
                worker_root,
                execution_root,
            )

        self.assertEqual("not_eligible", chain["qualification_status"])
        self.assertFalse(chain["executor_qualification_eligible"])
        self.assertIn("callable_handler_test_only", chain["nonqualification_reasons"])
        self.assertIn("opaque_snapshot_adapter", chain["nonqualification_reasons"])

    def test_upstream_chain_rejects_run_plan_resealed_after_worker_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            run_root = root / "run"
            worker_root = root / "worker"
            execution_root = root / "execution"
            dataset_root.mkdir()
            run_root.mkdir()
            build_dataset_bundle(dataset_root)
            build_run_bundle(run_root, dataset_root)
            add_executor_config(run_root)
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            execute_worker_bundle(
                worker_root,
                execution_root,
                RecordingHandler(),
                sleeper=lambda _seconds: None,
            )
            plan_path = run_root / "artifacts/qualification-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["assignment_seed"] = 8
            write_json(plan_path, plan)
            manifest_path = run_root / "sealed-run-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = next(
                item
                for item in manifest["files"]
                if item["role"] == "qualification-plan"
            )
            entry["sha256"] = sha256(plan_path)
            entry["size_bytes"] = plan_path.stat().st_size
            manifest["system_artifacts"]["qualification_plan"]["sha256"] = entry[
                "sha256"
            ]
            write_json(manifest_path, manifest)

            chain = validate_upstream_chain(
                dataset_root,
                run_root,
                worker_root,
                execution_root,
            )

        self.assertFalse(chain["validation_passed"])
        self.assertIn(
            "upstream-chain# worker_run_manifest_binding_mismatch",
            chain["errors"],
        )

    def test_sealed_plan_binds_the_active_scorer_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            run_root = root / "run"
            dataset_root.mkdir()
            run_root.mkdir()
            build_dataset_bundle(dataset_root)
            build_run_bundle(run_root, dataset_root)
            plan_path = run_root / "artifacts/qualification-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["scoring_implementation_sha256"] = "0" * 64
            write_json(plan_path, plan)
            manifest_path = run_root / "sealed-run-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = next(
                item
                for item in manifest["files"]
                if item["role"] == "qualification-plan"
            )
            entry["sha256"] = sha256(plan_path)
            entry["size_bytes"] = plan_path.stat().st_size
            manifest["system_artifacts"]["qualification_plan"]["sha256"] = entry[
                "sha256"
            ]
            write_json(manifest_path, manifest)

            report = validate_sealed_qualification_plan(dataset_root, run_root)

        self.assertFalse(report["passed"])
        self.assertIn(
            "qualification-plan# scoring_implementation_binding_mismatch",
            report["errors"],
        )

    def test_qualification_chain_rejects_rating_from_a_different_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            run_root = root / "run"
            worker_root = root / "worker"
            execution_root = root / "execution"
            rating_root = root / "rating"
            adjudication_root = root / "adjudication"
            latency_root = root / "latency"
            qualification_root = root / "qualification"
            for directory in (
                dataset_root,
                run_root,
                rating_root,
                adjudication_root,
                latency_root,
                qualification_root,
            ):
                directory.mkdir()
            build_dataset_bundle(dataset_root)
            build_run_bundle(run_root, dataset_root)
            add_executor_config(run_root)
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            execute_worker_bundle(
                worker_root,
                execution_root,
                RecordingHandler(),
                sleeper=lambda _seconds: None,
            )
            write_rating_bundle(rating_root)
            for directory, manifest_name in (
                (adjudication_root, "adjudication-manifest.json"),
                (latency_root, "latency-manifest.json"),
                (qualification_root, "qualification-manifest.json"),
            ):
                write_json(directory / manifest_name, {})

            chain = validate_qualification_chain(
                dataset_root,
                run_root,
                worker_root,
                execution_root,
                rating_root,
                adjudication_root,
                latency_root,
                qualification_root,
            )

        self.assertFalse(chain["chain_binding_passed"])
        self.assertIn(
            "qualification-chain# rating_qualification_plan_binding_mismatch",
            chain["errors"],
        )

    def test_qualification_cli_accepts_plan_and_full_chain_validation(self) -> None:
        plan = parse_args(["validate-plan", "dataset", "run"])
        chain = parse_args(
            [
                "validate-chain",
                "dataset",
                "run",
                "worker",
                "execution",
                "rating",
                "adjudication",
                "latency",
                "qualification",
            ]
        )

        self.assertEqual(Path("dataset"), plan.dataset_root)
        self.assertEqual(Path("run"), plan.run_root)
        self.assertEqual(Path("qualification"), chain.qualification_root)

    def test_bundle_validation_is_python_39_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_raw = canonical_json({}, pretty=True)
            (root / "public-result.json").write_bytes(result_raw)
            manifest = {
                "schema_version": "officelife-track-b-public-manifest-v1",
                "files": [
                    {
                        "path": "public-result.json",
                        "role": "public-result",
                        "sha256": sha256_bytes(result_raw),
                        "size_bytes": len(result_raw),
                    }
                ],
            }
            (root / "public-manifest.json").write_bytes(
                canonical_json(manifest, pretty=True)
            )

            report = validate_public_bundle(root)

        self.assertFalse(report["passed"])
        self.assertIn(
            "public-manifest.json# schema_validation_failed",
            report["errors"],
        )

    def test_rating_bundle_closes_assignments_submissions_and_raters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_rating_bundle(root)
            valid = validate_rating_bundle(root)
        self.assertTrue(valid["passed"], valid)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_rating_bundle(root, second_rater="rater-a")
            invalid = validate_rating_bundle(root)
        self.assertFalse(invalid["passed"])
        self.assertIn(
            "rating-assignments.jsonl# repeated_item_rater",
            invalid["errors"],
        )

    def test_public_bundle_validates_the_inventoried_result_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_raw = canonical_json({}, pretty=True)
            (root / "public-result.json").write_bytes(result_raw)
            manifest = public_manifest_for(result_raw)
            (root / "public-manifest.json").write_bytes(
                canonical_json(manifest, pretty=True)
            )

            report = validate_public_bundle(root)

        self.assertFalse(report["passed"])
        self.assertIn(
            "public-result.json# schema_validation_failed",
            report["errors"],
        )

    def test_projected_public_bundle_passes_structural_validation(self) -> None:
        from tests.test_officelife_track_b_qualification_bundles import (
            _write_public_bundle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trust_anchors, _private_key = _write_public_bundle(root)

            report = validate_public_bundle(root, **trust_anchors)

        self.assertTrue(report["passed"], report)
        self.assertTrue(report["qualification_eligible"])
        self.assertTrue(report["claimable"])


if __name__ == "__main__":
    unittest.main()
