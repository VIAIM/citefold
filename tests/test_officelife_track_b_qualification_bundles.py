from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Callable

from benchmarks.officelife_track_b_qualification import (
    BOOTSTRAP_PRNG,
    BOOTSTRAP_QUANTILE,
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    CUSTODIAN_PUBLIC_KEY_SCHEMA_VERSION,
    PUBLIC_PROJECTOR_VERSION,
    PUBLICATION_CONTEXT_SCHEMA_VERSION,
    QUALIFICATION_CONTRACT_VERSION,
    QUALIFICATION_PLAN_SCHEMA_VERSION,
    ScoredPair,
    build_private_summary,
    canonical_json,
    derive_citation_metrics,
    derive_safety_metrics,
    parse_args,
    prepare_public_result_draft,
    publication_context_artifact,
    public_result_projection,
    sha256_bytes,
    summarize_pairs,
    validate_adjudication_bundle,
    validate_public_bundle,
    validate_qualification_bundle,
)


TIMESTAMP = "2026-08-04T00:00:00Z"
NONQUALIFICATION_REASON = "current_executor_nonqualifying"

ROLE_HASH_FIELDS = {
    "adjudication-manifest.json": {
        "rating-submissions": "rating_submissions_sha256",
        "deterministic-assessments": "deterministic_assessments_sha256",
        "safety-reviews": "safety_reviews_sha256",
        "citation-assessments": "citation_assessments_sha256",
        "arm-measurements": "arm_measurements_sha256",
        "adjudicated-arms": "adjudicated_arms_sha256",
    },
    "qualification-manifest.json": {
        "adjudicated-arms": "adjudicated_arms_sha256",
        "scored-arms": "scored_arms_sha256",
        "scored-pairs": "scored_pairs_sha256",
        "gate-results": "gate_results_sha256",
        "private-summary": "private_summary_sha256",
    },
    "public-manifest.json": {
        "public-result": "public_result_sha256",
        "publication-context": "publication_context_sha256",
        "publication-receipt": "publication_receipt_sha256",
        "qualification-plan": "qualification_plan_sha256",
        "custodian-public-key": "custodian_public_key_artifact_sha256",
    },
}


def _write_json(path: Path, value: dict) -> bytes:
    raw = canonical_json(value, pretty=True)
    path.write_bytes(raw)
    return raw


def _write_jsonl(path: Path, records: list[dict]) -> bytes:
    raw = b"".join(canonical_json(record) + b"\n" for record in records)
    path.write_bytes(raw)
    return raw


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _inventory_entry(
    path: str,
    role: str,
    schema_version: str,
    raw: bytes,
    record_count: int,
    *,
    public: bool = False,
) -> dict:
    entry = {
        "path": path,
        "role": role,
        "artifact_kind": "jsonl-records" if path.endswith(".jsonl") else "json-document",
        "sha256": sha256_bytes(raw),
        "size_bytes": len(raw),
        "schema_version": schema_version,
        "record_count": record_count,
        "sensitivity": "public" if public else "restricted",
        "access_class": "public_projection" if public else "custodian_only",
        "media_type": "application/x-ndjson" if path.endswith(".jsonl") else "application/json",
    }
    if public:
        entry["artifact_id"] = role
    return entry


def _refresh_manifest(root: Path, manifest_name: str) -> None:
    manifest_path = root / manifest_name
    manifest = _read_json(manifest_path)
    for entry in manifest["files"]:
        raw = (root / entry["path"]).read_bytes()
        entry["sha256"] = sha256_bytes(raw)
        entry["size_bytes"] = len(raw)
        entry["record_count"] = (
            len(raw.splitlines()) if entry["path"].endswith(".jsonl") else 1
        )
        hash_field = ROLE_HASH_FIELDS.get(manifest_name, {}).get(entry["role"])
        if hash_field is not None:
            manifest[hash_field] = sha256_bytes(raw)
        if manifest_name == "adjudication-manifest.json" and entry["role"] == "adjudication-audit":
            manifest["audit_sha256"] = sha256_bytes(raw)
    _write_json(manifest_path, manifest)


def _empty_safety_reviews(task_id: str) -> list[dict]:
    categories = (
        "unsupported_memory",
        "stale_or_superseded",
        "no_evidence_false_answer",
        "deletion_violation",
        "cross_scope_leakage",
        "partial_asr_durable_commit",
    )
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
            "reviewed_at": TIMESTAMP,
        }
        for arm in ("no_memory", "memory_pack")
        for category in categories
    ]


def _deterministic_assessment(arm: str, verdict: str) -> dict:
    return {
        "schema_version": "officelife-track-b-deterministic-assessment-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "assessment_id": f"assessment-{arm}",
        "task_id": "task-01",
        "blinded_output_id": f"blind-{arm}",
        "check_id": "check-main",
        "output_sha256": "1" * 64,
        "label_sha256": "2" * 64,
        "check_sha256": "3" * 64,
        "parser_id": "exact-parser",
        "parser_version": "v1",
        "parser_sha256": "4" * 64,
        "verdict": verdict,
        "evidence_spans": [],
        "evaluated_at": TIMESTAMP,
    }


def _arm_measurement(arm: str) -> dict:
    is_memory = arm == "memory_pack"
    return {
        "schema_version": "officelife-track-b-arm-measurement-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "measurement_id": f"measurement-{arm}",
        "task_id": "task-01",
        "blinded_output_id": f"blind-{arm}",
        "context_characters": 100,
        "input_tokens": 25,
        "output_tokens": 4,
        "ingest_latency_ms": 10.0 if is_memory else None,
        "recall_latency_ms": 20.0 if is_memory else None,
        "end_to_end_latency_ms": 100.0,
        "reader_cost_usd": None,
        "total_cost_usd": None,
        "measured_at": TIMESTAMP,
    }


def _adjudicated_arm(arm: str, success: bool) -> dict:
    return {
        "schema_version": "officelife-track-b-adjudicated-arm-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "adjudication_id": f"adjudication-{arm}",
        "execution_id": f"execution-{arm}",
        "task_id": "task-01",
        "arm": arm,
        "blinded_output_id": f"blind-{arm}",
        "outcome": "answer",
        "deterministic_assessment_ids": [f"assessment-{arm}"],
        "rating_submission_ids": [],
        "safety_finding_ids": [],
        "citation_assessment_ids": [],
        "arm_measurement_id": f"measurement-{arm}",
        "deterministic_all_passed": success,
        "hard_prohibition_fired": False,
        "final_human_judgment": "not_required",
        "scored_product_failure": False,
        "adjudication_complete": True,
        "task_success": int(success),
        "adjudicated_at": TIMESTAMP,
    }


def _write_adjudication_bundle(root: Path) -> None:
    assessments = [
        _deterministic_assessment("no_memory", "fail"),
        _deterministic_assessment("memory_pack", "pass"),
    ]
    safety_reviews = _empty_safety_reviews("task-01")
    citations: list[dict] = []
    measurements = [
        _arm_measurement("no_memory"),
        _arm_measurement("memory_pack"),
    ]
    adjudicated_arms = [
        _adjudicated_arm("no_memory", False),
        _adjudicated_arm("memory_pack", True),
    ]
    artifacts = {
        "rating-submissions": (
            "rating-submissions.jsonl",
            "officelife-track-b-rating-submission-v1",
            _write_jsonl(root / "rating-submissions.jsonl", []),
            0,
        ),
        "deterministic-assessments": (
            "deterministic-assessments.jsonl",
            "officelife-track-b-deterministic-assessment-v1",
            _write_jsonl(root / "deterministic-assessments.jsonl", assessments),
            len(assessments),
        ),
        "safety-reviews": (
            "safety-reviews.jsonl",
            "officelife-track-b-safety-review-v1",
            _write_jsonl(root / "safety-reviews.jsonl", safety_reviews),
            len(safety_reviews),
        ),
        "citation-assessments": (
            "citation-assessments.jsonl",
            "officelife-track-b-citation-assessment-v1",
            _write_jsonl(root / "citation-assessments.jsonl", citations),
            0,
        ),
        "arm-measurements": (
            "arm-measurements.jsonl",
            "officelife-track-b-arm-measurement-v1",
            _write_jsonl(root / "arm-measurements.jsonl", measurements),
            len(measurements),
        ),
        "adjudicated-arms": (
            "adjudicated-arms.jsonl",
            "officelife-track-b-adjudicated-arm-v1",
            _write_jsonl(root / "adjudicated-arms.jsonl", adjudicated_arms),
            len(adjudicated_arms),
        ),
    }
    audit_raw = _write_jsonl(
        root / "adjudication-audit.jsonl",
        [
            {
                "schema_version": "officelife-track-b-adjudication-audit-v1",
                "sequence": 1,
                "event": "adjudication_complete",
                "recorded_at": TIMESTAMP,
                "previous_record_sha256": None,
                "payload_sha256": sha256_bytes(
                    artifacts["adjudicated-arms"][2]
                ),
            }
        ],
    )
    files = [
        _inventory_entry(path, role, schema_version, raw, count)
        for role, (path, schema_version, raw, count) in artifacts.items()
    ]
    files.append(
        _inventory_entry(
            "adjudication-audit.jsonl",
            "adjudication-audit",
            "officelife-track-b-adjudication-audit-v1",
            audit_raw,
            1,
        )
    )
    manifest = {
        "schema_version": "officelife-track-b-adjudication-manifest-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "generated_at": TIMESTAMP,
        "previous_adjudication_manifest_sha256": None,
        "qualification_plan_sha256": "5" * 64,
        "execution_manifest_sha256": "6" * 64,
        "rating_manifest_sha256": "7" * 64,
        "rating_submissions_sha256": sha256_bytes(
            artifacts["rating-submissions"][2]
        ),
        "deterministic_assessments_sha256": sha256_bytes(
            artifacts["deterministic-assessments"][2]
        ),
        "safety_reviews_sha256": sha256_bytes(artifacts["safety-reviews"][2]),
        "citation_assessments_sha256": sha256_bytes(
            artifacts["citation-assessments"][2]
        ),
        "arm_measurements_sha256": sha256_bytes(artifacts["arm-measurements"][2]),
        "adjudicated_arms_sha256": sha256_bytes(artifacts["adjudicated-arms"][2]),
        "audit_sha256": sha256_bytes(audit_raw),
        "files": files,
        "qualification_eligible": False,
        "nonqualification_reasons": [NONQUALIFICATION_REASON],
        "claimable": False,
    }
    _write_json(root / "adjudication-manifest.json", manifest)


def _scored_arm(arm: str, success: bool) -> dict:
    return {
        "schema_version": "officelife-track-b-scored-arm-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "scored_arm_id": f"scored-arm-{arm}",
        "adjudication_id": f"adjudication-{arm}",
        "task_id": "task-01",
        "user_id": "user-01",
        "arm": arm,
        "blinded_output_id": f"blind-{arm}",
        "memory_requirement": "required",
        "scenario_family": "stable_preferences",
        "surface_memberships": ["text_chat"],
        "task_success": int(success),
        "unsupported_memory": False,
        "stale_or_superseded": False,
        "no_evidence_false_answer": False,
        "deletion_violation": False,
        "cross_scope_leakage": False,
        "partial_asr_durable_commit": False,
        "applicable_safety_finding_count": 0,
        "correct_citation_links": 0,
        "emitted_citation_links": 0,
        "covered_required_facts": 0,
        "source_required_facts": 0,
        "scored_at": TIMESTAMP,
    }


def _scored_pair() -> dict:
    return {
        "schema_version": "officelife-track-b-scored-pair-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "scored_pair_id": "scored-pair-task-01",
        "task_id": "task-01",
        "user_id": "user-01",
        "memory_requirement": "required",
        "scenario_family": "stable_preferences",
        "surface_memberships": ["text_chat"],
        "history_length_bin": "50-99",
        "no_memory_scored_arm_id": "scored-arm-no_memory",
        "memory_pack_scored_arm_id": "scored-arm-memory_pack",
        "no_memory_success": 0,
        "memory_pack_success": 1,
        "paired_difference": 1,
        "complete_pair": True,
        "scored_at": TIMESTAMP,
    }


def _write_qualification_bundle(
    root: Path,
    *,
    eligible: bool = False,
    qualification_plan_sha256: str = "5" * 64,
) -> None:
    adjudicated_arms = [
        _adjudicated_arm("no_memory", False),
        _adjudicated_arm("memory_pack", True),
    ]
    scored_arms = [
        _scored_arm("no_memory", False),
        _scored_arm("memory_pack", True),
    ]
    scored_pair_record = _scored_pair()
    adjudicated_arms_raw = _write_jsonl(
        root / "adjudicated-arms.jsonl", adjudicated_arms
    )
    scored_arms_raw = _write_jsonl(root / "scored-arms.jsonl", scored_arms)
    scored_pairs_raw = _write_jsonl(
        root / "scored-pairs.jsonl", [scored_pair_record]
    )
    pair = ScoredPair(
        task_id="task-01",
        user_id="user-01",
        no_memory_success=False,
        memory_pack_success=True,
        scenario_family="stable_preferences",
        surface_memberships=("text_chat",),
        memory_requirement="required",
        history_length=50,
    )
    metrics, _replicates = summarize_pairs([pair])
    safety = derive_safety_metrics(_empty_safety_reviews("task-01"), [pair])
    summary = build_private_summary(
        [pair],
        metrics=metrics,
        safety=safety,
        citations=derive_citation_metrics([]),
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
            "reference_environment_sha256": "8" * 64,
        },
        artifact_hashes={
            "qualification_plan_sha256": qualification_plan_sha256,
            "execution_manifest_sha256": "6" * 64,
            "rating_manifest_sha256": "7" * 64,
            "adjudication_manifest_sha256": "c" * 64,
            "latency_manifest_sha256": "8" * 64,
            "scored_arms_sha256": sha256_bytes(scored_arms_raw),
            "scored_pairs_sha256": sha256_bytes(scored_pairs_raw),
        },
        generated_at=TIMESTAMP,
        eligibility_reports={
            "upstream": {
                "validation_passed": True,
                "upstream_ready_for_qualification": eligible,
                "nonqualification_reasons": [] if eligible else [NONQUALIFICATION_REASON],
            },
            "rating": {
                "passed": True,
                "qualification_eligible": eligible,
                "nonqualification_reasons": [] if eligible else [NONQUALIFICATION_REASON],
            },
            "adjudication": {
                "passed": True,
                "qualification_eligible": eligible,
                "nonqualification_reasons": [] if eligible else [NONQUALIFICATION_REASON],
            },
            "latency": {
                "passed": True,
                "qualification_eligible": eligible,
                "nonqualification_reasons": [] if eligible else [NONQUALIFICATION_REASON],
            },
        },
    )
    gate_results_raw = _write_jsonl(root / "gate-results.jsonl", summary["gates"])
    private_summary_raw = _write_json(root / "private-summary.json", summary)
    files = [
        _inventory_entry(
            "adjudicated-arms.jsonl",
            "adjudicated-arms",
            "officelife-track-b-adjudicated-arm-v1",
            adjudicated_arms_raw,
            len(adjudicated_arms),
        ),
        _inventory_entry(
            "scored-arms.jsonl",
            "scored-arms",
            "officelife-track-b-scored-arm-v1",
            scored_arms_raw,
            len(scored_arms),
        ),
        _inventory_entry(
            "scored-pairs.jsonl",
            "scored-pairs",
            "officelife-track-b-scored-pair-v1",
            scored_pairs_raw,
            1,
        ),
        _inventory_entry(
            "gate-results.jsonl",
            "gate-results",
            "officelife-track-b-gate-result-v1",
            gate_results_raw,
            len(summary["gates"]),
        ),
        _inventory_entry(
            "private-summary.json",
            "private-summary",
            "officelife-track-b-private-summary-v1",
            private_summary_raw,
            1,
        ),
    ]
    manifest = {
        "schema_version": "officelife-track-b-qualification-manifest-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "generated_at": TIMESTAMP,
        "previous_qualification_manifest_sha256": None,
        "qualification_plan_sha256": qualification_plan_sha256,
        "dataset_manifest_sha256": "9" * 64,
        "sealed_run_manifest_sha256": "a" * 64,
        "execution_manifest_sha256": "6" * 64,
        "rating_manifest_sha256": "7" * 64,
        "adjudication_manifest_sha256": "c" * 64,
        "latency_manifest_sha256": "8" * 64,
        "adjudicated_arms_sha256": sha256_bytes(adjudicated_arms_raw),
        "scored_arms_sha256": sha256_bytes(scored_arms_raw),
        "scored_pairs_sha256": sha256_bytes(scored_pairs_raw),
        "private_summary_sha256": sha256_bytes(private_summary_raw),
        "gate_results_sha256": sha256_bytes(gate_results_raw),
        "custodian_attestation_sha256": "b" * 64,
        "qualification_status": summary["qualification_status"],
        "qualification_complete": summary["qualification_complete"],
        "all_gates_passed": (
            True
            if summary["qualification_status"] == "passed"
            else False if summary["qualification_status"] == "failed" else None
        ),
        "files": files,
        "qualification_eligible": summary["qualification_eligible"],
        "nonqualification_reasons": summary["nonqualification_reasons"],
        "claimable": summary["claimable"],
    }
    _write_json(root / "qualification-manifest.json", manifest)


def _mutate_jsonl(
    root: Path,
    path: str,
    mutator: Callable[[list[dict]], None],
) -> None:
    records = _read_jsonl(root / path)
    mutator(records)
    _write_jsonl(root / path, records)


def _mutate_json(root: Path, path: str, mutator: Callable[[dict], None]) -> None:
    value = _read_json(root / path)
    mutator(value)
    _write_json(root / path, value)


def _bind_scored_hash_into_summary(root: Path, role: str) -> None:
    path = "scored-arms.jsonl" if role == "scored-arms" else "scored-pairs.jsonl"
    field = "scored_arms_sha256" if role == "scored-arms" else "scored_pairs_sha256"
    digest = sha256_bytes((root / path).read_bytes())
    _mutate_json(root, "private-summary.json", lambda value: value.__setitem__(field, digest))


def _publication_context() -> dict:
    return {
        "generated_at": "2026-08-04T01:00:00Z",
        "evaluation_started_at": TIMESTAMP,
        "evaluation_ended_at": "2026-08-04T00:30:00Z",
        "dataset_release_sha256": "c" * 64,
        "release_artifact_sha256": "d" * 64,
        "commit_id": "e" * 40,
        "configuration_hashes": [{"name": "prompt", "sha256": "1" * 64}],
        "model_provider_role_hashes": [
            {"name": "reader", "sha256": "2" * 64}
        ],
        "environment": {
            "environment_sha256": "3" * 64,
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


def _qualification_plan(custodian_public_key_sha256: str) -> dict:
    return {
        "schema_version": QUALIFICATION_PLAN_SCHEMA_VERSION,
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "artifact_contract_version": "officelife-track-b-artifact-contract-v1",
        "executor_contract_version": "officelife-track-b-executor-v1",
        "execution_profile_version": "officelife-track-b-execution-profile-v1",
        "generated_at": TIMESTAMP,
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
        "custodian_public_key_sha256": custodian_public_key_sha256,
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


def _generate_ed25519_keypair() -> tuple[bytes, bytes]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        private_path = root / "private.pem"
        public_path = root / "public.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "ED25519",
                "-out",
                str(private_path),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(private_path),
                "-pubout",
                "-out",
                str(public_path),
            ],
            check=True,
            capture_output=True,
        )
        return private_path.read_bytes(), public_path.read_bytes()


def _sign_publication_receipt(payload: dict, private_key_pem: bytes) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        private_path = root / "private.pem"
        payload_path = root / "receipt.json"
        signature_path = root / "receipt.sig"
        private_path.write_bytes(private_key_pem)
        payload_path.write_bytes(canonical_json(payload))
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(private_path),
                "-rawin",
                "-in",
                str(payload_path),
                "-out",
                str(signature_path),
            ],
            check=True,
            capture_output=True,
        )
        signature = base64.urlsafe_b64encode(signature_path.read_bytes()).rstrip(b"=")
    return {**payload, "signature": signature.decode("ascii")}


def _claimable_projection_inputs(
    qualification_root: Path,
) -> tuple[dict, dict, bytes, dict, bytes]:
    private_key_pem, public_key_pem = _generate_ed25519_keypair()
    public_key_sha256 = sha256_bytes(public_key_pem)
    plan = _qualification_plan(public_key_sha256)
    plan_sha256 = sha256_bytes(canonical_json(plan, pretty=True))
    _write_qualification_bundle(
        qualification_root,
        eligible=True,
        qualification_plan_sha256=plan_sha256,
    )
    context = _publication_context()
    context_artifact = {
        "schema_version": PUBLICATION_CONTEXT_SCHEMA_VERSION,
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        **context,
    }
    receipt_payload = {
        "schema_version": "officelife-track-b-publication-receipt-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "approval_id": "approval-track-b-v1",
        "approver_id": "custodian-01",
        "publication_status": "approved",
        "approved_at": TIMESTAMP,
        "qualification_plan_sha256": plan_sha256,
        "qualification_manifest_sha256": sha256_bytes(
            (qualification_root / "qualification-manifest.json").read_bytes()
        ),
        "private_summary_sha256": sha256_bytes(
            (qualification_root / "private-summary.json").read_bytes()
        ),
        "publication_context_sha256": "0" * 64,
        "public_result_sha256": "0" * 64,
        "public_projector_version": PUBLIC_PROJECTOR_VERSION,
        "public_projector_sha256": sha256_bytes(
            Path(public_result_projection.__code__.co_filename).read_bytes()
        ),
        "custodian_public_key_sha256": public_key_sha256,
        "signature_algorithm": "ed25519-openssl-v1",
    }
    draft = prepare_public_result_draft(
        qualification_root,
        plan,
        public_key_pem,
        context,
    )
    receipt_payload["publication_context_sha256"] = sha256_bytes(
        canonical_json(context_artifact, pretty=True)
    )
    receipt_payload["public_result_sha256"] = sha256_bytes(
        canonical_json(draft["public_result"], pretty=True)
    )
    receipt = _sign_publication_receipt(receipt_payload, private_key_pem)
    return plan, receipt, public_key_pem, context, private_key_pem


def _write_public_bundle(root: Path) -> tuple[dict[str, str], bytes]:
    with tempfile.TemporaryDirectory() as private_tmp:
        private_root = Path(private_tmp)
        plan, receipt, public_key, context, private_key = _claimable_projection_inputs(
            private_root
        )
        result = public_result_projection(
            private_root,
            plan,
            receipt,
            public_key,
            context,
        )
        qualification_manifest_sha256 = sha256_bytes(
            (private_root / "qualification-manifest.json").read_bytes()
        )
        private_summary_sha256 = sha256_bytes(
            (private_root / "private-summary.json").read_bytes()
        )
    context_artifact = publication_context_artifact(context)
    public_key_artifact = {
        "schema_version": CUSTODIAN_PUBLIC_KEY_SCHEMA_VERSION,
        "algorithm": "ed25519",
        "encoding": "pem",
        "public_key_pem": public_key.decode("ascii"),
    }
    result_raw = _write_json(root / "public-result.json", result)
    context_raw = _write_json(root / "publication-context.json", context_artifact)
    receipt_raw = _write_json(root / "publication-receipt.json", receipt)
    plan_raw = _write_json(root / "qualification-plan.json", plan)
    key_raw = _write_json(root / "custodian-public-key.json", public_key_artifact)
    manifest = {
        "schema_version": "officelife-track-b-public-manifest-v1",
        "qualification_contract_version": QUALIFICATION_CONTRACT_VERSION,
        "generated_at": "2026-08-04T01:00:00Z",
        "previous_public_manifest_sha256": None,
        "qualification_manifest_sha256": qualification_manifest_sha256,
        "private_summary_sha256": private_summary_sha256,
        "qualification_plan_sha256": sha256_bytes(plan_raw),
        "publication_context_sha256": sha256_bytes(context_raw),
        "publication_receipt_sha256": sha256_bytes(receipt_raw),
        "custodian_public_key_artifact_sha256": sha256_bytes(key_raw),
        "custodian_public_key_sha256": sha256_bytes(public_key),
        "public_result_sha256": sha256_bytes(result_raw),
        "public_projector_version": PUBLIC_PROJECTOR_VERSION,
        "public_projector_sha256": sha256_bytes(
            Path(public_result_projection.__code__.co_filename).read_bytes()
        ),
        "files": [
            _inventory_entry(
                "public-result.json",
                "public-result",
                "officelife-track-b-public-result-v1",
                result_raw,
                1,
                public=True,
            ),
            _inventory_entry(
                "publication-context.json",
                "publication-context",
                PUBLICATION_CONTEXT_SCHEMA_VERSION,
                context_raw,
                1,
                public=True,
            ),
            _inventory_entry(
                "publication-receipt.json",
                "publication-receipt",
                "officelife-track-b-publication-receipt-v1",
                receipt_raw,
                1,
                public=True,
            ),
            _inventory_entry(
                "qualification-plan.json",
                "qualification-plan",
                QUALIFICATION_PLAN_SCHEMA_VERSION,
                plan_raw,
                1,
                public=True,
            ),
            _inventory_entry(
                "custodian-public-key.json",
                "custodian-public-key",
                CUSTODIAN_PUBLIC_KEY_SCHEMA_VERSION,
                key_raw,
                1,
                public=True,
            ),
        ],
        "qualification_eligible": True,
        "nonqualification_reasons": [],
        "claimable": True,
    }
    _write_json(root / "public-manifest.json", manifest)
    return (
        {
            "trusted_qualification_plan_sha256": sha256_bytes(plan_raw),
            "trusted_custodian_public_key_sha256": sha256_bytes(public_key),
        },
        private_key,
    )


class OfficeLifeTrackBQualificationBundleTest(unittest.TestCase):
    def test_minimal_adjudication_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_adjudication_bundle(root)
            report = validate_adjudication_bundle(root)

        self.assertTrue(report["passed"], report)

    def test_adjudication_rejects_dangling_assessment_and_measurement(self) -> None:
        cases = (
            (
                "assessment",
                lambda records: records[0].__setitem__(
                    "deterministic_assessment_ids", ["missing-assessment"]
                ),
            ),
            (
                "measurement",
                lambda records: records[0].__setitem__(
                    "arm_measurement_id", "missing-measurement"
                ),
            ),
        )
        for name, mutator in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_adjudication_bundle(root)
                _mutate_jsonl(root, "adjudicated-arms.jsonl", mutator)
                _refresh_manifest(root, "adjudication-manifest.json")

                report = validate_adjudication_bundle(root)

                self.assertFalse(report["passed"], report)

    def test_adjudication_rejects_infrastructure_error_contradiction(self) -> None:
        def make_contradictory(records: list[dict]) -> None:
            arm = records[0]
            arm["outcome"] = "infrastructure_error"
            arm["deterministic_all_passed"] = False
            arm["hard_prohibition_fired"] = False
            arm["final_human_judgment"] = "not_evaluable"
            arm["scored_product_failure"] = False
            arm["adjudication_complete"] = False
            arm["task_success"] = None
            # The record remains schema-valid but still claims completed answer
            # assessments and measurements for an output that never existed.

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_adjudication_bundle(root)
            _mutate_jsonl(root, "adjudicated-arms.jsonl", make_contradictory)
            _refresh_manifest(root, "adjudication-manifest.json")

            report = validate_adjudication_bundle(root)

        self.assertFalse(report["passed"], report)

    def test_adjudication_rejects_answer_without_scoring_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_adjudication_bundle(root)
            _mutate_jsonl(
                root,
                "adjudicated-arms.jsonl",
                lambda records: records[1].__setitem__(
                    "deterministic_assessment_ids", []
                ),
            )
            _mutate_jsonl(
                root,
                "deterministic-assessments.jsonl",
                lambda records: records.pop(),
            )
            _refresh_manifest(root, "adjudication-manifest.json")

            report = validate_adjudication_bundle(root)

        self.assertFalse(report["passed"], report)
        self.assertIn(
            "adjudicated-arms.jsonl# answer_has_no_scoring_evidence",
            report["errors"],
        )

    def test_minimal_qualification_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_qualification_bundle(root)
            report = validate_qualification_bundle(root)

        self.assertTrue(report["passed"], report)

    def test_qualification_rejects_dangling_adjudication_and_scored_arm(self) -> None:
        cases = (
            (
                "adjudication",
                "scored-arms.jsonl",
                "scored-arms",
                lambda records: records[0].__setitem__(
                    "adjudication_id", "missing-adjudication"
                ),
            ),
            (
                "scored-arm",
                "scored-pairs.jsonl",
                "scored-pairs",
                lambda records: records[0].__setitem__(
                    "no_memory_scored_arm_id", "missing-scored-arm"
                ),
            ),
        )
        for name, path, role, mutator in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_qualification_bundle(root)
                _mutate_jsonl(root, path, mutator)
                _bind_scored_hash_into_summary(root, role)
                _refresh_manifest(root, "qualification-manifest.json")

                report = validate_qualification_bundle(root)

                self.assertFalse(report["passed"], report)

    def test_qualification_rejects_pair_difference_and_metadata_contradictions(self) -> None:
        cases = (
            (
                "paired-difference",
                lambda records: records[0].__setitem__("paired_difference", 0),
            ),
            (
                "metadata",
                lambda records: records[0].__setitem__(
                    "scenario_family", "correction"
                ),
            ),
        )
        for name, mutator in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_qualification_bundle(root)
                _mutate_jsonl(root, "scored-pairs.jsonl", mutator)
                _bind_scored_hash_into_summary(root, "scored-pairs")
                _refresh_manifest(root, "qualification-manifest.json")

                report = validate_qualification_bundle(root)

                self.assertFalse(report["passed"], report)

    def test_qualification_rejects_private_summary_contradictions(self) -> None:
        def wrong_count(summary: dict) -> None:
            summary["pair_count"] += 1

        def wrong_rate(summary: dict) -> None:
            summary["overall"]["absolute_lift"] = 0.5

        def wrong_gate(summary: dict) -> None:
            gate = next(
                item
                for item in summary["gates"]
                if item["gate_id"] == "overall_task_success_lift"
            )
            gate["observed"] = 0.5

        def wrong_safety(summary: dict) -> None:
            summary["safety"]["finding_count"] = 1

        def wrong_citations(summary: dict) -> None:
            summary["citations"]["precision"]["denominator"] = 1

        def wrong_status(summary: dict) -> None:
            summary["qualification_status"] = "not_evaluable"
            summary["qualification_eligible"] = True

        cases = (
            ("count", wrong_count),
            ("rate", wrong_rate),
            ("gate", wrong_gate),
            ("safety", wrong_safety),
            ("citations", wrong_citations),
            ("status", wrong_status),
        )
        for name, mutator in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_qualification_bundle(root)
                _mutate_json(root, "private-summary.json", mutator)
                _refresh_manifest(root, "qualification-manifest.json")

                report = validate_qualification_bundle(root)

                self.assertFalse(report["passed"], report)

    def test_public_rejects_qualification_manifest_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trust_anchors, _private_key = _write_public_bundle(root)
            valid = validate_public_bundle(root, **trust_anchors)
            self.assertTrue(valid["passed"], valid)

            _mutate_json(
                root,
                "public-manifest.json",
                lambda value: value.__setitem__(
                    "qualification_manifest_sha256", "0" * 64
                ),
            )
            mismatched = validate_public_bundle(root, **trust_anchors)

        self.assertFalse(mismatched["passed"], mismatched)

    def test_public_requires_a_matching_external_trust_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trust_anchors, _private_key = _write_public_bundle(root)

            missing = validate_public_bundle(root)
            wrong_plan = validate_public_bundle(
                root,
                trusted_qualification_plan_sha256="0" * 64,
                trusted_custodian_public_key_sha256=(
                    trust_anchors["trusted_custodian_public_key_sha256"]
                ),
            )

        self.assertFalse(missing["passed"], missing)
        self.assertIn(
            "public-manifest.json# trusted_publication_anchor_required",
            missing["errors"],
        )
        self.assertFalse(wrong_plan["passed"], wrong_plan)
        self.assertIn(
            "public-manifest.json# untrusted_qualification_plan",
            wrong_plan["errors"],
        )

    def test_public_validator_cli_requires_external_trust_anchors(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_args(["validate-public", "public-root"])

        args = parse_args(
            [
                "validate-public",
                "public-root",
                "--trusted-qualification-plan-sha256",
                "1" * 64,
                "--trusted-custodian-public-key-sha256",
                "2" * 64,
            ]
        )

        self.assertEqual("1" * 64, args.trusted_qualification_plan_sha256)
        self.assertEqual("2" * 64, args.trusted_custodian_public_key_sha256)

    def test_public_rejects_resigned_context_result_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trust_anchors, private_key = _write_public_bundle(root)
            _mutate_json(
                root,
                "publication-context.json",
                lambda value: value.__setitem__("commit_id", "a" * 40),
            )
            _mutate_json(
                root,
                "public-result.json",
                lambda value: value.__setitem__("commit_id", "b" * 40),
            )
            receipt = _read_json(root / "publication-receipt.json")
            receipt.pop("signature")
            receipt["publication_context_sha256"] = sha256_bytes(
                (root / "publication-context.json").read_bytes()
            )
            receipt["public_result_sha256"] = sha256_bytes(
                (root / "public-result.json").read_bytes()
            )
            _write_json(
                root / "publication-receipt.json",
                _sign_publication_receipt(receipt, private_key),
            )
            _refresh_manifest(root, "public-manifest.json")

            report = validate_public_bundle(root, **trust_anchors)

        self.assertFalse(report["passed"], report)
        self.assertIn(
            "public-result.json# publication_context_binding_mismatch",
            report["errors"],
        )
        self.assertNotIn("publication-receipt.json# signature_invalid", report["errors"])

    def test_public_rejects_resigned_receipt_for_another_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trust_anchors, private_key = _write_public_bundle(root)
            receipt = _read_json(root / "publication-receipt.json")
            receipt.pop("signature")
            receipt["public_result_sha256"] = "0" * 64
            _write_json(
                root / "publication-receipt.json",
                _sign_publication_receipt(receipt, private_key),
            )
            _refresh_manifest(root, "public-manifest.json")

            report = validate_public_bundle(root, **trust_anchors)

        self.assertFalse(report["passed"], report)
        self.assertIn(
            "publication-receipt.json# evidence_binding_mismatch",
            report["errors"],
        )
        self.assertNotIn("publication-receipt.json# signature_invalid", report["errors"])


if __name__ == "__main__":
    unittest.main()
