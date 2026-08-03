import copy
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.officelife_track_b_contract import (
    CONTRACT_VERSION,
    DATASET_MANIFEST_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    EXECUTION_PROFILE_VERSION,
    PROTOCOL_VERSION,
    SEALED_RUN_MANIFEST_SCHEMA_VERSION,
    TASK_INPUT_SCHEMA_VERSION,
    TASK_LABEL_SCHEMA_VERSION,
    USER_SCHEMA_VERSION,
    main,
    schema_paths,
    validate_dataset_bundle,
    validate_run_bundle,
    validate_schema_bundle,
)


PRIVATE_SENTINEL = "PRIVATE-SENTINEL-DO-NOT-REPORT"
BASE_TIME = "2026-01-01T00:00:00Z"
TASK_TIME = "2026-01-02T00:00:00Z"


def canonical_line(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_line(record) for record in records))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_ref(root: Path, path: str, artifact_id: str, media_type: str) -> dict:
    item = root / path
    return {
        "artifact_id": artifact_id,
        "path": path,
        "sha256": sha256(item),
        "size_bytes": item.stat().st_size,
        "media_type": media_type,
    }


def inventory_entry(
    root: Path,
    path: str,
    role: str,
    *,
    artifact_kind: str,
    schema_version: str = "opaque-v1",
    record_count: int = 0,
    sensitivity: str = "restricted",
    access_class: str,
    media_type: str,
) -> dict:
    item = root / path
    return {
        "path": path,
        "role": role,
        "artifact_kind": artifact_kind,
        "sha256": sha256(item),
        "size_bytes": item.stat().st_size,
        "schema_version": schema_version,
        "record_count": record_count,
        "sensitivity": sensitivity,
        "access_class": access_class,
        "media_type": media_type,
    }


def zero_counts() -> dict:
    return {
        "development": 0,
        "validation": 0,
        "hidden_test": 0,
    }


def empty_timeline() -> dict:
    return {
        "event_occurred_at_min": None,
        "event_occurred_at_max": None,
        "event_available_at_min": None,
        "event_available_at_max": None,
        "task_timestamp_min": None,
        "task_timestamp_max": None,
    }


def declared_counts() -> dict:
    users = zero_counts()
    users["hidden_test"] = 1
    tasks = zero_counts()
    tasks["hidden_test"] = 1
    surfaces = {
        split: {
            "text_chat": 0,
            "realtime_voice": 0,
            "third_party_agents": 0,
            "cross_channel": 0,
        }
        for split in ("development", "validation", "hidden_test")
    }
    surfaces["hidden_test"]["text_chat"] = 1
    families = {
        split: {
            "stable_preferences": 0,
            "open_loops": 0,
            "people_followup": 0,
            "meeting_decisions": 0,
            "stale_or_superseded": 0,
            "correction": 0,
            "no_evidence": 0,
            "scope_isolation": 0,
            "deletion": 0,
            "cross_channel": 0,
        }
        for split in ("development", "validation", "hidden_test")
    }
    families["hidden_test"]["stable_preferences"] = 1
    requirements = {
        split: {"required": 0, "optional": 0, "absent": 0}
        for split in ("development", "validation", "hidden_test")
    }
    requirements["hidden_test"]["required"] = 1
    timeline = {
        split: empty_timeline()
        for split in ("development", "validation", "hidden_test")
    }
    timeline["hidden_test"] = {
        "event_occurred_at_min": BASE_TIME,
        "event_occurred_at_max": BASE_TIME,
        "event_available_at_min": BASE_TIME,
        "event_available_at_max": BASE_TIME,
        "task_timestamp_min": TASK_TIME,
        "task_timestamp_max": TASK_TIME,
    }
    return {
        "users_by_split": users,
        "unique_tasks_by_split": tasks,
        "surface_memberships_by_split": surfaces,
        "scenario_families_by_split": families,
        "memory_requirements_by_split": requirements,
        "harm_unique_tasks_by_split": zero_counts(),
        "timeline_by_split": timeline,
        "hidden_test_per_user_minimums": {
            "history_span_days": 0.0,
            "memory_bearing_events": 1,
            "tasks": 1,
        },
    }


def build_dataset_bundle(root: Path) -> dict:
    scope = {
        "tenant_id": "tenant-a",
        "user_id": "user-01",
        "namespace": "personal",
        "connected_agent_authorization_ids": [],
    }
    payloads = {
        "payloads/event-01.txt": PRIVATE_SENTINEL + " event",
        "payloads/task-01.txt": PRIVATE_SENTINEL + " task",
        "payloads/recent-01.txt": PRIVATE_SENTINEL + " recent",
        "payloads/tool-01.json": "{}\n",
        "snapshots/snapshot-01.tar.zst": PRIVATE_SENTINEL + " snapshot",
    }
    for relative, content in payloads.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    governance_files = {
        "governance/consent.txt": "consent policy\n",
        "governance/deidentification.txt": "deidentification policy\n",
        "governance/codebook.json": "{}\n",
        "governance/prohibited-scan.json": "{}\n",
        "governance/access-control.txt": "access control\n",
        "governance/retention.txt": "retention\n",
        "governance/withdrawal.txt": "withdrawal\n",
        "governance/identity-commitment.txt": "commitment\n",
    }
    for relative, content in governance_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    user = {
        "schema_version": USER_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "user_id": "user-01",
        "split": "hidden_test",
        "allowed_scopes": [scope],
        "consent_record_id": "consent-01",
        "deidentification_record_id": "deid-01",
    }
    event = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "event_id": "event-01",
        "source_record_id": "source-01",
        "user_id": "user-01",
        "conversation_id": "conversation-01",
        "scope": scope,
        "occurred_at": BASE_TIME,
        "available_at": BASE_TIME,
        "source_surface": "text_chat",
        "modality": "text",
        "memory_bearing": True,
        "lifecycle_state": "finalized",
        "asr_state": "not_applicable",
        "recallable": True,
        "payload_refs": [
            artifact_ref(
                root,
                "payloads/event-01.txt",
                "event-payload-01",
                "text/plain",
            )
        ],
    }
    snapshot_path = "snapshots/snapshot-01.tar.zst"
    snapshot_id = f"snapshot-{sha256(root / snapshot_path)}"
    task_input = {
        "schema_version": TASK_INPUT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "task_id": "task-01",
        "user_id": "user-01",
        "conversation_id": "conversation-01",
        "split": "hidden_test",
        "allowed_scope": scope,
        "task_timestamp": TASK_TIME,
        "history_cutoff": TASK_TIME,
        "execution_surface": "text_chat",
        "availability_rule": "available-at-strictly-before-cutoff-v1",
        "input_artifact": artifact_ref(
            root,
            "payloads/task-01.txt",
            "task-payload-01",
            "text/plain",
        ),
        "recent_context_artifact": artifact_ref(
            root,
            "payloads/recent-01.txt",
            "recent-payload-01",
            "text/plain",
        ),
        "tool_fixture_artifact": artifact_ref(
            root,
            "payloads/tool-01.json",
            "tool-payload-01",
            "application/json",
        ),
        "snapshot_id": snapshot_id,
        "snapshot_artifact": artifact_ref(
            root,
            snapshot_path,
            snapshot_id,
            "application/zstd",
        ),
    }
    fact_value = {
        "value_type": "string",
        "canonical": "blue",
        "alternatives": [],
    }
    task_label = {
        "schema_version": TASK_LABEL_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "task_id": "task-01",
        "user_id": "user-01",
        "scenario_family": "stable_preferences",
        "surface_memberships": ["text_chat"],
        "source_surfaces": ["text_chat"],
        "memory_requirement": "required",
        "allowed_scope": scope,
        "acceptable_facts": [
            {
                "fact_id": "fact-01",
                "importance": "required",
                "acceptable_values": [fact_value],
                "valid_interval": {
                    "start_inclusive": None,
                    "end_exclusive": None,
                },
                "source_support_required": True,
                "acceptable_evidence_sets": [["event-01"]],
                "allowed_inference_ids": [],
            }
        ],
        "acceptable_answer_sets": [
            {
                "set_id": "answer-set-01",
                "required_fact_ids": ["fact-01"],
                "optional_fact_ids": [],
            }
        ],
        "allowed_non_memory_evidence_refs": [],
        "forbidden_items": [
            {
                "forbidden_id": "forbidden-01",
                "kind": "fact",
                "canonical": "red",
                "hard_prohibition": True,
            }
        ],
        "relevant_source_event_ids": ["event-01"],
        "superseded_event_ids": [],
        "deleted_event_ids": [],
        "deterministic_checks": [
            {
                "check_id": "check-01",
                "type": "fact",
                "subject_kind": "acceptable_fact",
                "subject_ref": "fact-01",
                "operator": "present",
                "expected_values": [fact_value],
                "must_pass": True,
                "hard_prohibition": False,
            },
            {
                "check_id": "check-hard-01",
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
        "human_judgment_required": False,
        "success_rule": {
            "rule_version": "binary-all-must-pass-v1",
            "acceptable_answer_set_logic": "any",
            "required_check_ids": ["check-01"],
            "hard_prohibition_check_ids": ["check-hard-01"],
            "human_judgment_required": False,
        },
    }
    records = {
        "users": [user],
        "events": [event],
        "task-inputs": [task_input],
        "task-labels": [task_label],
    }
    for role, items in records.items():
        write_jsonl(root / f"{role}.jsonl", items)

    files = [
        inventory_entry(
            root,
            "users.jsonl",
            "users",
            artifact_kind="jsonl-records",
            schema_version=USER_SCHEMA_VERSION,
            record_count=1,
            access_class="generator_input",
            media_type="application/x-ndjson",
        ),
        inventory_entry(
            root,
            "events.jsonl",
            "events",
            artifact_kind="jsonl-records",
            schema_version=EVENT_SCHEMA_VERSION,
            record_count=1,
            access_class="generator_input",
            media_type="application/x-ndjson",
        ),
        inventory_entry(
            root,
            "task-inputs.jsonl",
            "task-inputs",
            artifact_kind="jsonl-records",
            schema_version=TASK_INPUT_SCHEMA_VERSION,
            record_count=1,
            access_class="generator_input",
            media_type="application/x-ndjson",
        ),
        inventory_entry(
            root,
            "task-labels.jsonl",
            "task-labels",
            artifact_kind="jsonl-records",
            schema_version=TASK_LABEL_SCHEMA_VERSION,
            record_count=1,
            access_class="custodian_only",
            media_type="application/x-ndjson",
        ),
    ]
    for relative, role, media_type in (
        ("payloads/event-01.txt", "event-payload-01", "text/plain"),
        ("payloads/task-01.txt", "task-input-payload-01", "text/plain"),
        ("payloads/recent-01.txt", "recent-context-payload-01", "text/plain"),
        ("payloads/tool-01.json", "tool-fixture-payload-01", "application/json"),
    ):
        files.append(
            inventory_entry(
                root,
                relative,
                role,
                artifact_kind="json-document" if relative.endswith(".json") else "text",
                access_class="generator_input",
                media_type=media_type,
            )
        )
    files.append(
        inventory_entry(
            root,
            snapshot_path,
            snapshot_id,
            artifact_kind="package",
            access_class="executor_input",
            media_type="application/zstd",
        )
    )
    governance_roles = (
        ("governance/consent.txt", "consent-policy", "policy", "governance"),
        (
            "governance/deidentification.txt",
            "deidentification-policy",
            "policy",
            "governance",
        ),
        ("governance/codebook.json", "annotation-codebook", "codebook", "governance"),
        (
            "governance/prohibited-scan.json",
            "prohibited-identifiers-scan",
            "json-document",
            "governance",
        ),
        (
            "governance/access-control.txt",
            "access-control-policy",
            "policy",
            "governance",
        ),
        ("governance/retention.txt", "retention-policy", "policy", "governance"),
        ("governance/withdrawal.txt", "withdrawal-policy", "policy", "governance"),
        (
            "governance/identity-commitment.txt",
            "identity-mapping-commitment",
            "binary",
            "identity_vault",
        ),
    )
    for relative, role, kind, access in governance_roles:
        files.append(
            inventory_entry(
                root,
                relative,
                role,
                artifact_kind=kind,
                access_class=access,
                media_type="application/json" if relative.endswith(".json") else "text/plain",
            )
        )
    by_role = {item["role"]: item for item in files}
    governance = {
        "custodian_organization_id": "custodian-01",
        "consent_policy": {
            "version": "consent-v1",
            "file_role": "consent-policy",
            "sha256": by_role["consent-policy"]["sha256"],
        },
        "deidentification_policy": {
            "version": "deid-v1",
            "file_role": "deidentification-policy",
            "sha256": by_role["deidentification-policy"]["sha256"],
        },
        "annotation_codebook": {
            "version": "codebook-v1",
            "file_role": "annotation-codebook",
            "sha256": by_role["annotation-codebook"]["sha256"],
        },
        "prohibited_identifier_scan": {
            "version": "scan-v1",
            "file_role": "prohibited-identifiers-scan",
            "sha256": by_role["prohibited-identifiers-scan"]["sha256"],
        },
        "access_control_policy": {
            "version": "access-v1",
            "file_role": "access-control-policy",
            "sha256": by_role["access-control-policy"]["sha256"],
        },
        "retention_policy": {
            "version": "retention-v1",
            "file_role": "retention-policy",
            "sha256": by_role["retention-policy"]["sha256"],
        },
        "withdrawal_policy": {
            "version": "withdrawal-v1",
            "file_role": "withdrawal-policy",
            "sha256": by_role["withdrawal-policy"]["sha256"],
        },
        "identity_mapping_commitment": {
            "version": "identity-v1",
            "file_role": "identity-mapping-commitment",
            "sha256": by_role["identity-mapping-commitment"]["sha256"],
        },
    }
    manifest = {
        "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "dataset_release_id": "dataset-01",
        "frozen_at": "2026-01-03T00:00:00Z",
        "release_status": "sealed",
        "files": files,
        "declared_counts": declared_counts(),
        "governance": governance,
    }
    write_json(root / "dataset-manifest.json", manifest)
    return {"manifest": manifest, "records": records}


def refresh_role(root: Path, role: str, records: list[dict]) -> None:
    manifest_path = root / "dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["role"] == role)
    write_jsonl(root / entry["path"], records)
    entry["sha256"] = sha256(root / entry["path"])
    entry["size_bytes"] = (root / entry["path"]).stat().st_size
    entry["record_count"] = len(records)
    write_json(manifest_path, manifest)


def refresh_raw_role(root: Path, role: str, raw: bytes, record_count: int) -> None:
    manifest_path = root / "dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["role"] == role)
    (root / entry["path"]).write_bytes(raw)
    entry["sha256"] = sha256(root / entry["path"])
    entry["size_bytes"] = (root / entry["path"]).stat().st_size
    entry["record_count"] = record_count
    write_json(manifest_path, manifest)


def inventory_link(by_role: dict[str, dict], role: str) -> dict:
    return {"file_role": role, "sha256": by_role[role]["sha256"]}


def build_run_bundle(root: Path, dataset_root: Path) -> dict:
    artifacts = {
        "artifacts/citefold.whl": b"wheel-bytes",
        "artifacts/migration-report.json": b"{}\n",
        "artifacts/requirements.lock": b"dependency==1.0\n",
        "artifacts/system-prompt.txt": b"system prompt\n",
        "artifacts/task-template.txt": b"task template\n",
        "artifacts/memory-pack-template.txt": b"memory template\n",
        "artifacts/evaluator-prompt.txt": b"evaluator prompt\n",
        "artifacts/tools.json": b"{}\n",
        "artifacts/tool-schemas.json": b"{}\n",
        "artifacts/recent-context.txt": b"recent context builder\n",
    }
    for relative, raw in artifacts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    roles = (
        ("artifacts/citefold.whl", "citefold-distribution", "package", "application/zip"),
        (
            "artifacts/migration-report.json",
            "migration-report",
            "json-document",
            "application/json",
        ),
        ("artifacts/requirements.lock", "agent-dependency-lock", "text", "text/plain"),
        ("artifacts/system-prompt.txt", "system-prompt", "text", "text/plain"),
        ("artifacts/task-template.txt", "task-template", "text", "text/plain"),
        (
            "artifacts/memory-pack-template.txt",
            "memory-pack-placement-template",
            "text",
            "text/plain",
        ),
        ("artifacts/evaluator-prompt.txt", "evaluator-prompt", "text", "text/plain"),
        ("artifacts/tools.json", "tool-definitions", "json-document", "application/json"),
        (
            "artifacts/tool-schemas.json",
            "tool-schemas",
            "json-document",
            "application/json",
        ),
        (
            "artifacts/recent-context.txt",
            "recent-context-builder",
            "text",
            "text/plain",
        ),
    )
    files = [
        inventory_entry(
            root,
            relative,
            role,
            artifact_kind=kind,
            access_class="run_config",
            media_type=media_type,
        )
        for relative, role, kind, media_type in roles
    ]
    by_role = {item["role"]: item for item in files}
    enabled_model = {
        "enabled": True,
        "model_id": "provider-a/model-2026-01-01",
        "immutable_model_version": "model-2026-01-01",
        "actual_upstream_provider": "provider-a",
        "immutable_route": "provider-a/route-01",
    }
    disabled_model = {
        "enabled": False,
        "model_id": None,
        "immutable_model_version": None,
        "actual_upstream_provider": None,
        "immutable_route": None,
    }
    models = {
        role: copy.deepcopy(enabled_model if role == "reader" else disabled_model)
        for role in (
            "reader",
            "observation",
            "asr",
            "vision",
            "consolidation",
            "embedding",
            "secondary_judge",
        )
    }
    manifest = {
        "schema_version": SEALED_RUN_MANIFEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "run_id": "run-01",
        "iteration_id": "iteration-01",
        "sealed_at": "2026-01-04T00:00:00Z",
        "dataset": {
            "dataset_release_id": "dataset-01",
            "manifest_path": "dataset-manifest.json",
            "manifest_sha256": sha256(dataset_root / "dataset-manifest.json"),
            "split": "hidden_test",
        },
        "files": files,
        "citefold": {
            "version": "0.2.0",
            "code_commit": "abcdef1",
            "distribution": inventory_link(by_role, "citefold-distribution"),
            "on_disk_schema_version": 2,
            "migration_state": "verified_current",
            "migration_report": inventory_link(by_role, "migration-report"),
        },
        "agent": {
            "implementation_commit": "abcdef2",
            "dependency_lock": inventory_link(by_role, "agent-dependency-lock"),
            "agent_turn_contract": "agent-turn-v1",
        },
        "models": models,
        "system_artifacts": {
            "system_prompt": inventory_link(by_role, "system-prompt"),
            "task_template": inventory_link(by_role, "task-template"),
            "memory_pack_placement_template": inventory_link(
                by_role, "memory-pack-placement-template"
            ),
            "evaluator_prompt": inventory_link(by_role, "evaluator-prompt"),
            "tool_definitions": inventory_link(by_role, "tool-definitions"),
            "tool_schemas": inventory_link(by_role, "tool-schemas"),
            "recent_context_builder": inventory_link(by_role, "recent-context-builder"),
        },
        "memory": {
            "memory_pack_mode": "bounded-cited",
            "logical_token_budget": 2200,
            "retrieval_backend": "sqlite-fts",
            "index_backend": "sqlite",
            "retrieval_config": [{"name": "top-k", "value": 8}],
            "feature_flags": [{"name": "strict-evidence", "enabled": True}],
        },
        "generation": {
            "temperature": 0.0,
            "top_p": 1.0,
            "max_output_tokens": 512,
            "seed_supported": True,
            "seed": 17,
            "timeout_seconds": 60.0,
            "retry_count": 2,
            "backoff_seconds": [1.0, 2.0],
            "fallback_policy": "none",
            "fallback_routes": [],
            "product_failure_categories": ["product-timeout"],
            "request_parameters": [],
        },
        "provider_policy": {
            "routing_mode": "fixed_provider",
            "upstream_provider": "provider-a",
            "privacy_mode": "no-training",
            "retention_policy": "zero-retention",
            "data_collection_policy": "disabled",
            "allow_provider_training": False,
            "transmission_regions": ["region-a"],
        },
        "randomization": {
            "algorithm": "hmac-sha256-v1",
            "seed": 20260804,
            "arm_labels": ["no_memory", "memory_pack"],
            "task_order_policy": "fixed-dataset-order",
            "output_blinding_algorithm": "uuid-v4-v1",
        },
        "reference_environment": {
            "operating_system": "linux",
            "operating_system_version": "test-1",
            "architecture": "x86-64",
            "python_implementation": "cpython",
            "python_version": "3.13.0",
            "cpu": "synthetic cpu",
            "memory_bytes": 17179869184,
            "storage_type": "local_filesystem",
            "storage_description": "synthetic local storage",
            "locale": "C.UTF-8",
            "timezone": "UTC",
        },
    }
    write_json(root / "sealed-run-manifest.json", manifest)
    return manifest


def rewrite_dataset_manifest(root: Path, mutate) -> None:
    path = root / "dataset-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    write_json(path, manifest)


def rewrite_run_manifest(root: Path, mutate) -> None:
    path = root / "sealed-run-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    write_json(path, manifest)


class OfficeLifeTrackBContractTest(unittest.TestCase):
    def test_all_schemas_are_valid_local_draft_2020_12(self) -> None:
        self.assertEqual([], validate_schema_bundle())
        self.assertEqual(7, len(schema_paths()))
        known_ids = set()
        references = []
        for path in schema_paths().values():
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema",
                schema["$schema"],
            )
            known_ids.add(schema["$id"])

            def collect(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key == "$ref":
                            references.append(item)
                        else:
                            collect(item)
                elif isinstance(value, list):
                    for item in value:
                        collect(item)

            collect(schema)
        self.assertTrue(references)
        self.assertTrue(
            all(
                not reference.split("#", 1)[0]
                or reference.split("#", 1)[0] in known_ids
                for reference in references
            )
        )

    def test_minimal_dataset_and_run_validate_but_remain_nonclaimable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            run_root = root / "run"
            dataset_root.mkdir()
            run_root.mkdir()
            build_dataset_bundle(dataset_root)
            build_run_bundle(run_root, dataset_root)

            dataset_report = validate_dataset_bundle(dataset_root)
            run_report = validate_run_bundle(dataset_root, run_root)

        self.assertTrue(dataset_report["validation"]["passed"], dataset_report)
        self.assertTrue(dataset_report["passed"])
        self.assertFalse(dataset_report["minimum_dataset_gates"]["passed"])
        self.assertFalse(dataset_report["claimable"])
        self.assertTrue(dataset_report["separation"]["task_inputs_and_labels_physically_separate"])
        self.assertTrue(run_report["validation"]["passed"], run_report)
        self.assertTrue(run_report["passed"])
        self.assertFalse(run_report["claimable"])
        serialized = json.dumps({"dataset": dataset_report, "run": run_report})
        self.assertNotIn(PRIVATE_SENTINEL, serialized)
        self.assertNotIn("user-01", serialized)
        self.assertNotIn(str(root), serialized)

    def test_hidden_label_fields_and_unknown_properties_fail_closed(self) -> None:
        for field_name, value in (
            ("expected_answer", PRIVATE_SENTINEL),
            ("memory_requirement", "required"),
            ("relevant_source_event_ids", ["event-01"]),
            ("ratings", [True, False]),
            ("treatment", "memory_pack"),
        ):
            with self.subTest(field_name=field_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    bundle = build_dataset_bundle(root)
                    records = copy.deepcopy(bundle["records"]["task-inputs"])
                    records[0][field_name] = value
                    refresh_role(root, "task-inputs", records)
                    report = validate_dataset_bundle(root)
                self.assertFalse(report["validation"]["passed"])
                self.assertTrue(
                    any("schema_additionalProperties" in error for error in report["validation"]["errors"])
                )
                self.assertNotIn(PRIVATE_SENTINEL, json.dumps(report))

    def test_duplicate_keys_nonfinite_and_noncanonical_jsonl_are_rejected(self) -> None:
        cases = {
            "duplicate": b'{"schema_version":"x","schema_version":"y"}\n',
            "nonfinite": b'{"schema_version":NaN}\n',
            "blank-line": b'{}\n\n',
            "array": b'[]\n',
            "noncanonical": b'{ "schema_version": "x" }\n',
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    build_dataset_bundle(root)
                    refresh_raw_role(root, "users", raw, 1)
                    report = validate_dataset_bundle(root)
                self.assertFalse(report["validation"]["passed"])

    def test_schema_invalid_values_return_reports_without_crashing(self) -> None:
        record_mutations = (
            (
                "unhashable-event-user",
                "events",
                lambda records: records[0].__setitem__("user_id", []),
            ),
            (
                "unhashable-surface-membership",
                "task-labels",
                lambda records: records[0].__setitem__(
                    "surface_memberships", [{}]
                ),
            ),
            (
                "unhashable-check-operator",
                "task-labels",
                lambda records: records[0]["deterministic_checks"][0].__setitem__(
                    "operator", []
                ),
            ),
        )
        for name, role, mutate in record_mutations:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    bundle = build_dataset_bundle(root)
                    records = copy.deepcopy(bundle["records"][role])
                    mutate(records)
                    refresh_role(root, role, records)
                    report = validate_dataset_bundle(root)
                self.assertFalse(report["validation"]["passed"], report)

        with self.subTest("invalid-dataset-manifest-shape"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                rewrite_dataset_manifest(
                    root,
                    lambda manifest: manifest.__setitem__("files", None),
                )
                report = validate_dataset_bundle(root)
            self.assertFalse(report["validation"]["passed"], report)

        with self.subTest("invalid-run-manifest-shape"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root = root / "dataset"
                run_root = root / "run"
                dataset_root.mkdir()
                run_root.mkdir()
                build_dataset_bundle(dataset_root)
                build_run_bundle(run_root, dataset_root)
                rewrite_run_manifest(
                    run_root,
                    lambda manifest: manifest.__setitem__("models", []),
                )
                report = validate_run_bundle(dataset_root, run_root)
            self.assertFalse(report["validation"]["passed"], report)

    def test_inventory_rejects_tampering_extra_paths_and_symlinks(self) -> None:
        with self.subTest("tamper"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                with (root / "payloads/task-01.txt").open("ab") as handle:
                    handle.write(b"tampered")
                report = validate_dataset_bundle(root)
            self.assertFalse(report["validation"]["passed"])
            self.assertTrue(any("inventory_" in error for error in report["validation"]["errors"]))

        with self.subTest("extra-file"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                (root / "undeclared.txt").write_text("extra", encoding="utf-8")
                report = validate_dataset_bundle(root)
            self.assertTrue(any("undeclared_file" in error for error in report["validation"]["errors"]))

        with self.subTest("invalid-json-document"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                path = root / "payloads/tool-01.json"
                path.write_bytes(b'{"duplicate":1,"duplicate":2}\n')
                manifest = json.loads((root / "dataset-manifest.json").read_text(encoding="utf-8"))
                entry = next(
                    item
                    for item in manifest["files"]
                    if item["path"] == "payloads/tool-01.json"
                )
                entry["sha256"] = sha256(path)
                entry["size_bytes"] = path.stat().st_size
                write_json(root / "dataset-manifest.json", manifest)
                report = validate_dataset_bundle(root)
            self.assertTrue(
                any("duplicate_json_key" in error for error in report["validation"]["errors"])
            )

        for unsafe_path in (
            "/etc/passwd",
            "../outside.txt",
            "a/../../outside.txt",
            "a\\outside.txt",
            "file://outside.txt",
        ):
            with self.subTest(unsafe_path=unsafe_path):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    build_dataset_bundle(root)
                    rewrite_dataset_manifest(
                        root,
                        lambda manifest: manifest["files"][0].__setitem__(
                            "path", unsafe_path
                        ),
                    )
                    report = validate_dataset_bundle(root)
                self.assertFalse(report["validation"]["passed"])
                self.assertTrue(any("unsafe_relative_path" in error or "schema_pattern" in error for error in report["validation"]["errors"]))

        if hasattr(os, "symlink"):
            with self.subTest("symlink"):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    build_dataset_bundle(root)
                    target = root / "payloads/task-01.txt"
                    backup = root / "payloads/task-01-real.txt"
                    target.rename(backup)
                    target.symlink_to(backup.name)
                    report = validate_dataset_bundle(root)
                self.assertFalse(report["validation"]["passed"])
                self.assertTrue(any("symlink_forbidden" in error for error in report["validation"]["errors"]))

        if hasattr(os, "link"):
            with self.subTest("external-hardlink"):
                with tempfile.TemporaryDirectory() as tmp:
                    parent = Path(tmp)
                    root = parent / "dataset"
                    root.mkdir()
                    build_dataset_bundle(root)
                    target = root / "payloads/task-01.txt"
                    external = parent / "external.txt"
                    external.write_bytes(target.read_bytes())
                    target.unlink()
                    os.link(external, target)
                    report = validate_dataset_bundle(root)
                self.assertFalse(report["validation"]["passed"])
                self.assertTrue(
                    any("hardlink_forbidden" in error for error in report["validation"]["errors"])
                )

    def test_snapshot_access_roles_and_private_content_are_bound(self) -> None:
        with self.subTest("snapshot-id"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = build_dataset_bundle(root)
                records = copy.deepcopy(bundle["records"]["task-inputs"])
                records[0]["snapshot_id"] = "snapshot-forged"
                refresh_role(root, "task-inputs", records)
                report = validate_dataset_bundle(root)
            self.assertTrue(
                any("snapshot_id_not_content_addressed" in error for error in report["validation"]["errors"])
            )

        with self.subTest("artifact-id-rebound"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = build_dataset_bundle(root)
                records = copy.deepcopy(bundle["records"]["task-inputs"])
                records[0]["recent_context_artifact"]["artifact_id"] = records[0][
                    "input_artifact"
                ]["artifact_id"]
                refresh_role(root, "task-inputs", records)
                report = validate_dataset_bundle(root)
            self.assertTrue(
                any("artifact_id_rebound" in error for error in report["validation"]["errors"])
            )

        with self.subTest("task-role"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                rewrite_dataset_manifest(
                    root,
                    lambda manifest: next(
                        item
                        for item in manifest["files"]
                        if item["path"] == "payloads/task-01.txt"
                    ).__setitem__("role", "arbitrary-generator-file"),
                )
                report = validate_dataset_bundle(root)
            self.assertTrue(
                any("task_artifact_role_invalid" in error for error in report["validation"]["errors"])
            )

        with self.subTest("hidden-label-copy"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                copied_path = "payloads/labels-copy.jsonl"
                (root / copied_path).write_bytes((root / "task-labels.jsonl").read_bytes())
                manifest = json.loads((root / "dataset-manifest.json").read_text(encoding="utf-8"))
                manifest["files"].append(
                    inventory_entry(
                        root,
                        copied_path,
                        "task-input-payload-label-copy",
                        artifact_kind="text",
                        access_class="generator_input",
                        media_type="application/x-ndjson",
                    )
                )
                write_json(root / "dataset-manifest.json", manifest)
                report = validate_dataset_bundle(root)
            self.assertTrue(
                any(
                    "private_content_exposed_across_access_classes" in error
                    for error in report["validation"]["errors"]
                )
            )

        with self.subTest("hidden-label-substring-in-generator-input"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = build_dataset_bundle(root)
                payload_path = root / "payloads/task-01.txt"
                payload_path.write_bytes(
                    b"prefix\n"
                    + (root / "task-labels.jsonl").read_bytes()
                    + b"suffix\n"
                )
                manifest_path = root / "dataset-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                entry = next(
                    item
                    for item in manifest["files"]
                    if item["path"] == "payloads/task-01.txt"
                )
                entry["sha256"] = sha256(payload_path)
                entry["size_bytes"] = payload_path.stat().st_size
                write_json(manifest_path, manifest)
                inputs = copy.deepcopy(bundle["records"]["task-inputs"])
                inputs[0]["input_artifact"] = artifact_ref(
                    root,
                    "payloads/task-01.txt",
                    "task-payload-01",
                    "text/plain",
                )
                refresh_role(root, "task-inputs", inputs)
                report = validate_dataset_bundle(root)
            self.assertTrue(
                any(
                    "private_content_exposed_across_access_classes" in error
                    for error in report["validation"]["errors"]
                ),
                report,
            )

        with self.subTest("untrusted-role-redacted"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                rewrite_dataset_manifest(
                    root,
                    lambda manifest: next(
                        item
                        for item in manifest["files"]
                        if item["path"] == "payloads/event-01.txt"
                    ).__setitem__("role", PRIVATE_SENTINEL),
                )
                with (root / "payloads/event-01.txt").open("ab") as handle:
                    handle.write(b"tamper")
                report = validate_dataset_bundle(root)
            self.assertNotIn(PRIVATE_SENTINEL, json.dumps(report))

    def test_pairing_scope_cutoff_partial_and_source_identity_are_enforced(self) -> None:
        mutations = []

        def missing_label(bundle):
            return "task-labels", []

        mutations.append(("missing-label", missing_label))

        def unknown_event(bundle):
            records = copy.deepcopy(bundle["records"]["task-labels"])
            records[0]["relevant_source_event_ids"] = ["event-missing"]
            records[0]["acceptable_facts"][0]["acceptable_evidence_sets"] = [["event-missing"]]
            return "task-labels", records

        mutations.append(("unknown-event", unknown_event))

        def cutoff_equal(bundle):
            records = copy.deepcopy(bundle["records"]["events"])
            records[0]["available_at"] = TASK_TIME
            return "events", records

        mutations.append(("cutoff-equal", cutoff_equal))

        def partial_event(bundle):
            records = copy.deepcopy(bundle["records"]["events"])
            records[0].update(
                {
                    "source_surface": "realtime_voice",
                    "modality": "audio",
                    "memory_bearing": False,
                    "lifecycle_state": "partial",
                    "asr_state": "partial",
                    "recallable": False,
                }
            )
            return "events", records

        mutations.append(("partial-event", partial_event))

        def duplicate_source(bundle):
            records = copy.deepcopy(bundle["records"]["events"])
            duplicate = copy.deepcopy(records[0])
            duplicate["event_id"] = "event-02"
            records.append(duplicate)
            return "events", records

        mutations.append(("duplicate-source", duplicate_source))

        def cross_scope(bundle):
            records = copy.deepcopy(bundle["records"]["task-labels"])
            records[0]["allowed_scope"]["namespace"] = "other"
            return "task-labels", records

        mutations.append(("cross-scope", cross_scope))

        for name, mutate in mutations:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    bundle = build_dataset_bundle(root)
                    role, records = mutate(bundle)
                    refresh_role(root, role, records)
                    report = validate_dataset_bundle(root)
                self.assertFalse(report["validation"]["passed"])

    def test_normalized_event_and_task_counting_cannot_be_inflated(self) -> None:
        with self.subTest("task-metadata-clone"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = build_dataset_bundle(root)
                duplicate_input_path = "payloads/task-02.txt"
                (root / duplicate_input_path).write_text(
                    PRIVATE_SENTINEL + " task\nattachment-id: unique-02",
                    encoding="utf-8",
                )
                manifest_path = root / "dataset-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["files"].append(
                    inventory_entry(
                        root,
                        duplicate_input_path,
                        "task-input-payload-02",
                        artifact_kind="text",
                        access_class="generator_input",
                        media_type="text/plain",
                    )
                )
                write_json(manifest_path, manifest)
                inputs = copy.deepcopy(bundle["records"]["task-inputs"])
                duplicate_input = copy.deepcopy(inputs[0])
                duplicate_input.update(
                    {
                        "task_id": "task-02",
                        "conversation_id": "conversation-02",
                        "task_timestamp": "2026-01-02T01:00:00Z",
                        "execution_surface": "realtime_voice",
                    }
                )
                duplicate_input["input_artifact"] = artifact_ref(
                    root,
                    duplicate_input_path,
                    "task-payload-02",
                    "text/plain",
                )
                inputs.append(duplicate_input)
                labels = copy.deepcopy(bundle["records"]["task-labels"])
                duplicate_label = copy.deepcopy(labels[0])
                duplicate_label.update(
                    {
                        "task_id": "task-02",
                        "surface_memberships": [
                            "text_chat",
                            "realtime_voice",
                            "cross_channel",
                        ],
                    }
                )
                labels.append(duplicate_label)
                refresh_role(root, "task-inputs", inputs)
                refresh_role(root, "task-labels", labels)
                report = validate_dataset_bundle(root)
            self.assertTrue(
                any(
                    "duplicate_task_counting_fingerprint" in error
                    for error in report["validation"]["errors"]
                ),
                report,
            )

        def add_second_event(
            root: Path,
            bundle: dict,
            content: str,
            *,
            retain_original_payload: bool = False,
        ) -> None:
            relative = "payloads/event-02.txt"
            (root / relative).write_text(content, encoding="utf-8")
            manifest_path = root / "dataset-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"].append(
                inventory_entry(
                    root,
                    relative,
                    "event-payload-02",
                    artifact_kind="text",
                    access_class="generator_input",
                    media_type="text/plain",
                )
            )
            write_json(manifest_path, manifest)
            events = copy.deepcopy(bundle["records"]["events"])
            second = copy.deepcopy(events[0])
            second.update(
                {
                    "event_id": "event-02",
                    "source_record_id": "source-02",
                    "payload_refs": (
                        copy.deepcopy(events[0]["payload_refs"])
                        if retain_original_payload
                        else []
                    )
                    + [
                        artifact_ref(
                            root,
                            relative,
                            "event-payload-02",
                            "text/plain",
                        )
                    ],
                }
            )
            events.append(second)
            refresh_role(root, "events", events)

        event_cases = (
            (
                "unicode-case-whitespace-variant",
                "  private-sentinel-do-not-report   EVENT  ",
                False,
                1,
            ),
            ("unique-dummy-attachment", "unique attachment", True, 1),
            ("empty-text", "   \n\t", False, 1),
            ("genuinely-distinct-normalized-text", "different memory", False, 2),
        )
        for name, content, retain_original, expected_count in event_cases:
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    bundle = build_dataset_bundle(root)
                    add_second_event(
                        root,
                        bundle,
                        content,
                        retain_original_payload=retain_original,
                    )
                    if expected_count == 2:
                        rewrite_dataset_manifest(
                            root,
                            lambda manifest: manifest["declared_counts"][
                                "hidden_test_per_user_minimums"
                            ].__setitem__("memory_bearing_events", 2),
                        )
                    report = validate_dataset_bundle(root)
                self.assertTrue(report["validation"]["passed"], report)
                self.assertEqual(
                    2,
                    report["dataset"]["record_counts"]["events"],
                )

    def test_surface_family_and_success_labels_cannot_inflate_gates(self) -> None:
        def inflated_surfaces(records):
            records[0]["surface_memberships"] = [
                "text_chat",
                "realtime_voice",
                "third_party_agents",
                "cross_channel",
            ]
            records[0]["source_surfaces"] = [
                "text_chat",
                "realtime_voice",
                "third_party_agents",
            ]

        def fake_deletion(records):
            records[0]["scenario_family"] = "deletion"

        def downgrade_required_fact(records):
            answer_set = records[0]["acceptable_answer_sets"][0]
            answer_set["required_fact_ids"] = []
            answer_set["optional_fact_ids"] = ["fact-01"]

        def empty_required_checks(records):
            records[0]["success_rule"]["required_check_ids"] = []

        def empty_hard_checks(records):
            records[0]["success_rule"]["hard_prohibition_check_ids"] = []

        def future_fact(records):
            records[0]["acceptable_facts"][0]["valid_interval"] = {
                "start_inclusive": "2027-01-01T00:00:00Z",
                "end_exclusive": "2027-01-02T00:00:00Z",
            }

        def colliding_subject_namespaces(records):
            records[0]["forbidden_items"][0]["forbidden_id"] = "fact-01"
            records[0]["deterministic_checks"][1]["subject_ref"] = "fact-01"

        def wrong_required_fact_check_type(records):
            check = records[0]["deterministic_checks"][0]
            check["type"] = "scope"
            check["operator"] = "inside_allowed_scope"

        def dummy_scope_harm_check(records):
            label = records[0]
            label["scenario_family"] = "scope_isolation"
            label["allowed_non_memory_evidence_refs"] = ["dummy-scope"]
            label["deterministic_checks"].append(
                {
                    "check_id": "check-scope-dummy",
                    "type": "scope",
                    "subject_kind": "non_memory_evidence",
                    "subject_ref": "dummy-scope",
                    "operator": "inside_allowed_scope",
                    "expected_values": [],
                    "must_pass": False,
                    "hard_prohibition": True,
                }
            )
            label["success_rule"]["hard_prohibition_check_ids"].append(
                "check-scope-dummy"
            )

        cases = (
            ("inflated-surfaces", inflated_surfaces, "surface_"),
            ("fake-deletion", fake_deletion, "deletion_family_unsubstantiated"),
            ("downgraded-required-fact", downgrade_required_fact, "required_fact_"),
            ("empty-required-checks", empty_required_checks, "required_check_"),
            ("empty-hard-checks", empty_hard_checks, "hard_prohibition_"),
            ("future-fact", future_fact, "fact_not_valid_at_task_time"),
            (
                "colliding-subject-namespaces",
                colliding_subject_namespaces,
                "subject_namespace_collision",
            ),
            (
                "wrong-required-fact-check-type",
                wrong_required_fact_check_type,
                "required_fact_unchecked",
            ),
            (
                "dummy-scope-harm-check",
                dummy_scope_harm_check,
                "scope_family_unsubstantiated",
            ),
        )
        for name, mutate, expected_error in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    bundle = build_dataset_bundle(root)
                    records = copy.deepcopy(bundle["records"]["task-labels"])
                    mutate(records)
                    refresh_role(root, "task-labels", records)
                    report = validate_dataset_bundle(root)
                self.assertFalse(report["validation"]["passed"])
                self.assertTrue(
                    any(
                        expected_error in error
                        for error in report["validation"]["errors"]
                    ),
                    report,
                )

    def test_invalidation_timeline_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = build_dataset_bundle(root)
            records = copy.deepcopy(bundle["records"]["events"])
            invalidating = copy.deepcopy(records[0])
            invalidating.update(
                {
                    "event_id": "event-02",
                    "source_record_id": "source-02",
                    "occurred_at": "2026-01-01T01:00:00Z",
                    "available_at": "2026-01-03T00:00:00Z",
                }
            )
            records[0].update(
                {
                    "lifecycle_state": "tombstoned",
                    "recallable": False,
                    "invalidated_at": "2025-12-31T23:00:00Z",
                    "invalidation_reason": "corrected",
                    "invalidated_by_event_id": "event-02",
                }
            )
            records.append(invalidating)
            refresh_role(root, "events", records)
            report = validate_dataset_bundle(root)
        self.assertTrue(
            any("invalidation_timestamp_invalid" in error for error in report["validation"]["errors"]),
            report,
        )

    def test_manifest_counts_and_governance_links_are_recomputed(self) -> None:
        with self.subTest("counts"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                rewrite_dataset_manifest(
                    root,
                    lambda manifest: manifest["declared_counts"]["users_by_split"].__setitem__(
                        "hidden_test", 99
                    ),
                )
                report = validate_dataset_bundle(root)
            self.assertTrue(any("declared_counts_mismatch" in error for error in report["validation"]["errors"]))

        with self.subTest("governance"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build_dataset_bundle(root)
                rewrite_dataset_manifest(
                    root,
                    lambda manifest: manifest["governance"]["consent_policy"].__setitem__(
                        "sha256", "0" * 64
                    ),
                )
                report = validate_dataset_bundle(root)
            self.assertTrue(any("sha256_mismatch" in error for error in report["validation"]["errors"]))

    def test_run_manifest_binds_dataset_models_and_every_input_file(self) -> None:
        mutations = (
            (
                "dataset-hash",
                lambda manifest: manifest["dataset"].__setitem__("manifest_sha256", "0" * 64),
            ),
            (
                "rolling-model",
                lambda manifest: manifest["models"]["reader"].__setitem__(
                    "immutable_model_version", "latest"
                ),
            ),
            (
                "rolling-route",
                lambda manifest: manifest["models"]["reader"].__setitem__(
                    "immutable_route", "auto"
                ),
            ),
            (
                "provider-mismatch",
                lambda manifest: manifest["provider_policy"].__setitem__(
                    "upstream_provider", "provider-b"
                ),
            ),
            (
                "cross-provider-primary-model",
                lambda manifest: manifest["models"]["reader"].__setitem__(
                    "model_id", "provider-b/model-2026-01-01"
                ),
            ),
            (
                "cross-provider-primary-route",
                lambda manifest: manifest["models"]["reader"].__setitem__(
                    "immutable_route", "provider-b/route-01"
                ),
            ),
            (
                "duplicate-feature-name",
                lambda manifest: manifest["memory"]["feature_flags"].append(
                    {"name": "strict-evidence", "enabled": False}
                ),
            ),
            (
                "duplicate-request-name",
                lambda manifest: manifest["generation"]["request_parameters"].extend(
                    [
                        {"name": "mode", "value": "a"},
                        {"name": "mode", "value": "b"},
                    ]
                ),
            ),
            (
                "rolling-fallback-route",
                lambda manifest: manifest["generation"].update(
                    {"fallback_policy": "fixed_order", "fallback_routes": ["auto"]}
                ),
            ),
            (
                "cross-provider-fallback-route",
                lambda manifest: manifest["generation"].update(
                    {
                        "fallback_policy": "fixed_order",
                        "fallback_routes": ["provider-b/route-01"],
                    }
                ),
            ),
            (
                "missing-model-role",
                lambda manifest: manifest["models"].pop("embedding"),
            ),
            (
                "reference-hash",
                lambda manifest: manifest["system_artifacts"]["system_prompt"].__setitem__(
                    "sha256", "0" * 64
                ),
            ),
            (
                "wrong-profile",
                lambda manifest: manifest.__setitem__("execution_profile_version", "banana"),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    dataset_root = root / "dataset"
                    run_root = root / "run"
                    dataset_root.mkdir()
                    run_root.mkdir()
                    build_dataset_bundle(dataset_root)
                    build_run_bundle(run_root, dataset_root)
                    rewrite_run_manifest(run_root, mutate)
                    report = validate_run_bundle(dataset_root, run_root)
                self.assertFalse(report["validation"]["passed"])

        with self.subTest("run-file-tamper"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root = root / "dataset"
                run_root = root / "run"
                dataset_root.mkdir()
                run_root.mkdir()
                build_dataset_bundle(dataset_root)
                build_run_bundle(run_root, dataset_root)
                with (run_root / "artifacts/system-prompt.txt").open("ab") as handle:
                    handle.write(b"tamper")
                report = validate_run_bundle(dataset_root, run_root)
            self.assertFalse(report["validation"]["passed"])

        with self.subTest("hidden-label-copy-into-run-config"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root = root / "dataset"
                run_root = root / "run"
                dataset_root.mkdir()
                run_root.mkdir()
                build_dataset_bundle(dataset_root)
                build_run_bundle(run_root, dataset_root)
                copied_path = run_root / "artifacts/system-prompt.txt"
                copied_path.write_bytes((dataset_root / "task-labels.jsonl").read_bytes())
                manifest_path = run_root / "sealed-run-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                entry = next(
                    item
                    for item in manifest["files"]
                    if item["role"] == "system-prompt"
                )
                entry["sha256"] = sha256(copied_path)
                entry["size_bytes"] = copied_path.stat().st_size
                manifest["system_artifacts"]["system_prompt"]["sha256"] = entry[
                    "sha256"
                ]
                write_json(manifest_path, manifest)
                report = validate_run_bundle(dataset_root, run_root)
            self.assertTrue(
                any(
                    "private_dataset_content_exposed_in_run_config" in error
                    for error in report["validation"]["errors"]
                ),
                report,
            )

        with self.subTest("hidden-label-substring-in-run-config"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root = root / "dataset"
                run_root = root / "run"
                dataset_root.mkdir()
                run_root.mkdir()
                build_dataset_bundle(dataset_root)
                build_run_bundle(run_root, dataset_root)
                copied_path = run_root / "artifacts/system-prompt.txt"
                copied_path.write_bytes(
                    b"prefix\n"
                    + (dataset_root / "task-labels.jsonl").read_bytes()
                    + b"suffix\n"
                )
                manifest_path = run_root / "sealed-run-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                entry = next(
                    item
                    for item in manifest["files"]
                    if item["role"] == "system-prompt"
                )
                entry["sha256"] = sha256(copied_path)
                entry["size_bytes"] = copied_path.stat().st_size
                manifest["system_artifacts"]["system_prompt"]["sha256"] = entry[
                    "sha256"
                ]
                write_json(manifest_path, manifest)
                report = validate_run_bundle(dataset_root, run_root)
            self.assertTrue(
                any(
                    "private_dataset_content_exposed_in_run_config" in error
                    for error in report["validation"]["errors"]
                ),
                report,
            )

        with self.subTest("hidden-label-object-wrapped-in-run-json"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root = root / "dataset"
                run_root = root / "run"
                dataset_root.mkdir()
                run_root.mkdir()
                build_dataset_bundle(dataset_root)
                build_run_bundle(run_root, dataset_root)
                hidden_label = json.loads(
                    (dataset_root / "task-labels.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()[0]
                )
                copied_path = run_root / "artifacts/tool-schemas.json"
                write_json(copied_path, {"hidden_labels_copy": [hidden_label]})
                manifest_path = run_root / "sealed-run-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                entry = next(
                    item
                    for item in manifest["files"]
                    if item["role"] == "tool-schemas"
                )
                entry["sha256"] = sha256(copied_path)
                entry["size_bytes"] = copied_path.stat().st_size
                manifest["system_artifacts"]["tool_schemas"]["sha256"] = entry[
                    "sha256"
                ]
                write_json(manifest_path, manifest)
                report = validate_run_bundle(dataset_root, run_root)
            self.assertTrue(
                any(
                    "private_dataset_content_exposed_in_run_config" in error
                    for error in report["validation"]["errors"]
                ),
                report,
            )

        with self.subTest("invalidated-dataset"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root = root / "dataset"
                run_root = root / "run"
                dataset_root.mkdir()
                run_root.mkdir()
                build_dataset_bundle(dataset_root)
                rewrite_dataset_manifest(
                    dataset_root,
                    lambda manifest: manifest.__setitem__("release_status", "invalidated"),
                )
                dataset_report = validate_dataset_bundle(dataset_root)
                build_run_bundle(run_root, dataset_root)
                report = validate_run_bundle(dataset_root, run_root)
            self.assertTrue(dataset_report["validation"]["passed"])
            self.assertFalse(dataset_report["dataset"]["status_usable_for_new_run"])
            self.assertFalse(dataset_report["passed"])
            self.assertTrue(report["dataset"]["validation_passed"])
            self.assertFalse(report["validation"]["passed"])
            self.assertTrue(any("dataset_not_available" in error for error in report["validation"]["errors"]))

        with self.subTest("dataset-change-between-validation-and-binding"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root = root / "dataset"
                run_root = root / "run"
                dataset_root.mkdir()
                run_root.mkdir()
                build_dataset_bundle(dataset_root)
                build_run_bundle(run_root, dataset_root)
                original_validate = validate_dataset_bundle

                def validate_then_replace(*args, **kwargs):
                    result = original_validate(*args, **kwargs)
                    rewrite_dataset_manifest(
                        dataset_root,
                        lambda manifest: manifest.__setitem__(
                            "frozen_at", "2026-01-03T01:00:00Z"
                        ),
                    )
                    rewrite_run_manifest(
                        run_root,
                        lambda manifest: manifest["dataset"].__setitem__(
                            "manifest_sha256",
                            sha256(dataset_root / "dataset-manifest.json"),
                        ),
                    )
                    return result

                with patch(
                    "benchmarks.officelife_track_b_contract.validate_dataset_bundle",
                    side_effect=validate_then_replace,
                ):
                    report = validate_run_bundle(dataset_root, run_root)
            self.assertTrue(
                any(
                    "dataset_changed_during_run_validation" in error
                    for error in report["validation"]["errors"]
                ),
                report,
            )

    def test_strict_minimum_gate_and_cli_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            run_root = root / "run"
            dataset_root.mkdir()
            run_root.mkdir()
            build_dataset_bundle(dataset_root)
            build_run_bundle(run_root, dataset_root)
            regular = validate_dataset_bundle(dataset_root)
            strict = validate_dataset_bundle(
                dataset_root,
                enforce_minimum_dataset_gates=True,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                normal_exit = main(["validate-dataset", str(dataset_root)])
                strict_exit = main(
                    [
                        "validate-dataset",
                        str(dataset_root),
                        "--enforce-minimum-dataset-gates",
                    ]
                )
                strict_run_exit = main(
                    [
                        "validate-run",
                        str(dataset_root),
                        str(run_root),
                        "--enforce-minimum-dataset-gates",
                    ]
                )

        self.assertTrue(regular["validation"]["passed"])
        self.assertTrue(regular["passed"])
        self.assertTrue(strict["validation"]["passed"])
        self.assertFalse(strict["passed"])
        self.assertEqual(0, normal_exit)
        self.assertEqual(3, strict_exit)
        self.assertEqual(3, strict_run_exit)

    def test_output_path_cannot_overlap_private_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_dataset_bundle(root)
            with self.assertRaisesRegex(SystemExit, "outside every private input root"):
                main(
                    [
                        "validate-dataset",
                        str(root),
                        "--output-json",
                        str(root / "report.json"),
                    ]
                )


if __name__ == "__main__":
    unittest.main()
