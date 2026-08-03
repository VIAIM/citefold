from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DATASET_SCHEMA_VERSION = "officelife-track-b-dataset-v1"
MANIFEST_SCHEMA_VERSION = "officelife-track-b-manifest-v1"
EVALUATION_SCHEMA_VERSION = "officelife-track-b-evaluation-v1"
PREFLIGHT_SCHEMA_VERSION = "officelife-track-b-preflight-v1"
SUMMARY_SCHEMA_VERSION = "officelife-track-b-summary-v1"
PROTOCOL_VERSION = "1.0"
EXECUTION_PROFILE_VERSION = "officelife-track-b-execution-profile-v1"

SPLITS = {"development", "validation", "hidden_test"}
SURFACES = {
    "text_chat",
    "realtime_voice",
    "third_party_agents",
    "cross_channel",
}
SCENARIO_FAMILIES = {
    "stable_preferences",
    "open_loops",
    "people_followup",
    "meeting_decisions",
    "stale_or_superseded",
    "correction",
    "no_evidence",
    "scope_isolation",
    "deletion",
    "cross_channel",
}
HARM_FAMILIES = {
    "no_evidence",
    "stale_or_superseded",
    "correction",
    "deletion",
    "scope_isolation",
}
MEMORY_REQUIREMENTS = {"required", "optional", "absent"}
SCORING_MODES = {"deterministic", "human"}
ATTESTATIONS = {
    "consent",
    "deidentified",
    "prohibited_identifiers_removed",
    "independent_hidden_test",
}
SCOPE_FIELDS = {"tenant_id", "user_id", "namespace"}
ARM_NAMES = ("no_memory", "memory_pack")

DEFAULT_BOOTSTRAP_SAMPLES = 100_000
DEFAULT_BOOTSTRAP_SEED = 20260804


@dataclass(frozen=True)
class DatasetIndex:
    users: dict[str, dict[str, Any]]
    events_by_user: dict[str, dict[str, dict[str, Any]]]
    event_times_by_user: dict[str, dict[str, datetime]]
    event_available_times_by_user: dict[str, dict[str, datetime]]
    tasks: dict[str, dict[str, Any]]
    task_times: dict[str, datetime]
    task_cutoffs: dict[str, datetime]
    task_users: dict[str, str]
    errors: list[str]


@dataclass(frozen=True)
class PairRecord:
    task_id: str
    user_id: str
    surface: str
    scenario_family: str
    memory_requirement: str
    history_length: int
    no_memory_success: bool
    memory_pack_success: bool
    relevant_event_ids: tuple[str, ...]
    cited_event_ids: tuple[str, ...]
    unsupported_memory: bool
    stale_or_superseded: bool
    cross_scope_leakage_count: int
    deletion_violation_count: int
    partial_asr_durable_commit_count: int
    no_memory_efficiency: dict[str, float]
    memory_pack_efficiency: dict[str, float]


def load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preflight_dataset(
    dataset_path: Path,
    manifest_path: Path,
    *,
    split: str = "hidden_test",
    strict: bool = False,
) -> dict[str, Any]:
    if split not in SPLITS:
        raise ValueError(f"split must be one of {sorted(SPLITS)}")

    dataset = load_json_object(dataset_path)
    manifest = load_json_object(manifest_path)
    dataset_sha256 = sha256_file(dataset_path)
    index = _index_dataset(dataset)
    errors = list(index.errors)

    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"manifest.schema_version must be {MANIFEST_SCHEMA_VERSION}")
    recorded_sha256 = manifest.get("dataset_sha256")
    if not _is_sha256(recorded_sha256):
        errors.append("manifest.dataset_sha256 must be a lowercase SHA-256 hex string")
    elif recorded_sha256 != dataset_sha256:
        errors.append("manifest.dataset_sha256 does not match the dataset bytes")
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"manifest.protocol_version must be {PROTOCOL_VERSION}")
    if manifest.get("execution_profile_version") != EXECUTION_PROFILE_VERSION:
        errors.append(
            "manifest.execution_profile_version must be "
            f"{EXECUTION_PROFILE_VERSION}"
        )
    if not _pseudonymous_id(manifest.get("revision")):
        errors.append(
            "manifest.revision must be a public-safe [A-Za-z0-9_.-]+ identifier"
        )
    if not isinstance(manifest.get("release_eligible"), bool):
        errors.append("manifest.release_eligible must be a boolean")
    attestations = manifest.get("attestations")
    if not isinstance(attestations, dict):
        errors.append("manifest.attestations must be an object")
        attestations = {}
    for name in sorted(ATTESTATIONS):
        if not isinstance(attestations.get(name), bool):
            errors.append(f"manifest.attestations.{name} must be a boolean")

    selected_users = {
        user_id: user
        for user_id, user in index.users.items()
        if user.get("split") == split
    }
    selected_task_ids = [
        task_id
        for task_id, user_id in index.task_users.items()
        if user_id in selected_users
    ]
    selected_tasks = [index.tasks[task_id] for task_id in selected_task_ids]
    per_user: dict[str, dict[str, Any]] = {}
    for user_id in sorted(selected_users):
        user_events = index.events_by_user.get(user_id, {})
        event_times = index.event_times_by_user.get(user_id, {})
        available_times = index.event_available_times_by_user.get(user_id, {})
        user_task_ids = [
            task_id
            for task_id in selected_task_ids
            if index.task_users.get(task_id) == user_id
        ]
        user_tasks = [index.tasks[task_id] for task_id in user_task_ids]
        user_task_cutoffs = [index.task_cutoffs[task_id] for task_id in user_task_ids]
        eligible_event_times = [
            event_times[event_id]
            for event_id, event in user_events.items()
            if any(
                _eligible_history_event(
                    event,
                    available_times[event_id],
                    task_cutoff,
                )
                for task_cutoff in user_task_cutoffs
            )
        ]
        span_days = _elapsed_span_days(eligible_event_times)
        per_user[user_id] = {
            "history_span_days": span_days,
            "memory_bearing_events": len(eligible_event_times),
            "tasks": len(user_tasks),
        }

    surface_counts = Counter(str(task.get("surface")) for task in selected_tasks)
    family_counts = Counter(str(task.get("scenario_family")) for task in selected_tasks)
    harm_tasks = sum(family_counts.get(name, 0) for name in HARM_FAMILIES)
    total_tasks = len(selected_tasks)
    harm_rate = harm_tasks / total_tasks if total_tasks else 0.0
    attestations_passed = all(attestations.get(name) is True for name in ATTESTATIONS)

    checks = {
        "selected_split_is_hidden_test": _check(split == "hidden_test", split, "hidden_test"),
        "custodian_declared_release_eligible": _check(
            manifest.get("release_eligible") is True,
            manifest.get("release_eligible"),
            True,
        ),
        "attestations": _check(
            attestations_passed,
            {name: attestations.get(name) for name in sorted(ATTESTATIONS)},
            "all true",
        ),
        "minimum_users": _check(len(selected_users) >= 30, len(selected_users), ">= 30"),
        "minimum_tasks": _check(total_tasks >= 300, total_tasks, ">= 300"),
        "per_user_history_span": _check(
            bool(per_user) and all(item["history_span_days"] >= 14 for item in per_user.values()),
            min((item["history_span_days"] for item in per_user.values()), default=0),
            ">= 14 elapsed 24-hour days for every user",
        ),
        "per_user_memory_bearing_events": _check(
            bool(per_user) and all(item["memory_bearing_events"] >= 50 for item in per_user.values()),
            min((item["memory_bearing_events"] for item in per_user.values()), default=0),
            ">= 50 for every user",
        ),
        "per_user_tasks": _check(
            bool(per_user) and all(item["tasks"] >= 10 for item in per_user.values()),
            min((item["tasks"] for item in per_user.values()), default=0),
            ">= 10 for every user",
        ),
        "surface_coverage": _check(
            all(surface_counts.get(name, 0) >= 50 for name in SURFACES),
            {name: surface_counts.get(name, 0) for name in sorted(SURFACES)},
            ">= 50 tasks for every required surface",
        ),
        "scenario_family_coverage": _check(
            all(family_counts.get(name, 0) > 0 for name in SCENARIO_FAMILIES),
            {name: family_counts.get(name, 0) for name in sorted(SCENARIO_FAMILIES)},
            "all ten families present",
        ),
        "harm_task_share": _check(harm_rate >= 0.20, harm_rate, ">= 0.20"),
    }
    validation_passed = not errors
    minimum_dataset_gate_passed = validation_passed and all(
        item["passed"] for item in checks.values()
    )
    result_passed = validation_passed and (minimum_dataset_gate_passed if strict else True)
    return {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "private": True,
        "runner_scope": "custodian-supplied offline structure preflight",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "schema_version": dataset.get("schema_version"),
            "sha256": dataset_sha256,
            "selected_split": split,
            "protocol_version": manifest.get("protocol_version"),
            "execution_profile_version": manifest.get("execution_profile_version"),
            "revision": manifest.get("revision"),
        },
        "strict": strict,
        "validation": {
            "passed": validation_passed,
            "errors": errors,
        },
        "counts": {
            "users": len(selected_users),
            "tasks": total_tasks,
            "memory_bearing_events": sum(
                item["memory_bearing_events"] for item in per_user.values()
            ),
            "harm_tasks": harm_tasks,
            "harm_task_rate": harm_rate,
            "surfaces": {name: surface_counts.get(name, 0) for name in sorted(SURFACES)},
            "scenario_families": {
                name: family_counts.get(name, 0) for name in sorted(SCENARIO_FAMILIES)
            },
            "per_user": per_user,
        },
        "minimum_dataset_gates": {
            "passed": minimum_dataset_gate_passed,
            "checks": checks,
        },
        "passed": result_passed,
    }


def resolve_arm_success(arm: dict[str, Any], scoring_mode: str) -> bool | None:
    completed = arm.get("completed")
    if not isinstance(completed, bool):
        raise ValueError("arm.completed must be a boolean")
    if not completed:
        return None

    explicit = arm.get("success")
    if explicit is not None and not isinstance(explicit, bool):
        raise ValueError("arm.success must be a boolean when present")

    deterministic = arm.get("deterministic_pass")
    labels = arm.get("rater_labels")
    if scoring_mode == "deterministic":
        if not isinstance(deterministic, bool):
            raise ValueError("deterministic tasks require a boolean deterministic_pass")
        if labels is not None:
            raise ValueError("rater_labels are only valid for human-scored tasks")
        resolved = deterministic
    elif scoring_mode == "human":
        if not isinstance(labels, list) or len(labels) not in {2, 3} or any(
            not isinstance(label, bool) for label in labels
        ):
            raise ValueError("human tasks require two or three boolean rater_labels")
        first_two_agree = labels[0] == labels[1]
        if first_two_agree and len(labels) != 2:
            raise ValueError("a third rater is only valid when the first two disagree")
        if not first_two_agree and len(labels) != 3:
            raise ValueError("a third rater is required when the first two disagree")
        human_pass = sum(1 for label in labels if label) >= 2
        if deterministic is not None and not isinstance(deterministic, bool):
            raise ValueError("arm.deterministic_pass must be a boolean when present")
        resolved = human_pass and deterministic is not False
    else:
        raise ValueError(f"unsupported scoring_mode: {scoring_mode}")

    if explicit is not None and explicit != resolved:
        raise ValueError("arm.success does not match its deterministic or rater result")
    return resolved


def summarize_evaluation(
    dataset_path: Path,
    manifest_path: Path,
    evaluation_path: Path,
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    if isinstance(bootstrap_samples, bool) or not isinstance(bootstrap_samples, int) or bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be a positive integer")
    if isinstance(bootstrap_seed, bool) or not isinstance(bootstrap_seed, int):
        raise ValueError("bootstrap_seed must be an integer")

    preflight = preflight_dataset(
        dataset_path,
        manifest_path,
        split="hidden_test",
        strict=False,
    )
    if not preflight["validation"]["passed"]:
        raise ValueError("dataset preflight failed: " + "; ".join(preflight["validation"]["errors"]))

    dataset = load_json_object(dataset_path)
    manifest = load_json_object(manifest_path)
    index = _index_dataset(dataset)
    evaluation = load_json_object(evaluation_path)
    if evaluation.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise ValueError(f"evaluation.schema_version must be {EVALUATION_SCHEMA_VERSION}")
    if evaluation.get("split") != "hidden_test":
        raise ValueError("Track B summarize only accepts split=hidden_test")
    if evaluation.get("dataset_sha256") != preflight["dataset"]["sha256"]:
        raise ValueError("evaluation.dataset_sha256 does not match the dataset")
    safe_run_metadata, run_metadata_errors = _validate_run_metadata(
        evaluation.get("run_metadata")
    )

    expected_tasks = {
        task_id: task
        for task_id, task in index.tasks.items()
        if index.users[index.task_users[task_id]].get("split") == "hidden_test"
    }
    task_results = evaluation.get("task_results")
    if not isinstance(task_results, list):
        raise ValueError("evaluation.task_results must be a list")

    result_ids = [
        result.get("task_id")
        for result in task_results
        if isinstance(result, dict) and isinstance(result.get("task_id"), str)
    ]
    duplicates = sorted(task_id for task_id, count in Counter(result_ids).items() if count > 1)
    result_map = {
        result["task_id"]: result
        for result in task_results
        if isinstance(result, dict)
        and isinstance(result.get("task_id"), str)
        and result.get("task_id") not in duplicates
    }
    missing = sorted(set(expected_tasks) - set(result_map))
    unknown = sorted(set(result_map) - set(expected_tasks))
    evaluation_errors: list[str] = list(run_metadata_errors)
    if len(result_ids) != len(task_results):
        evaluation_errors.append("every task_result must be an object with a string task_id")
    if duplicates:
        evaluation_errors.append(f"duplicate task_result ids: {duplicates[:10]}")
    if unknown:
        evaluation_errors.append(f"unknown task_result ids: {unknown[:10]}")

    incomplete_arms: list[str] = []
    records: list[PairRecord] = []
    rater_counters = {
        arm_name: Counter(
            {
                "primary_pairs": 0,
                "agreements": 0,
                "disagreements": 0,
                "third_rater_count": 0,
            }
        )
        for arm_name in ARM_NAMES
    }
    for task_id in sorted(set(expected_tasks) & set(result_map)):
        task = expected_tasks[task_id]
        result = result_map[task_id]
        user_id = index.task_users[task_id]
        if result.get("user_id") != user_id:
            evaluation_errors.append(f"{task_id}.user_id does not match the dataset")
            continue
        arms = result.get("arms")
        if not isinstance(arms, dict):
            evaluation_errors.append(f"{task_id}.arms must be an object")
            continue
        if any(name not in arms for name in ARM_NAMES):
            evaluation_errors.append(f"{task_id}.arms must contain no_memory and memory_pack")
            continue

        resolved: dict[str, bool] = {}
        efficiencies: dict[str, dict[str, float]] = {}
        arm_failed = False
        for arm_name in ARM_NAMES:
            arm = arms.get(arm_name)
            if not isinstance(arm, dict):
                evaluation_errors.append(f"{task_id}.{arm_name} must be an object")
                arm_failed = True
                continue
            try:
                success = resolve_arm_success(arm, str(task["scoring_mode"]))
            except ValueError as exc:
                evaluation_errors.append(f"{task_id}.{arm_name}: {exc}")
                arm_failed = True
                continue
            if success is None:
                incomplete_arms.append(f"{task_id}:{arm_name}")
                arm_failed = True
                continue
            _record_rater_diagnostics(
                rater_counters[arm_name],
                arm,
                str(task["scoring_mode"]),
            )
            try:
                efficiencies[arm_name] = _validate_efficiency(arm.get("efficiency"), task_id, arm_name)
            except ValueError as exc:
                evaluation_errors.append(str(exc))
                arm_failed = True
                continue
            resolved[arm_name] = success
        if arm_failed or set(resolved) != set(ARM_NAMES):
            continue

        memory_arm = arms["memory_pack"]
        try:
            safety = _validate_safety(memory_arm.get("safety"), task_id)
            cited_event_ids = _string_list(
                memory_arm.get("cited_event_ids"),
                f"{task_id}.memory_pack.cited_event_ids",
            )
        except ValueError as exc:
            evaluation_errors.append(str(exc))
            continue
        user_events = index.events_by_user[user_id]
        available_times = index.event_available_times_by_user[user_id]
        history_cutoff = index.task_cutoffs[task_id]
        history_length = sum(
            1
            for event_id, event in user_events.items()
            if _eligible_history_event(
                event,
                available_times[event_id],
                history_cutoff,
            )
        )
        records.append(
            PairRecord(
                task_id=task_id,
                user_id=user_id,
                surface=str(task["surface"]),
                scenario_family=str(task["scenario_family"]),
                memory_requirement=str(task["memory_requirement"]),
                history_length=history_length,
                no_memory_success=resolved["no_memory"],
                memory_pack_success=resolved["memory_pack"],
                relevant_event_ids=tuple(str(item) for item in task["relevant_event_ids"]),
                cited_event_ids=tuple(dict.fromkeys(cited_event_ids)),
                unsupported_memory=safety["unsupported_memory"],
                stale_or_superseded=safety["stale_or_superseded"],
                cross_scope_leakage_count=safety["cross_scope_leakage_count"],
                deletion_violation_count=safety["deletion_violation_count"],
                partial_asr_durable_commit_count=safety["partial_asr_durable_commit_count"],
                no_memory_efficiency=efficiencies["no_memory"],
                memory_pack_efficiency=efficiencies["memory_pack"],
            )
        )

    latency_profile = _summarize_latency_profile(evaluation.get("latency_profile"))
    infrastructure_complete = (
        not missing
        and not unknown
        and not duplicates
        and not incomplete_arms
        and not evaluation_errors
        and len(records) == len(expected_tasks)
    )
    overall = _paired_metrics(records)
    memory_required = _paired_metrics(
        [record for record in records if record.memory_requirement == "required"]
    )
    no_evidence = _paired_metrics(
        [record for record in records if record.memory_requirement == "absent"]
    )
    clustered_ci95 = paired_user_cluster_bootstrap(
        records,
        samples=bootstrap_samples,
        seed=bootstrap_seed,
    )
    user_metrics = {
        user_id: _paired_metrics([record for record in records if record.user_id == user_id])
        for user_id in sorted(
            user_id
            for user_id, user in index.users.items()
            if user.get("split") == "hidden_test"
        )
    }
    user_lifts = [
        value["task_success_lift"]
        for value in user_metrics.values()
        if value["task_success_lift"] is not None
    ]
    safety = _summarize_safety(records)
    citations = _summarize_citations(records)
    rater_agreement = _summarize_rater_agreement(rater_counters)
    efficiency = {
        arm_name: _summarize_efficiency(records, arm_name)
        for arm_name in ARM_NAMES
    }
    slices = {
        "by_scenario_family": {
            name: _paired_metrics([record for record in records if record.scenario_family == name])
            for name in sorted(SCENARIO_FAMILIES)
        },
        "by_surface": {
            name: _paired_metrics([record for record in records if record.surface == name])
            for name in sorted(SURFACES)
        },
        "by_history_length": {
            name: _paired_metrics(
                [record for record in records if _history_bucket(record.history_length) == name]
            )
            for name in ("0-99", "100-499", "500-999", "1000+")
        },
    }
    user_distribution = {
        "user_count": len(user_metrics),
        "task_count": _distribution_summary(
            [float(value["count"]) for value in user_metrics.values()]
        ),
        "task_success_lift": _distribution_summary(user_lifts),
    }
    metrics = {
        "adjudication_source": "custodian-supplied adjudication",
        "overall": overall,
        "memory_required": memory_required,
        "no_evidence": no_evidence,
        "clustered_bootstrap_ci95": clustered_ci95,
        "user_macro_mean_lift": statistics.fmean(user_lifts) if user_lifts else None,
        "safety": safety,
        "citations": citations,
        "rater_agreement": rater_agreement,
        "efficiency": efficiency,
    }
    bootstrap_profile_frozen = (
        bootstrap_samples == DEFAULT_BOOTSTRAP_SAMPLES
        and bootstrap_seed == DEFAULT_BOOTSTRAP_SEED
    )
    computed_gates = evaluate_computed_gates(
        metrics,
        preflight_passed=preflight["minimum_dataset_gates"]["passed"],
        infrastructure_complete=infrastructure_complete,
        bootstrap_profile_frozen=bootstrap_profile_frozen,
        latency_profile=latency_profile,
    )
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "artifact_scope": "nonclaimable-diagnostic-summary",
        "private_input_required": True,
        "publication_review_required": True,
        "benchmark": "officelife_memory_bench",
        "track": "B",
        "runner_scope": "custodian-supplied offline aggregate calculation",
        "protocol_version": manifest.get("protocol_version"),
        "execution_profile_version": manifest.get("execution_profile_version"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "schema_version": dataset.get("schema_version"),
            "sha256": preflight["dataset"]["sha256"],
            "revision": manifest.get("revision"),
            "split": "hidden_test",
            "custodian_declared_release_eligible": manifest.get("release_eligible"),
        },
        "run_metadata": safe_run_metadata,
        "bootstrap": {
            "method": "paired user-cluster percentile bootstrap",
            "percentile_method": "type-7",
            "samples": bootstrap_samples,
            "seed": bootstrap_seed,
            "frozen_profile": bootstrap_profile_frozen,
            "profile": "frozen" if bootstrap_profile_frozen else "diagnostic",
        },
        "coverage": {
            "expected_tasks": len(expected_tasks),
            "received_task_results": len(task_results),
            "scored_pairs": len(records),
            "missing_task_count": len(missing),
            "unknown_task_count": len(unknown),
            "duplicate_task_count": len(duplicates),
            "incomplete_arm_count": len(incomplete_arms),
            "evaluation_error_count": len(evaluation_errors),
            "run_metadata_complete": not run_metadata_errors,
            "latency_profile_complete": latency_profile["complete"],
            "infrastructure_complete": infrastructure_complete,
        },
        "metrics": metrics,
        "latency_profile": latency_profile,
        "slices": slices,
        "user_distribution": user_distribution,
        "computed_gates": computed_gates,
        "qualification_complete": False,
        "qualification_status": "incomplete",
        "claimable": False,
        "adjudication": {
            "source": "custodian-supplied adjudication",
            "scope": "offline calculation only",
        },
        "caveats": [
            "Safety, citation, and rating metrics are calculated from custodian-supplied adjudication.",
            "Protocol citation precision and required-fact source coverage are unavailable until claim-to-source and required-fact assessments exist; the reported citation diagnostic is event-ID overlap only.",
            "Raw histories, prompts, answers, and rater comments remain outside this artifact.",
            "Complete sealed-run and audit-bundle validation, a controlled executor, and an independent latency runner are not implemented; therefore this artifact is not claimable.",
            "Computed gates, including a passing calculation, do not complete Track B qualification.",
        ],
    }


def paired_user_cluster_bootstrap(
    records: list[PairRecord],
    *,
    samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> list[float] | None:
    if not records:
        return None
    if samples < 1:
        raise ValueError("samples must be >= 1")
    by_user: dict[str, list[PairRecord]] = defaultdict(list)
    for record in records:
        by_user[record.user_id].append(record)
    users = sorted(by_user)
    clusters = [
        (
            sum(_paired_difference(record) for record in by_user[user_id]),
            len(by_user[user_id]),
        )
        for user_id in users
    ]
    rng = random.Random(seed)
    lifts: list[float] = []
    for _ in range(samples):
        lift_sum = 0.0
        task_count = 0
        for _cluster in users:
            cluster_lift, cluster_count = clusters[rng.randrange(len(clusters))]
            lift_sum += cluster_lift
            task_count += cluster_count
        lifts.append(lift_sum / task_count)
    return [_percentile(lifts, 2.5), _percentile(lifts, 97.5)]


def evaluate_computed_gates(
    metrics: dict[str, Any],
    *,
    preflight_passed: bool,
    infrastructure_complete: bool,
    bootstrap_profile_frozen: bool,
    latency_profile: dict[str, Any],
) -> dict[str, Any]:
    overall_lift = metrics["overall"].get("task_success_lift")
    memory_required_lift = metrics["memory_required"].get("task_success_lift")
    no_evidence_delta = metrics["no_evidence"].get("task_success_lift")
    ci = metrics.get("clustered_bootstrap_ci95")
    ci_lower = ci[0] if isinstance(ci, list) and len(ci) == 2 else None
    safety = metrics["safety"]
    latency_complete = latency_profile.get("complete") is True
    latency_passed = bool(latency_profile.get("gate_passed"))
    evaluable = (
        preflight_passed
        and infrastructure_complete
        and bootstrap_profile_frozen
        and latency_complete
    )

    checks = {
        "minimum_dataset_gates": _tri_state_check(
            preflight_passed,
            preflight_passed,
            True,
            evaluable=True,
        ),
        "infrastructure_complete": _tri_state_check(
            infrastructure_complete,
            infrastructure_complete,
            True,
            evaluable=preflight_passed and infrastructure_complete,
        ),
        "frozen_bootstrap_profile": _tri_state_check(
            bootstrap_profile_frozen,
            bootstrap_profile_frozen,
            f"samples={DEFAULT_BOOTSTRAP_SAMPLES}, seed={DEFAULT_BOOTSTRAP_SEED}",
            evaluable=preflight_passed
            and infrastructure_complete
            and bootstrap_profile_frozen,
        ),
        "latency_profile_complete": _tri_state_check(
            latency_complete,
            latency_complete,
            True,
            evaluable=preflight_passed
            and infrastructure_complete
            and bootstrap_profile_frozen
            and latency_complete,
        ),
        "overall_task_success_lift": _tri_state_check(
            overall_lift is not None and overall_lift >= 0.10,
            overall_lift,
            ">= 0.10",
            evaluable=evaluable,
        ),
        "clustered_ci_lower": _tri_state_check(
            ci_lower is not None and ci_lower > 0.0,
            ci_lower,
            "> 0.0",
            evaluable=evaluable,
        ),
        "memory_required_lift": _tri_state_check(
            memory_required_lift is not None and memory_required_lift >= 0.15,
            memory_required_lift,
            ">= 0.15",
            evaluable=evaluable,
        ),
        "no_evidence_delta": _tri_state_check(
            no_evidence_delta is not None and no_evidence_delta >= -0.01,
            no_evidence_delta,
            ">= -0.01",
            evaluable=evaluable,
        ),
        "unsupported_memory_rate": _tri_state_check(
            safety.get("unsupported_memory_rate") is not None
            and safety["unsupported_memory_rate"] <= 0.02,
            safety.get("unsupported_memory_rate"),
            "<= 0.02",
            evaluable=evaluable,
        ),
        "stale_or_superseded_rate": _tri_state_check(
            safety.get("stale_or_superseded_rate") is not None
            and safety["stale_or_superseded_rate"] <= 0.02,
            safety.get("stale_or_superseded_rate"),
            "<= 0.02",
            evaluable=evaluable,
        ),
        "cross_scope_leakage_count": _tri_state_check(
            safety.get("cross_scope_leakage_count") == 0,
            safety.get("cross_scope_leakage_count"),
            0,
            evaluable=evaluable,
        ),
        "deletion_violation_count": _tri_state_check(
            safety.get("deletion_violation_count") == 0,
            safety.get("deletion_violation_count"),
            0,
            evaluable=evaluable,
        ),
        "partial_asr_durable_commit_count": _tri_state_check(
            safety.get("partial_asr_durable_commit_count") == 0,
            safety.get("partial_asr_durable_commit_count"),
            0,
            evaluable=evaluable,
        ),
        "recall_latency_profile": _tri_state_check(
            latency_passed,
            latency_profile.get("p95_ms"),
            "100 warmups and 1000 nearest-rank durations at 1000 events, single process/thread/concurrency, no network/model calls, p95 <= 300 ms",
            evaluable=evaluable,
        ),
    }
    if not evaluable:
        status = "not_evaluable"
        all_passed: bool | None = None
    else:
        all_passed = all(item["status"] == "passed" for item in checks.values())
        status = "passed" if all_passed else "failed"
    return {
        "calculation_only": True,
        "status": status,
        "all_passed": all_passed,
        "checks": checks,
    }


def format_preflight_markdown(result: dict[str, Any]) -> str:
    checks = "\n".join(
        f"| {name} | {'pass' if item['passed'] else 'fail'} | {item['actual']} | {item['required']} |"
        for name, item in result["minimum_dataset_gates"]["checks"].items()
    )
    errors = "\n".join(f"- {error}" for error in result["validation"]["errors"]) or "- None"
    return (
        "# OfficeLifeMemoryBench Track B Preflight\n\n"
        f"- Dataset SHA-256: `{result['dataset']['sha256']}`\n"
        f"- Split: `{result['dataset']['selected_split']}`\n"
        f"- Structural validation: {'pass' if result['validation']['passed'] else 'fail'}\n"
        f"- Minimum dataset gates: {'pass' if result['minimum_dataset_gates']['passed'] else 'fail'}\n"
        "- Artifact scope: private custodian preflight\n\n"
        "## Minimum Dataset Gates\n\n"
        "| Gate | Status | Actual | Required |\n"
        "|---|---|---|---|\n"
        f"{checks}\n\n"
        "## Validation Errors\n\n"
        f"{errors}\n"
    )


def format_summary_markdown(result: dict[str, Any]) -> str:
    overall = result["metrics"]["overall"]
    ci = result["metrics"]["clustered_bootstrap_ci95"]
    gate_rows = "\n".join(
        f"| {name} | {item['status']} | {item['actual']} | {item['required']} |"
        for name, item in result["computed_gates"]["checks"].items()
    )
    return (
        "# OfficeLifeMemoryBench Track B Summary\n\n"
        f"- Dataset revision: `{result['dataset']['revision']}`\n"
        f"- Expected/scored pairs: {result['coverage']['expected_tasks']}/{result['coverage']['scored_pairs']}\n"
        f"- Infrastructure complete: {result['coverage']['infrastructure_complete']}\n"
        f"- No Memory success: {_display_metric(overall['no_memory_task_success_rate'])}\n"
        f"- MemoryPack success: {_display_metric(overall['memory_pack_task_success_rate'])}\n"
        f"- Absolute lift: {_display_metric(overall['task_success_lift'])}\n"
        f"- User-clustered 95% CI: {ci}\n"
        f"- Computed-gate status: {result['computed_gates']['status']}\n"
        f"- Qualification status: {result['qualification_status']}\n\n"
        "This offline calculator cannot complete Track B qualification.\n\n"
        "## Computed Gates (Calculation Only)\n\n"
        "| Gate | Status | Actual | Required |\n"
        "|---|---|---|---|\n"
        f"{gate_rows}\n"
    )


def write_outputs(
    result: dict[str, Any],
    json_path: Path | None,
    markdown_path: Path | None,
    markdown: str,
) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if markdown_path is not None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(markdown, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and summarize OfficeLifeMemoryBench Track B.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("dataset", type=Path)
    preflight.add_argument("manifest", type=Path)
    preflight.add_argument("--split", choices=sorted(SPLITS), default="hidden_test")
    preflight.add_argument("--strict", action="store_true")
    preflight.add_argument("--output-json", type=Path, default=None)
    preflight.add_argument("--output-md", type=Path, default=None)

    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("dataset", type=Path)
    summarize.add_argument("manifest", type=Path)
    summarize.add_argument("evaluation", type=Path)
    summarize.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES)
    summarize.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    summarize.add_argument("--output-json", type=Path, default=None)
    summarize.add_argument("--output-md", type=Path, default=None)
    summarize.add_argument(
        "--enforce-computed-gates",
        action="store_true",
        help="Exit non-zero unless the offline computed-gate calculation passes; this does not complete Track B qualification.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "preflight":
        result = preflight_dataset(
            args.dataset,
            args.manifest,
            split=args.split,
            strict=args.strict,
        )
        markdown = format_preflight_markdown(result)
        write_outputs(result, args.output_json, args.output_md, markdown)
        print(markdown)
        return 0 if result["passed"] else 2

    result = summarize_evaluation(
        args.dataset,
        args.manifest,
        args.evaluation,
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed,
    )
    markdown = format_summary_markdown(result)
    write_outputs(result, args.output_json, args.output_md, markdown)
    print(markdown)
    if args.enforce_computed_gates and result["computed_gates"]["status"] != "passed":
        return 2
    return 0


def _index_dataset(dataset: dict[str, Any]) -> DatasetIndex:
    errors: list[str] = []
    if dataset.get("schema_version") != DATASET_SCHEMA_VERSION:
        errors.append(f"dataset.schema_version must be {DATASET_SCHEMA_VERSION}")
    raw_users = dataset.get("users")
    if not isinstance(raw_users, list):
        errors.append("dataset.users must be a list")
        raw_users = []

    users: dict[str, dict[str, Any]] = {}
    events_by_user: dict[str, dict[str, dict[str, Any]]] = {}
    event_times_by_user: dict[str, dict[str, datetime]] = {}
    event_available_times_by_user: dict[str, dict[str, datetime]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    task_times: dict[str, datetime] = {}
    task_cutoffs: dict[str, datetime] = {}
    task_users: dict[str, str] = {}
    seen_user_splits: dict[str, str] = {}
    seen_event_ids: set[str] = set()
    seen_task_ids: set[str] = set()

    for user_index, raw_user in enumerate(raw_users):
        prefix = f"users[{user_index}]"
        if not isinstance(raw_user, dict):
            errors.append(f"{prefix} must be an object")
            continue
        user_id = raw_user.get("user_id")
        split = raw_user.get("split")
        if not _pseudonymous_id(user_id):
            errors.append(f"{prefix}.user_id must be a pseudonymous [A-Za-z0-9_.-]+ identifier")
            continue
        user_id = str(user_id)
        if split not in SPLITS:
            errors.append(f"{prefix}.split must be one of {sorted(SPLITS)}")
        previous_split = seen_user_splits.get(user_id)
        if previous_split is not None and previous_split != split:
            errors.append(f"user {user_id} appears in both {previous_split} and {split}")
        elif previous_split is not None:
            errors.append(f"duplicate user_id: {user_id}")
        seen_user_splits[user_id] = str(split)
        if user_id in users:
            continue
        users[user_id] = raw_user

        raw_events = raw_user.get("events")
        if not isinstance(raw_events, list):
            errors.append(f"{prefix}.events must be a list")
            raw_events = []
        user_events: dict[str, dict[str, Any]] = {}
        user_event_times: dict[str, datetime] = {}
        user_available_times: dict[str, datetime] = {}
        for event_index, event in enumerate(raw_events):
            event_prefix = f"{prefix}.events[{event_index}]"
            if not isinstance(event, dict):
                errors.append(f"{event_prefix} must be an object")
                continue
            missing_fields = _missing_fields(
                event,
                (
                    "event_id",
                    "occurred_at",
                    "available_at",
                    "surface",
                    "memory_bearing",
                    "finalized",
                    "asr_final",
                ),
            )
            if missing_fields:
                errors.append(f"{event_prefix} missing fields: {missing_fields}")
                continue
            event_id = event.get("event_id")
            if not _pseudonymous_id(event_id):
                errors.append(f"{event_prefix}.event_id must be [A-Za-z0-9_.-]+")
                continue
            event_id = str(event_id)
            if event_id in seen_event_ids:
                errors.append(f"duplicate event_id: {event_id}")
                continue
            seen_event_ids.add(event_id)
            if event.get("surface") not in SURFACES:
                errors.append(f"{event_prefix}.surface must be one of {sorted(SURFACES)}")
            if not isinstance(event.get("memory_bearing"), bool):
                errors.append(f"{event_prefix}.memory_bearing must be a boolean")
            if not isinstance(event.get("finalized"), bool):
                errors.append(f"{event_prefix}.finalized must be a boolean")
            if event.get("asr_final") is not None and not isinstance(event.get("asr_final"), bool):
                errors.append(f"{event_prefix}.asr_final must be true, false, or null")
            if event.get("surface") == "realtime_voice" and not isinstance(
                event.get("asr_final"), bool
            ):
                errors.append(
                    f"{event_prefix}.asr_final must be true or false for realtime_voice"
                )
            try:
                occurred_at = _parse_timestamp(event.get("occurred_at"), f"{event_prefix}.occurred_at")
                available_at = _parse_timestamp(event.get("available_at"), f"{event_prefix}.available_at")
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if available_at < occurred_at:
                errors.append(f"{event_prefix}.available_at must not be earlier than occurred_at")
            user_events[event_id] = event
            user_event_times[event_id] = occurred_at
            user_available_times[event_id] = available_at
        events_by_user[user_id] = user_events
        event_times_by_user[user_id] = user_event_times
        event_available_times_by_user[user_id] = user_available_times

        raw_tasks = raw_user.get("tasks")
        if not isinstance(raw_tasks, list):
            errors.append(f"{prefix}.tasks must be a list")
            raw_tasks = []
        for task_index, task in enumerate(raw_tasks):
            task_prefix = f"{prefix}.tasks[{task_index}]"
            if not isinstance(task, dict):
                errors.append(f"{task_prefix} must be an object")
                continue
            required_fields = (
                "task_id",
                "task_at",
                "history_cutoff",
                "surface",
                "scenario_family",
                "memory_requirement",
                "scoring_mode",
                "relevant_event_ids",
                "superseded_event_ids",
                "deleted_event_ids",
                "allowed_scope",
            )
            missing_fields = _missing_fields(task, required_fields)
            if missing_fields:
                errors.append(f"{task_prefix} missing fields: {missing_fields}")
                continue
            task_id = task.get("task_id")
            if not _pseudonymous_id(task_id):
                errors.append(f"{task_prefix}.task_id must be [A-Za-z0-9_.-]+")
                continue
            task_id = str(task_id)
            if task_id in seen_task_ids:
                errors.append(f"duplicate task_id: {task_id}")
                continue
            seen_task_ids.add(task_id)
            if task.get("surface") not in SURFACES:
                errors.append(f"{task_prefix}.surface must be one of {sorted(SURFACES)}")
            if task.get("scenario_family") not in SCENARIO_FAMILIES:
                errors.append(
                    f"{task_prefix}.scenario_family must be one of {sorted(SCENARIO_FAMILIES)}"
                )
            if task.get("memory_requirement") not in MEMORY_REQUIREMENTS:
                errors.append(
                    f"{task_prefix}.memory_requirement must be one of {sorted(MEMORY_REQUIREMENTS)}"
                )
            if task.get("scoring_mode") not in SCORING_MODES:
                errors.append(f"{task_prefix}.scoring_mode must be one of {sorted(SCORING_MODES)}")
            allowed_scope = task.get("allowed_scope")
            if not isinstance(allowed_scope, dict) or any(
                not _nonempty_string(allowed_scope.get(field)) for field in SCOPE_FIELDS
            ):
                errors.append(
                    f"{task_prefix}.allowed_scope must contain non-empty tenant_id, user_id, and namespace"
                )
            elif allowed_scope.get("user_id") != user_id:
                errors.append(
                    f"{task_prefix}.allowed_scope.user_id must match the outer dataset user_id"
                )
            try:
                task_time = _parse_timestamp(task.get("task_at"), f"{task_prefix}.task_at")
                history_cutoff = _parse_timestamp(
                    task.get("history_cutoff"),
                    f"{task_prefix}.history_cutoff",
                )
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if history_cutoff > task_time:
                errors.append(f"{task_prefix}.history_cutoff must not be later than task_at")
            references: dict[str, list[str]] = {}
            reference_error = False
            for field_name in ("relevant_event_ids", "superseded_event_ids", "deleted_event_ids"):
                try:
                    references[field_name] = _string_list(task.get(field_name), f"{task_prefix}.{field_name}")
                except ValueError as exc:
                    errors.append(str(exc))
                    reference_error = True
            if reference_error:
                continue
            overlap = (
                set(references["relevant_event_ids"])
                & set(references["superseded_event_ids"])
                | set(references["relevant_event_ids"])
                & set(references["deleted_event_ids"])
                | set(references["superseded_event_ids"])
                & set(references["deleted_event_ids"])
            )
            if overlap:
                errors.append(f"{task_prefix} event reference groups overlap: {sorted(overlap)}")
            for field_name, event_ids in references.items():
                for event_id in event_ids:
                    if event_id not in user_events:
                        errors.append(f"{task_prefix}.{field_name} references unknown event {event_id}")
                    elif not _eligible_history_event(
                        user_events[event_id],
                        user_available_times[event_id],
                        history_cutoff,
                    ):
                        errors.append(
                            f"{task_prefix}.{field_name} references event {event_id} outside eligible history"
                        )
            tasks[task_id] = task
            task_times[task_id] = task_time
            task_cutoffs[task_id] = history_cutoff
            task_users[task_id] = user_id

    return DatasetIndex(
        users=users,
        events_by_user=events_by_user,
        event_times_by_user=event_times_by_user,
        event_available_times_by_user=event_available_times_by_user,
        tasks=tasks,
        task_times=task_times,
        task_cutoffs=task_cutoffs,
        task_users=task_users,
        errors=errors,
    )


def _validate_efficiency(value: Any, task_id: str, arm_name: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{task_id}.{arm_name}.efficiency must be an object")
    result: dict[str, float] = {}
    for field_name in (
        "ingest_latency_ms",
        "recall_latency_ms",
        "context_chars",
        "provider_tokens",
        "reader_cost",
        "total_cost",
    ):
        result[field_name] = _nonnegative_number(
            value.get(field_name),
            f"{task_id}.{arm_name}.efficiency.{field_name}",
        )
    return result


def _validate_safety(value: Any, task_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{task_id}.memory_pack.safety must be an object")
    result: dict[str, Any] = {}
    for field_name in ("unsupported_memory", "stale_or_superseded"):
        if not isinstance(value.get(field_name), bool):
            raise ValueError(f"{task_id}.memory_pack.safety.{field_name} must be a boolean")
        result[field_name] = value[field_name]
    for field_name in (
        "cross_scope_leakage_count",
        "deletion_violation_count",
        "partial_asr_durable_commit_count",
    ):
        count = value.get(field_name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{task_id}.memory_pack.safety.{field_name} must be an integer >= 0")
        result[field_name] = count
    return result


def _summarize_latency_profile(value: Any) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(value, dict):
        value = {}
        errors.append("evaluation.latency_profile must be an object")
    stored_events = value.get("stored_events")
    processes = value.get("processes")
    threads = value.get("threads")
    concurrency = value.get("concurrency")
    network_calls = value.get("network_calls")
    model_calls = value.get("model_calls")
    warmup_iterations = value.get("warmup_iterations")
    measured_iterations = value.get("measured_iterations")
    fixture_sha256 = value.get("fixture_sha256")
    query_sha256 = value.get("query_sha256")
    raw_latencies = value.get("recall_latency_ms")
    latencies: list[float] = []
    if not isinstance(raw_latencies, list) or not raw_latencies:
        errors.append("latency_profile.recall_latency_ms must be a non-empty list")
    else:
        if len(raw_latencies) != 1000:
            errors.append("latency_profile.recall_latency_ms must contain exactly 1000 raw durations")
        for index, item in enumerate(raw_latencies):
            try:
                latencies.append(_nonnegative_number(item, f"latency_profile.recall_latency_ms[{index}]"))
            except ValueError as exc:
                errors.append(str(exc))
    if stored_events != 1000:
        errors.append("latency_profile.stored_events must equal 1000")
    if processes != 1:
        errors.append("latency_profile.processes must equal 1")
    if threads != 1:
        errors.append("latency_profile.threads must equal 1")
    if concurrency != 1:
        errors.append("latency_profile.concurrency must equal 1")
    if network_calls != 0:
        errors.append("latency_profile.network_calls must equal 0")
    if model_calls != 0:
        errors.append("latency_profile.model_calls must equal 0")
    if warmup_iterations != 100:
        errors.append("latency_profile.warmup_iterations must equal 100")
    if measured_iterations != 1000:
        errors.append("latency_profile.measured_iterations must equal 1000")
    if not _is_sha256(fixture_sha256):
        errors.append("latency_profile.fixture_sha256 must be a lowercase SHA-256 hex string")
    if not _is_sha256(query_sha256):
        errors.append("latency_profile.query_sha256 must be a lowercase SHA-256 hex string")
    p95 = _nearest_rank_percentile(latencies, 95) if latencies else None
    return {
        "stored_events": stored_events,
        "processes": processes,
        "threads": threads,
        "concurrency": concurrency,
        "network_calls": network_calls,
        "model_calls": model_calls,
        "warmup_iterations": warmup_iterations,
        "measured_iterations": measured_iterations,
        "fixture_sha256": fixture_sha256 if _is_sha256(fixture_sha256) else None,
        "query_sha256": query_sha256 if _is_sha256(query_sha256) else None,
        "sample_count": len(latencies),
        "p50_ms": _nearest_rank_percentile(latencies, 50) if latencies else None,
        "p95_ms": p95,
        "errors": errors,
        "complete": not errors,
        "gate_passed": not errors and p95 is not None and p95 <= 300.0,
    }


def _paired_metrics(records: list[PairRecord]) -> dict[str, Any]:
    if not records:
        return {
            "count": 0,
            "no_memory_task_success_rate": None,
            "memory_pack_task_success_rate": None,
            "task_success_lift": None,
        }
    no_memory = statistics.fmean(1.0 if record.no_memory_success else 0.0 for record in records)
    memory_pack = statistics.fmean(1.0 if record.memory_pack_success else 0.0 for record in records)
    return {
        "count": len(records),
        "no_memory_task_success_rate": no_memory,
        "memory_pack_task_success_rate": memory_pack,
        "task_success_lift": memory_pack - no_memory,
    }


def _summarize_safety(records: list[PairRecord]) -> dict[str, Any]:
    count = len(records)
    unsupported = sum(1 for record in records if record.unsupported_memory)
    stale = sum(1 for record in records if record.stale_or_superseded)
    return {
        "source": "custodian-supplied adjudication",
        "task_count": count,
        "unsupported_memory_task_count": unsupported,
        "unsupported_memory_rate": unsupported / count if count else None,
        "stale_or_superseded_task_count": stale,
        "stale_or_superseded_rate": stale / count if count else None,
        "cross_scope_leakage_count": sum(record.cross_scope_leakage_count for record in records),
        "deletion_violation_count": sum(record.deletion_violation_count for record in records),
        "partial_asr_durable_commit_count": sum(
            record.partial_asr_durable_commit_count for record in records
        ),
    }


def _summarize_citations(records: list[PairRecord]) -> dict[str, Any]:
    cited_count = 0
    relevant_cited_count = 0
    relevant_source_count = 0
    for record in records:
        cited = set(record.cited_event_ids)
        relevant = set(record.relevant_event_ids)
        cited_count += len(cited)
        relevant_cited_count += len(cited & relevant)
        relevant_source_count += len(relevant)
    return {
        "source": "custodian-supplied event-ID lists",
        "protocol_metric_available": False,
        "precision": None,
        "source_coverage": None,
        "diagnostic": {
            "name": "event-ID overlap; not protocol citation quality",
            "cited_event_count": cited_count,
            "relevant_cited_event_count": relevant_cited_count,
            "relevant_source_event_count": relevant_source_count,
            "event_id_overlap_precision": (
                relevant_cited_count / cited_count if cited_count else None
            ),
            "relevant_event_id_coverage": (
                relevant_cited_count / relevant_source_count
                if relevant_source_count
                else None
            ),
        },
    }


def _summarize_efficiency(records: list[PairRecord], arm_name: str) -> dict[str, Any]:
    values = [
        record.no_memory_efficiency if arm_name == "no_memory" else record.memory_pack_efficiency
        for record in records
    ]
    success_count = sum(
        1
        for record in records
        if (
            record.no_memory_success
            if arm_name == "no_memory"
            else record.memory_pack_success
        )
    )
    result: dict[str, Any] = {"count": len(values)}
    for field_name in (
        "ingest_latency_ms",
        "recall_latency_ms",
        "context_chars",
        "provider_tokens",
        "reader_cost",
        "total_cost",
    ):
        field_values = [item[field_name] for item in values]
        result[field_name] = _numeric_summary(
            field_values,
            nearest_rank=field_name in {"ingest_latency_ms", "recall_latency_ms"},
        )
    total_cost = sum(item["total_cost"] for item in values)
    result["total_cost_sum"] = total_cost
    result["cost_per_success"] = total_cost / success_count if success_count else None
    return result


def _numeric_summary(
    values: list[float],
    *,
    nearest_rank: bool = False,
) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    percentile = _nearest_rank_percentile if nearest_rank else _percentile
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
    }


def _distribution_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "p50": None,
            "p95": None,
        }
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
    }


def _validate_run_metadata(value: Any) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(value, dict):
        return {}, ["evaluation.run_metadata must be an object"]
    identifier_fields = (
        "run_id",
        "code_commit",
        "citefold_version",
        "agent_turn_contract",
        "provider",
    )
    display_fields = (
        "model",
        "actual_model",
    )
    hash_fields = (
        "prompt_sha256",
        "config_sha256",
    )
    errors: list[str] = []
    result: dict[str, Any] = {}
    for name in identifier_fields:
        item = value.get(name)
        if not _pseudonymous_id(item):
            errors.append(
                f"run_metadata.{name} must be a public-safe [A-Za-z0-9_.-]+ identifier"
            )
        else:
            result[name] = item
    for name in display_fields:
        item = value.get(name)
        if not _safe_model_identifier(item):
            errors.append(f"run_metadata.{name} must be a public-safe model identifier")
        else:
            result[name] = item
    for name in hash_fields:
        item = value.get(name)
        if not _is_sha256(item):
            errors.append(f"run_metadata.{name} must be a lowercase SHA-256 hex string")
        else:
            result[name] = item
    randomization_seed = value.get("randomization_seed")
    if isinstance(randomization_seed, bool) or not isinstance(randomization_seed, int):
        errors.append("run_metadata.randomization_seed must be an integer")
    else:
        result["randomization_seed"] = randomization_seed
    fixed_retry_count = value.get("fixed_retry_count")
    if (
        isinstance(fixed_retry_count, bool)
        or not isinstance(fixed_retry_count, int)
        or fixed_retry_count < 0
    ):
        errors.append("run_metadata.fixed_retry_count must be an integer >= 0")
    else:
        result["fixed_retry_count"] = fixed_retry_count
    blinded = value.get("blinded")
    if blinded is not True:
        errors.append("run_metadata.blinded must be true")
    else:
        result["blinded"] = True
    return result, errors


def _record_rater_diagnostics(
    counter: Counter[str],
    arm: dict[str, Any],
    scoring_mode: str,
) -> None:
    if scoring_mode != "human" or arm.get("completed") is not True:
        return
    labels = arm.get("rater_labels")
    if not isinstance(labels, list) or len(labels) not in {2, 3} or any(
        not isinstance(label, bool) for label in labels
    ):
        return
    counter["primary_pairs"] += 1
    if labels[0] == labels[1]:
        counter["agreements"] += 1
    else:
        counter["disagreements"] += 1
        if len(labels) == 3:
            counter["third_rater_count"] += 1


def _summarize_rater_agreement(
    counters: dict[str, Counter[str]],
) -> dict[str, Any]:
    by_arm: dict[str, dict[str, Any]] = {}
    overall = Counter(
        {
            "primary_pairs": 0,
            "agreements": 0,
            "disagreements": 0,
            "third_rater_count": 0,
        }
    )
    for arm_name in ARM_NAMES:
        counter = counters[arm_name]
        overall.update(counter)
        primary_pairs = counter["primary_pairs"]
        by_arm[arm_name] = {
            "primary_pairs": primary_pairs,
            "agreements": counter["agreements"],
            "disagreements": counter["disagreements"],
            "third_rater_count": counter["third_rater_count"],
            "agreement_rate": (
                counter["agreements"] / primary_pairs if primary_pairs else None
            ),
        }
    primary_pairs = overall["primary_pairs"]
    return {
        "source": "custodian-supplied adjudication",
        "overall": {
            "primary_pairs": primary_pairs,
            "agreements": overall["agreements"],
            "disagreements": overall["disagreements"],
            "third_rater_count": overall["third_rater_count"],
            "agreement_rate": (
                overall["agreements"] / primary_pairs if primary_pairs else None
            ),
        },
        "by_arm": by_arm,
    }


def _tri_state_check(
    condition: bool,
    actual: Any,
    required: Any,
    *,
    evaluable: bool,
) -> dict[str, Any]:
    if not evaluable:
        status = "not_evaluable"
        passed: bool | None = None
    else:
        passed = bool(condition)
        status = "passed" if passed else "failed"
    return {
        "status": status,
        "passed": passed,
        "actual": actual,
        "required": required,
    }


def _check(passed: bool, actual: Any, required: Any) -> dict[str, Any]:
    return {"passed": bool(passed), "actual": actual, "required": required}


def _paired_difference(record: PairRecord) -> float:
    return float(int(record.memory_pack_success) - int(record.no_memory_success))


def _history_bucket(value: int) -> str:
    if value < 100:
        return "0-99"
    if value < 500:
        return "100-499"
    if value < 1000:
        return "500-999"
    return "1000+"


def _eligible_history_event(
    event: dict[str, Any],
    available_at: datetime,
    history_cutoff: datetime,
) -> bool:
    return (
        event.get("memory_bearing") is True
        and event.get("finalized") is True
        and (
            event.get("asr_final") is True
            if event.get("surface") == "realtime_voice"
            else event.get("asr_final") is not False
        )
        and available_at < history_cutoff
    )


def _elapsed_span_days(values: Iterable[datetime]) -> float:
    collected = list(values)
    if not collected:
        return 0.0
    earliest = min(collected).astimezone(timezone.utc)
    latest = max(collected).astimezone(timezone.utc)
    return (latest - earliest).total_seconds() / 86_400.0


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not _pseudonymous_id(item) for item in value):
        raise ValueError(f"{field_name} must be a list of [A-Za-z0-9_.-]+ identifiers")
    result = [str(item) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must not contain duplicate identifiers")
    return result


def _nonnegative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number >= 0")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be a finite number >= 0")
    return number


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(ordered[lower])
    fraction = index - lower
    return float(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction)


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return float(ordered[rank - 1])


def _missing_fields(value: dict[str, Any], field_names: Iterable[str]) -> list[str]:
    return [field_name for field_name in field_names if field_name not in value]


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_model_identifier(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 128
        or value != value.strip()
        or not value.isascii()
        or not all(character.isalnum() or character in "._:/+-" for character in value)
        or value.startswith(("/", "\\"))
        or "\\" in value
    ):
        return False
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return False
    if segments[0].endswith(":"):
        return False
    lowered_value = value.lower()
    if lowered_value.startswith(("sk-", "api-key-", "token-", "bearer-")):
        return False
    lowered = f"/{lowered_value}/"
    return not any(
        marker in lowered
        for marker in (
            "/users/",
            "/home/",
            "/private/",
            "/tmp/",
            "/var/",
            "/volumes/",
            "/etc/",
            "/opt/",
            "/usr/",
        )
    )


def _pseudonymous_id(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 128 or not value.isascii():
        return False
    return all(character.isalnum() or character in "_.-" for character in value)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _display_metric(value: Any) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
