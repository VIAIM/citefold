from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from jsonschema import Draft202012Validator

from benchmarks.officelife_track_b_contract import (
    CONTRACT_VERSION,
    EXECUTION_PROFILE_VERSION,
    MAX_JSONL_BYTES,
    MAX_JSONL_LINE_BYTES,
    PROTOCOL_VERSION,
    _schema_runtime,
    validate_run_bundle,
)


EXECUTOR_CONTRACT_VERSION = "officelife-track-b-executor-v1"
EXECUTOR_CONFIG_ROLE = "track-b-executor-config"
EXECUTOR_CONFIG_SCHEMA_VERSION = "officelife-track-b-executor-config-v1"
WORKER_MANIFEST_SCHEMA_VERSION = "officelife-track-b-worker-manifest-v1"
WORKER_RUN_CONFIG_SCHEMA_VERSION = "officelife-track-b-worker-run-config-v1"
EXECUTION_MANIFEST_SCHEMA_VERSION = "officelife-track-b-execution-manifest-v1"
EXECUTION_BINDING_SCHEMA_VERSION = "officelife-track-b-execution-binding-v1"
ARM_OUTPUT_SCHEMA_VERSION = "officelife-track-b-arm-output-v1"
BLINDED_OUTPUT_SCHEMA_VERSION = "officelife-track-b-blinded-output-v1"
UNBLINDING_SCHEMA_VERSION = "officelife-track-b-unblinding-record-v1"
AUDIT_EVENT_SCHEMA_VERSION = "officelife-track-b-execution-audit-event-v1"

EXECUTOR_SCHEMA_PATH = (
    Path(__file__).with_name("schemas")
    / "officelife_track_b_executor"
    / "v1"
    / "execution.schema.json"
)
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_AGGREGATE_BYTES = 128 * 1024 * 1024
MIN_AGGREGATE_RECORD_BYTES = 4 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_JSON_DEPTH = 64
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)

WORKER_RUN_ROLES = {
    "citefold-distribution",
    "agent-dependency-lock",
    "system-prompt",
    "task-template",
    "memory-pack-placement-template",
    "tool-definitions",
    "tool-schemas",
    "recent-context-builder",
    "qualification-plan",
    EXECUTOR_CONFIG_ROLE,
}
WORKSPACE_RUN_ROLES = WORKER_RUN_ROLES - {"qualification-plan"}
WORKER_DERIVED_ENTRIES = {
    "task-inputs": ("task-inputs.jsonl", "derived", "task-inputs", "derived"),
    "preflight-report": ("preflight-report.json", "derived", None, "derived"),
    "worker-run-config": ("worker-run-config.json", "derived", None, "derived"),
    "source-sealed-run-manifest": (
        "source-sealed-run-manifest.json",
        "run",
        None,
        "run_config",
    ),
}
FORBIDDEN_WORKER_TOKENS = (
    "task-label",
    "evaluator",
    "governance",
    "identity-vault",
    "identity_mapping",
    "annotation-codebook",
)
TASK_ARTIFACT_ACCESS = {
    "input_artifact": "generator_input",
    "recent_context_artifact": "generator_input",
    "tool_fixture_artifact": "generator_input",
    "snapshot_artifact": "executor_input",
}
ARM_NAMES = ("no_memory", "memory_pack")
RETRYABLE_INFRASTRUCTURE_CATEGORIES = {
    "fallback_forbidden",
    "handler_output_invalid",
    "memory_pack_treatment_missing",
    "no_memory_treatment_contaminated",
    "output-parsing",
    "provider-availability",
    "provider-transport",
    "provider_identity_mismatch",
    "response-decoding",
}


class ExecutorValidationError(ValueError):
    """Raised when an executor artifact fails closed validation."""


class HandlerInfrastructureError(RuntimeError):
    """A frozen, retryable infrastructure failure reported by a handler."""

    def __init__(self, category: str) -> None:
        if not _valid_id(category):
            raise ValueError("infrastructure error category must be a safe identifier")
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class ArmRequest:
    execution_id: str
    attempt_id: str
    attempt_index: int
    task_id: str
    memory_mode: str
    execution_order: int
    idempotency_key: str
    paired_input_sha256: str
    request_sha256: str
    memory_pack_present: bool
    workspace_root: Path
    input_path: Path
    recent_context_path: Path
    tool_fixture_path: Path
    snapshot_path: Path
    run_artifact_paths: dict[str, Path]
    run_config: dict[str, Any]
    task_input: dict[str, Any]


@dataclass(frozen=True)
class HandlerResult:
    outcome: str
    content: str | None
    actual_model_id: str
    actual_model_version: str
    actual_upstream_provider: str
    actual_route: str
    fallback_used: bool
    memory_pack: dict[str, Any] | None
    trace: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    error_category: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "content": self.content,
            "actual_model_id": self.actual_model_id,
            "actual_model_version": self.actual_model_version,
            "actual_upstream_provider": self.actual_upstream_provider,
            "actual_route": self.actual_route,
            "fallback_used": self.fallback_used,
            "memory_pack": deepcopy(self.memory_pack),
            "trace": deepcopy(self.trace),
            "usage": deepcopy(self.usage),
            "error_category": self.error_category,
        }


class ControlledAgentHandler(Protocol):
    handler_id: str

    def execute(self, request: ArmRequest) -> HandlerResult:
        ...


def prepare_worker_bundle(
    dataset_root: Path,
    run_root: Path,
    worker_root: Path,
    *,
    enforce_minimum_dataset_gates: bool = True,
) -> dict[str, Any]:
    """Create a label-free worker bundle in a custodian-only preparation phase.

    This function necessarily validates the hidden labels. A qualifying operator
    must invoke the worker later in a different process with no access to the
    source dataset root. The generated bundle never claims that OS isolation was
    enforced.
    """

    dataset = _resolved_directory(dataset_root, "dataset root")
    run = _resolved_directory(run_root, "run root")
    destination = worker_root.expanduser().resolve(strict=False)
    _require_distinct_roots((dataset, run, destination))
    if destination.exists():
        raise FileExistsError(f"worker root already exists: {destination.name}")

    preflight = validate_run_bundle(
        dataset,
        run,
        enforce_minimum_dataset_gates=enforce_minimum_dataset_gates,
    )
    if not preflight.get("passed"):
        raise ExecutorValidationError("dataset and sealed-run preflight did not pass")

    dataset_manifest_raw = _read_regular(dataset, "dataset-manifest.json", MAX_JSON_BYTES)
    run_manifest_raw = _read_regular(run, "sealed-run-manifest.json", MAX_JSON_BYTES)
    if _sha256(dataset_manifest_raw) != preflight["dataset"]["manifest_sha256"]:
        raise ExecutorValidationError("dataset changed after preflight")
    if _sha256(run_manifest_raw) != preflight["run"]["manifest_sha256"]:
        raise ExecutorValidationError("run changed after preflight")
    dataset_manifest = _decode_json_object(dataset_manifest_raw)
    run_manifest = _decode_json_object(run_manifest_raw)
    dataset_entries = _entries_by_role(dataset_manifest, "dataset")
    run_entries = _entries_by_role(run_manifest, "run")

    executor_entry = run_entries.get(EXECUTOR_CONFIG_ROLE)
    if executor_entry is None:
        raise ExecutorValidationError("sealed run is missing the executor config")
    executor_config_raw = _read_inventory_file(run, executor_entry)
    executor_config = _decode_json_object(executor_config_raw)
    _require_schema(executor_config, "executorConfig")
    _validate_frozen_executor_inputs(run_manifest, executor_config)

    task_inputs_entry = dataset_entries.get("task-inputs")
    if task_inputs_entry is None:
        raise ExecutorValidationError("dataset is missing task inputs")
    task_inputs_raw = _read_inventory_file(dataset, task_inputs_entry)
    task_inputs = _decode_canonical_jsonl(
        task_inputs_raw,
        line_limit=MAX_JSONL_LINE_BYTES,
    )
    task_validator = _task_input_validator()
    selected_tasks: list[dict[str, Any]] = []
    for record in task_inputs:
        errors = sorted(task_validator.iter_errors(record), key=lambda item: list(item.path))
        if errors:
            raise ExecutorValidationError("task input record failed its frozen schema")
        if record.get("split") == "hidden_test":
            selected_tasks.append(record)
    if not selected_tasks:
        raise ExecutorValidationError("worker bundle has no hidden-test task inputs")
    _aggregate_record_limit(len(selected_tasks) * len(ARM_NAMES))
    task_ids = [str(record["task_id"]) for record in selected_tasks]
    if len(set(task_ids)) != len(task_ids):
        raise ExecutorValidationError("worker task IDs are not unique")

    dataset_entries_by_path = _entries_by_path(dataset_manifest, "dataset")
    run_entries_by_path = _entries_by_path(run_manifest, "run")
    selected_dataset_paths: dict[str, dict[str, Any]] = {}
    for task in selected_tasks:
        for field_name, expected_access in TASK_ARTIFACT_ACCESS.items():
            reference = task[field_name]
            entry = dataset_entries_by_path.get(reference["path"])
            if entry is None or not _reference_matches(reference, entry):
                raise ExecutorValidationError("task artifact reference changed after preflight")
            if entry.get("access_class") != expected_access:
                raise ExecutorValidationError("task artifact access class is not worker-safe")
            selected_dataset_paths[str(entry["path"])] = entry

    selected_run_entries: dict[str, dict[str, Any]] = {}
    for role in sorted(WORKER_RUN_ROLES):
        entry = run_entries.get(role)
        if entry is None:
            raise ExecutorValidationError(f"sealed run is missing required worker role: {role}")
        if entry.get("access_class") != "run_config":
            raise ExecutorValidationError("worker run artifact has an invalid access class")
        selected_run_entries[role] = entry

    run_config = _build_worker_run_config(run_manifest, executor_config)
    _require_schema(run_config, "workerRunConfig")
    preflight_raw = _canonical_json(preflight, pretty=True)
    selected_task_raw = b"".join(_canonical_json(record) for record in selected_tasks)
    if not selected_task_raw.endswith(b"\n"):
        raise AssertionError("canonical JSONL encoding must end with LF")
    if len(selected_task_raw) > MAX_JSONL_BYTES:
        raise ExecutorValidationError("worker task inputs exceed the frozen JSONL limit")

    _mkdir_private(destination.parent)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.prepare-", dir=str(destination.parent))
    )
    try:
        worker_entries: list[dict[str, Any]] = []
        _write_new_file(temporary / "task-inputs.jsonl", selected_task_raw)
        worker_entries.append(
            _worker_entry(
                temporary,
                "task-inputs.jsonl",
                "task-inputs",
                "application/x-ndjson",
                source_bundle="derived",
                source_role="task-inputs",
                source_access_class="derived",
            )
        )
        _write_new_file(temporary / "preflight-report.json", preflight_raw)
        worker_entries.append(
            _worker_entry(
                temporary,
                "preflight-report.json",
                "preflight-report",
                "application/json",
                source_bundle="derived",
                source_role=None,
                source_access_class="derived",
            )
        )
        run_config_raw = _canonical_json(run_config, pretty=True)
        _write_new_file(temporary / "worker-run-config.json", run_config_raw)
        worker_entries.append(
            _worker_entry(
                temporary,
                "worker-run-config.json",
                "worker-run-config",
                "application/json",
                source_bundle="derived",
                source_role=None,
                source_access_class="derived",
            )
        )
        _write_new_file(
            temporary / "source-sealed-run-manifest.json",
            run_manifest_raw,
        )
        worker_entries.append(
            _worker_entry(
                temporary,
                "source-sealed-run-manifest.json",
                "source-sealed-run-manifest",
                "application/json",
                source_bundle="run",
                source_role=None,
                source_access_class="run_config",
            )
        )

        occupied_paths = {
            "task-inputs.jsonl",
            "preflight-report.json",
            "worker-run-config.json",
            "source-sealed-run-manifest.json",
        }
        occupied_roles = {
            "task-inputs",
            "preflight-report",
            "worker-run-config",
            "source-sealed-run-manifest",
        }
        for relative, entry in sorted(selected_dataset_paths.items()):
            if relative in occupied_paths or str(entry["role"]) in occupied_roles:
                raise ExecutorValidationError("worker dataset artifact collides with a derived artifact")
            raw = _read_inventory_file(dataset, entry)
            _write_new_file(temporary / relative, raw)
            worker_entries.append(
                _worker_entry(
                    temporary,
                    relative,
                    str(entry["role"]),
                    str(entry["media_type"]),
                    source_bundle="dataset",
                    source_role=str(entry["role"]),
                    source_access_class=str(entry["access_class"]),
                )
            )
            occupied_paths.add(relative)
            occupied_roles.add(str(entry["role"]))

        for role, entry in sorted(selected_run_entries.items()):
            relative = str(entry["path"])
            if relative in occupied_paths or role in occupied_roles:
                raise ExecutorValidationError("worker run artifact collides with another artifact")
            if run_entries_by_path.get(relative) is not entry:
                raise ExecutorValidationError("run inventory path mapping changed")
            raw = _read_inventory_file(run, entry)
            _write_new_file(temporary / relative, raw)
            worker_entries.append(
                _worker_entry(
                    temporary,
                    relative,
                    role,
                    str(entry["media_type"]),
                    source_bundle="run",
                    source_role=role,
                    source_access_class="run_config",
                )
            )
            occupied_paths.add(relative)
            occupied_roles.add(role)

        reasons = _nonqualification_reasons(executor_config)
        worker_manifest = {
            "schema_version": WORKER_MANIFEST_SCHEMA_VERSION,
            "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
            "contract_version": CONTRACT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "execution_profile_version": EXECUTION_PROFILE_VERSION,
            "generated_at": _utc_now(),
            "dataset_release_id": dataset_manifest["dataset_release_id"],
            "run_id": run_manifest["run_id"],
            "iteration_id": run_manifest["iteration_id"],
            "selected_split": "hidden_test",
            "source_dataset_manifest_sha256": _sha256(dataset_manifest_raw),
            "source_run_manifest_sha256": _sha256(run_manifest_raw),
            "source_task_inputs_sha256": _sha256(task_inputs_raw),
            "preflight_report_sha256": _sha256(preflight_raw),
            "task_ids": task_ids,
            "files": sorted(worker_entries, key=lambda item: item["path"]),
            "qualification_eligible": False,
            "nonqualification_reasons": reasons,
        }
        _require_schema(worker_manifest, "workerManifest")
        _write_new_file(
            temporary / "worker-manifest.json",
            _canonical_json(worker_manifest, pretty=True),
        )
        validation = validate_worker_bundle(temporary)
        if not validation["passed"]:
            raise ExecutorValidationError("prepared worker bundle failed self-validation")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return validate_worker_bundle(destination)


def validate_worker_bundle(worker_root: Path) -> dict[str, Any]:
    errors: list[str] = []
    root = worker_root.expanduser().resolve(strict=False)
    try:
        if not root.is_dir():
            raise ExecutorValidationError("worker root is not a directory")
        manifest_raw = _read_regular(root, "worker-manifest.json", MAX_JSON_BYTES)
        manifest = _decode_json_object(manifest_raw)
        errors.extend(_schema_errors(manifest, "workerManifest", "worker-manifest"))
        entries = _validate_inventory(root, manifest, "worker-manifest.json", errors)
        _validate_worker_semantics(root, manifest, entries, errors)
    except (OSError, ValueError, ExecutorValidationError):
        errors.append("worker_bundle# unreadable_or_invalid")
        manifest_raw = None
        manifest = {}
    validation_passed = not errors
    reasons = manifest.get("nonqualification_reasons", [])
    return {
        "schema_version": "officelife-track-b-worker-validation-v1",
        "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
        "artifact_scope": "private-label-free-worker-preflight",
        "private": True,
        "claimable": False,
        "validation_passed": validation_passed,
        "errors": sorted(set(errors)),
        "worker_manifest_sha256": _sha256(manifest_raw) if manifest_raw is not None else None,
        "task_count": len(manifest.get("task_ids", [])) if isinstance(manifest, dict) else 0,
        "qualification_eligible": False,
        "nonqualification_reasons": list(reasons) if isinstance(reasons, list) else [],
        "passed": validation_passed,
    }


def execute_worker_bundle(
    worker_root: Path,
    execution_root: Path,
    handler: ControlledAgentHandler,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Execute a prepared bundle with a test-only Python callable adapter.

    The callable adapter intentionally makes every artifact non-qualifying. A
    formal run must replace it with an independently launched, OS-sandboxed
    worker while retaining this byte-level request/result contract.
    """

    worker = _resolved_directory(worker_root, "worker root")
    destination = execution_root.expanduser().resolve(strict=False)
    _require_distinct_roots((worker, destination))
    loaded = _load_worker_bundle(worker)
    config = loaded["run_config"]["executor_config"]
    if config["handler_protocol"] != "callable-test-v1":
        raise ExecutorValidationError(
            "this draft implements only the explicitly non-qualifying callable test adapter"
        )
    if getattr(handler, "handler_id", None) != config["handler_id"]:
        raise ExecutorValidationError("handler identity does not match the sealed executor config")
    expected_arm_count = len(loaded["tasks"]) * len(ARM_NAMES)
    aggregate_record_limit = _aggregate_record_limit(expected_arm_count)

    if os.path.lexists(destination / "execution-manifest.json"):
        existing = validate_execution_bundle(worker, destination)
        if not existing["validation_passed"]:
            raise ExecutorValidationError("existing execution bundle failed validation")
        return existing

    _mkdir_private(destination.parent)
    destination.mkdir(parents=False, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    lock_path = destination.with_name(f".{destination.name}.executor.lock")
    lock_fd = _acquire_execution_lock(lock_path)

    now = clock or (lambda: datetime.now(timezone.utc))
    try:
        _ensure_execution_binding(destination, loaded)
        _preflight_partial_execution(destination, loaded)
        arm_outputs: list[dict[str, Any]] = []
        run_config = loaded["run_config"]
        for task in loaded["tasks"]:
            order = _arm_order(run_config, str(task["task_id"]))
            for execution_order, arm in enumerate(order):
                output = _execute_arm(
                    worker,
                    destination,
                    task,
                    arm,
                    execution_order,
                    run_config,
                    handler,
                    aggregate_record_limit=aggregate_record_limit,
                    sleeper=sleeper,
                    clock=now,
                )
                arm_outputs.append(output)

        _remove_directory_tree(destination, ".work")
        _write_aggregates(destination, arm_outputs)
        status = (
            "complete"
            if all(item["outcome"] != "infrastructure_error" for item in arm_outputs)
            else "incomplete"
        )
        nonqualification = sorted(
            set(loaded["manifest"]["nonqualification_reasons"])
            | {"callable_handler_test_only"}
        )
        files = _execution_inventory(destination)
        audit_events = _decode_canonical_jsonl(
            _read_regular(
                destination,
                "execution-audit.jsonl",
                MAX_AGGREGATE_BYTES,
            ),
            line_limit=aggregate_record_limit,
        )
        manifest = {
            "schema_version": EXECUTION_MANIFEST_SCHEMA_VERSION,
            "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
            "contract_version": CONTRACT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "execution_profile_version": EXECUTION_PROFILE_VERSION,
            "generated_at": _format_time(now()),
            "worker_manifest_sha256": loaded["manifest_sha256"],
            "run_id": run_config["run_id"],
            "iteration_id": run_config["iteration_id"],
            "execution_status": status,
            "expected_task_count": len(loaded["tasks"]),
            "expected_arm_count": len(loaded["tasks"]) * 2,
            "completed_arm_count": sum(
                item["outcome"] != "infrastructure_error" for item in arm_outputs
            ),
            "audit_chain_head": audit_events[-1]["event_sha256"],
            "files": files,
            "qualification_eligible": False,
            "nonqualification_reasons": nonqualification,
            "claimable": False,
        }
        _require_schema(manifest, "executionManifest")
        _write_new_file(
            destination / "execution-manifest.json",
            _canonical_json(manifest, pretty=True),
        )
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    return validate_execution_bundle(worker, destination)


def validate_execution_bundle(
    worker_root: Path,
    execution_root: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    worker_report = validate_worker_bundle(worker_root)
    worker: dict[str, Any] | None = None
    if not worker_report["passed"]:
        errors.append("execution# worker_bundle_invalid")
    else:
        try:
            worker = _load_worker_bundle(_resolved_directory(worker_root, "worker root"))
        except (OSError, ValueError, ExecutorValidationError):
            errors.append("execution# worker_bundle_changed")
    root = execution_root.expanduser().resolve(strict=False)
    try:
        manifest_raw = _read_regular(root, "execution-manifest.json", MAX_JSON_BYTES)
        manifest = _decode_json_object(manifest_raw)
        errors.extend(_schema_errors(manifest, "executionManifest", "execution-manifest"))
        entries = _validate_inventory(root, manifest, "execution-manifest.json", errors)
        _validate_execution_semantics(
            root,
            manifest,
            worker_report,
            worker,
            entries,
            errors,
        )
    except (OSError, ValueError, ExecutorValidationError):
        errors.append("execution_bundle# unreadable_or_invalid")
        manifest_raw = None
        manifest = {}
    validation_passed = not errors
    status = manifest.get("execution_status") if isinstance(manifest, dict) else None
    reasons = manifest.get("nonqualification_reasons", []) if isinstance(manifest, dict) else []
    return {
        "schema_version": "officelife-track-b-execution-validation-v1",
        "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
        "artifact_scope": "private-controlled-execution",
        "private": True,
        "claimable": False,
        "validation_passed": validation_passed,
        "errors": sorted(set(errors)),
        "execution_manifest_sha256": _sha256(manifest_raw) if manifest_raw is not None else None,
        "execution_status": status,
        "qualification_eligible": False,
        "nonqualification_reasons": list(reasons) if isinstance(reasons, list) else [],
        "passed": validation_passed and status == "complete",
    }


def _acquire_execution_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ExecutorValidationError("execution lock is unsafe or unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise ExecutorValidationError("execution lock is not a private regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ExecutorValidationError(
                    "another executor owns this execution root"
                ) from exc
            raise
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _aggregate_record_limit(expected_arm_count: int) -> int:
    if (
        isinstance(expected_arm_count, bool)
        or not isinstance(expected_arm_count, int)
        or expected_arm_count < 1
    ):
        raise ExecutorValidationError("execution arm count is invalid")
    limit = MAX_AGGREGATE_BYTES // expected_arm_count
    if limit < MIN_AGGREGATE_RECORD_BYTES:
        raise ExecutorValidationError(
            "execution has too many arms for the aggregate artifact envelope"
        )
    return limit


def _load_worker_bundle(root: Path) -> dict[str, Any]:
    report = validate_worker_bundle(root)
    if not report["passed"]:
        raise ExecutorValidationError("worker bundle preflight did not pass")
    manifest_raw = _read_regular(root, "worker-manifest.json", MAX_JSON_BYTES)
    manifest = _decode_json_object(manifest_raw)
    tasks = _decode_canonical_jsonl(
        _read_regular(root, "task-inputs.jsonl", MAX_JSONL_BYTES),
        line_limit=MAX_JSONL_LINE_BYTES,
    )
    run_config = _decode_json_object(
        _read_regular(root, "worker-run-config.json", MAX_JSON_BYTES)
    )
    return {
        "manifest": manifest,
        "manifest_sha256": _sha256(manifest_raw),
        "tasks": tasks,
        "run_config": run_config,
    }


def _build_execution_binding(worker: dict[str, Any]) -> dict[str, Any]:
    run_config = worker["run_config"]
    executor_config = run_config["executor_config"]
    binding = {
        "schema_version": EXECUTION_BINDING_SCHEMA_VERSION,
        "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
        "worker_manifest_sha256": worker["manifest_sha256"],
        "run_id": run_config["run_id"],
        "iteration_id": run_config["iteration_id"],
        "handler_id": executor_config["handler_id"],
        "handler_protocol": executor_config["handler_protocol"],
        "snapshot_adapter_id": executor_config["snapshot_adapter_id"],
        "worker_isolation": executor_config["worker_isolation"],
    }
    _require_schema(binding, "executionBinding")
    return binding


def _ensure_execution_binding(root: Path, worker: dict[str, Any]) -> None:
    expected = _build_execution_binding(worker)
    relative = "execution-binding.json"
    path = root / relative
    if os.path.lexists(path):
        raw = _read_regular(root, relative, MAX_JSON_BYTES)
        actual = _decode_json_object(raw)
        if (
            _schema_errors(actual, "executionBinding", "execution-binding")
            or raw != _canonical_json(actual, pretty=True)
            or actual != expected
        ):
            raise ExecutorValidationError(
                "stored worker binding conflicts with frozen execution"
            )
        return
    if _scan_regular_files(root, exclude=set()):
        raise ExecutorValidationError(
            "partial execution artifacts exist without a worker binding"
        )
    _write_new_file(path, _canonical_json(expected, pretty=True))


def _preflight_partial_execution(root: Path, worker: dict[str, Any]) -> None:
    _remove_directory_tree(root, ".work")
    run_config = worker["run_config"]
    aggregate_record_limit = _aggregate_record_limit(
        len(worker["tasks"]) * len(ARM_NAMES)
    )
    expected_arms: list[tuple[dict[str, Any], str, int, str]] = []
    allowed = {
        "execution-binding.json",
        "arm-outputs.jsonl",
        "blinded-outputs.jsonl",
        "unblinding-map.jsonl",
        "execution-audit.jsonl",
    }
    retry_count = run_config["generation"]["retry_count"]
    for task in worker["tasks"]:
        task_id = str(task["task_id"])
        for execution_order, arm in enumerate(_arm_order(run_config, task_id)):
            execution_id = _execution_id(run_config, task_id, arm)
            expected_arms.append((task, arm, execution_order, execution_id))
            allowed.update(
                {
                    f"arms/{execution_id}/request.json",
                    f"arms/{execution_id}/result.json",
                    f"traces/{execution_id}.json",
                    *{
                        f"arms/{execution_id}/attempts/{index:03d}.json"
                        for index in range(retry_count + 1)
                    },
                }
            )
    actual = _scan_regular_files(root, exclude=set())
    if not actual.issubset(allowed):
        raise ExecutorValidationError(
            "partial execution contains an unexpected artifact"
        )
    for task, arm, execution_order, execution_id in expected_arms:
        _preflight_partial_arm(
            root,
            task,
            arm,
            execution_order,
            execution_id,
            run_config,
            aggregate_record_limit,
        )


def _preflight_partial_arm(
    root: Path,
    task: dict[str, Any],
    arm: str,
    execution_order: int,
    execution_id: str,
    run_config: dict[str, Any],
    aggregate_record_limit: int,
) -> None:
    task_id = str(task["task_id"])
    request_relative = f"arms/{execution_id}/request.json"
    result_relative = f"arms/{execution_id}/result.json"
    trace_relative = f"traces/{execution_id}.json"
    arm_root = root / "arms" / execution_id
    request_exists = os.path.lexists(root / request_relative)
    result_exists = os.path.lexists(root / result_relative)
    trace_exists = os.path.lexists(root / trace_relative)
    attempt_names = _existing_attempt_names(
        arm_root,
        run_config["generation"]["retry_count"],
    )
    if not request_exists:
        if result_exists or trace_exists or attempt_names:
            raise ExecutorValidationError(
                "stored arm request is missing for partial execution"
            )
        return

    request_raw = _read_regular(root, request_relative, MAX_JSON_BYTES)
    request = _decode_json_object(request_raw)
    request_errors = _arm_request_errors(
        request_raw,
        request,
        execution_id=execution_id,
        task_id=task_id,
        arm=arm,
        execution_order=execution_order,
        paired_input_sha256=_paired_input_sha256(task, run_config),
    )
    if request_errors:
        raise ExecutorValidationError(
            "stored arm request conflicts with frozen execution"
        )

    receipts: list[dict[str, Any]] = []
    for attempt_index in range(len(attempt_names)):
        relative = f"arms/{execution_id}/attempts/{attempt_index:03d}.json"
        raw = _read_regular(root, relative, MAX_JSON_BYTES)
        receipt = _decode_json_object(raw)
        if _attempt_receipt_errors(
            raw,
            receipt,
            execution_id=execution_id,
            attempt_index=attempt_index,
            arm=arm,
            run_config=run_config,
        ):
            raise ExecutorValidationError(
                "stored attempt conflicts with frozen execution"
            )
        receipts.append(receipt)
    if _attempt_sequence_errors(
        receipts,
        run_config["generation"]["retry_count"],
    ):
        raise ExecutorValidationError(
            "stored attempt sequence conflicts with frozen execution"
        )
    request_sha256 = _sha256(_canonical_json(request))
    if (
        receipts
        and _receipt_is_terminal(
            receipts[-1],
            len(receipts),
            run_config["generation"]["retry_count"],
        )
        and not _handler_result_fits_artifact_envelope(
            receipts[-1],
            request,
            request_sha256,
            len(receipts),
            aggregate_record_limit,
        )
    ):
        raise ExecutorValidationError(
            "stored attempt exceeds the artifact envelope"
        )

    if result_exists:
        output = _decode_json_object(
            _read_regular(root, result_relative, MAX_JSON_BYTES)
        )
        errors: list[str] = []
        _validate_one_arm(
            root,
            task,
            arm,
            execution_order,
            run_config,
            output,
            errors,
            aggregate_record_limit=aggregate_record_limit,
        )
        if errors:
            raise ExecutorValidationError(
                "stored arm result conflicts with frozen execution"
            )
        return

    if trace_exists:
        if not receipts or not _receipt_is_terminal(
            receipts[-1],
            len(receipts),
            run_config["generation"]["retry_count"],
        ):
            raise ExecutorValidationError(
                "stored trace has no terminal attempt receipt"
            )
        _, expected_trace_relative, expected_trace_raw = _arm_result_payload(
            request,
            request_sha256,
            receipts[-1],
            len(receipts),
        )
        if (
            expected_trace_relative != trace_relative
            or _read_regular(root, trace_relative, MAX_JSON_BYTES)
            != expected_trace_raw
        ):
            raise ExecutorValidationError(
                "stored trace conflicts with frozen execution"
            )


def _validate_worker_semantics(
    root: Path,
    manifest: dict[str, Any],
    entries: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    if any(
        token in f"{entry.get('path', '')} {entry.get('role', '')}".lower()
        for entry in entries.values()
        for token in FORBIDDEN_WORKER_TOKENS
    ):
        errors.append("worker-manifest# forbidden_private_role_or_path")
    allowed_access = {"generator_input", "executor_input", "run_config", "derived"}
    if any(entry.get("source_access_class") not in allowed_access for entry in entries.values()):
        errors.append("worker-manifest# forbidden_access_class")
    required_roles = {
        "task-inputs",
        "preflight-report",
        "worker-run-config",
        "source-sealed-run-manifest",
        *WORKER_RUN_ROLES,
    }
    missing_roles = required_roles - set(entries)
    if missing_roles:
        errors.append("worker-manifest# missing_required_role")
    for role, expected in WORKER_DERIVED_ENTRIES.items():
        entry = entries.get(role)
        if entry is None:
            continue
        actual = (
            entry.get("path"),
            entry.get("source_bundle"),
            entry.get("source_role"),
            entry.get("source_access_class"),
        )
        if actual != expected:
            errors.append("worker-manifest# canonical_path_mismatch")
    for role in WORKER_RUN_ROLES:
        entry = entries.get(role)
        if entry is not None and (
            entry.get("source_bundle") != "run"
            or entry.get("source_role") != role
            or entry.get("source_access_class") != "run_config"
        ):
            errors.append("worker-manifest# run_provenance_mismatch")

    task_entry = entries.get("task-inputs")
    run_config_entry = entries.get("worker-run-config")
    preflight_entry = entries.get("preflight-report")
    source_run_entry = entries.get("source-sealed-run-manifest")
    if (
        task_entry is None
        or run_config_entry is None
        or preflight_entry is None
        or source_run_entry is None
    ):
        return
    try:
        tasks = _decode_canonical_jsonl(
            _read_regular(root, str(task_entry["path"]), MAX_JSONL_BYTES),
            line_limit=MAX_JSONL_LINE_BYTES,
        )
        run_config = _decode_json_object(
            _read_regular(root, str(run_config_entry["path"]), MAX_JSON_BYTES)
        )
        preflight_raw = _read_regular(root, str(preflight_entry["path"]), MAX_JSON_BYTES)
        preflight = _decode_json_object(preflight_raw)
        source_run_raw = _read_regular(
            root,
            str(source_run_entry["path"]),
            MAX_JSON_BYTES,
        )
        source_run_manifest = _decode_json_object(source_run_raw)
    except (OSError, ValueError, ExecutorValidationError):
        errors.append("worker-manifest# derived_artifact_invalid")
        return
    errors.extend(_schema_errors(run_config, "workerRunConfig", "worker-run-config"))
    sealed_schemas, sealed_registry = _schema_runtime()
    if list(
        Draft202012Validator(
            sealed_schemas["sealed-run-manifest"],
            registry=sealed_registry,
        ).iter_errors(source_run_manifest)
    ):
        errors.append("source-sealed-run-manifest# schema_invalid")
    if _sha256(source_run_raw) != manifest.get("source_run_manifest_sha256"):
        errors.append("worker-manifest# source_run_manifest_sha256_mismatch")
    source_run_entries = _entries_by_role(source_run_manifest, "source run")
    for role in WORKER_RUN_ROLES:
        worker_entry = entries.get(role)
        source_entry = source_run_entries.get(role)
        if (
            worker_entry is None
            or source_entry is None
            or any(
                worker_entry.get(field) != source_entry.get(field)
                for field in ("path", "sha256", "size_bytes", "media_type")
            )
        ):
            errors.append("worker-manifest# run_artifact_binding_mismatch")
            break
    task_validator = _task_input_validator()
    for record in tasks:
        if list(task_validator.iter_errors(record)):
            errors.append("task-inputs# schema_invalid")
            break
    task_ids = [record.get("task_id") for record in tasks]
    if task_ids != manifest.get("task_ids") or len(set(task_ids)) != len(task_ids):
        errors.append("worker-manifest# task_identity_mismatch")
    if any(record.get("split") != "hidden_test" for record in tasks):
        errors.append("task-inputs# non_hidden_task_exposed")
    if _sha256(preflight_raw) != manifest.get("preflight_report_sha256"):
        errors.append("worker-manifest# preflight_sha256_mismatch")
    preflight_validation = preflight.get("validation")
    preflight_dataset = preflight.get("dataset")
    preflight_run = preflight.get("run")
    source_dataset = source_run_manifest.get("dataset")
    if (
        preflight.get("passed") is not True
        or preflight.get("private") is not True
        or preflight.get("claimable") is not False
        or not isinstance(preflight_validation, dict)
        or preflight_validation.get("passed") is not True
        or not isinstance(preflight_dataset, dict)
        or preflight_dataset.get("validation_passed") is not True
    ):
        errors.append("preflight-report# preflight_not_passed")
    if (
        not isinstance(preflight_dataset, dict)
        or preflight_dataset.get("manifest_sha256")
        != manifest.get("source_dataset_manifest_sha256")
        or not isinstance(preflight_run, dict)
        or preflight_run.get("manifest_sha256")
        != manifest.get("source_run_manifest_sha256")
    ):
        errors.append("preflight-report# source_manifest_binding_mismatch")
    if (
        not isinstance(source_dataset, dict)
        or source_dataset.get("manifest_sha256")
        != manifest.get("source_dataset_manifest_sha256")
    ):
        errors.append("source-sealed-run-manifest# dataset_binding_mismatch")
    if (
        source_run_manifest.get("run_id") != manifest.get("run_id")
        or source_run_manifest.get("iteration_id") != manifest.get("iteration_id")
    ):
        errors.append("source-sealed-run-manifest# run_identity_mismatch")
    if run_config.get("run_id") != manifest.get("run_id") or run_config.get(
        "iteration_id"
    ) != manifest.get("iteration_id"):
        errors.append("worker-manifest# run_identity_mismatch")
    try:
        _validate_frozen_worker_config(run_config)
    except ExecutorValidationError:
        errors.append("worker-run-config# frozen_config_invalid")
    executor_entry = entries.get(EXECUTOR_CONFIG_ROLE)
    if executor_entry is not None:
        try:
            executor_raw = _read_regular(
                root,
                str(executor_entry["path"]),
                MAX_JSON_BYTES,
            )
            executor_config = _decode_json_object(executor_raw)
            _require_schema(executor_config, "executorConfig")
            expected_projection = _build_worker_run_config(
                source_run_manifest,
                executor_config,
            )
            if run_config != expected_projection:
                errors.append("worker-run-config# sealed_projection_mismatch")
            source_executor_entry = _entries_by_role(
                source_run_manifest,
                "source run",
            ).get(EXECUTOR_CONFIG_ROLE)
            if (
                source_executor_entry is None
                or source_executor_entry.get("sha256") != _sha256(executor_raw)
                or source_executor_entry.get("size_bytes") != len(executor_raw)
            ):
                errors.append("worker-run-config# executor_config_binding_mismatch")
        except (OSError, ValueError, ExecutorValidationError):
            errors.append("worker-run-config# sealed_projection_invalid")

    entries_by_path = {str(entry["path"]): entry for entry in entries.values()}
    for task in tasks:
        for field_name, expected_access in TASK_ARTIFACT_ACCESS.items():
            reference = task.get(field_name)
            if not isinstance(reference, dict):
                errors.append("task-inputs# artifact_reference_invalid")
                continue
            entry = entries_by_path.get(str(reference.get("path")))
            if entry is None or not _reference_matches(reference, entry):
                errors.append("task-inputs# artifact_reference_mismatch")
            elif entry.get("source_access_class") != expected_access:
                errors.append("task-inputs# artifact_access_mismatch")
            elif (
                entry.get("source_bundle") != "dataset"
                or entry.get("source_role") != entry.get("role")
            ):
                errors.append("task-inputs# artifact_provenance_mismatch")


def _paired_input_sha256(task: dict[str, Any], run_config: dict[str, Any]) -> str:
    paired_input = {
        "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
        "task_input": task,
        "agent_turn_contract": run_config["agent_turn_contract"],
        "citefold": run_config["citefold"],
        "reader_model": run_config["reader_model"],
        "system_artifacts": run_config["system_artifacts"],
        "memory": run_config["memory"],
        "generation": run_config["generation"],
        "provider_policy": run_config["provider_policy"],
    }
    return _sha256(_canonical_json(paired_input))


def _execution_id(run_config: dict[str, Any], task_id: str, arm: str) -> str:
    return "exec-" + _sha256(
        _canonical_json(
            {
                "executor": EXECUTOR_CONTRACT_VERSION,
                "run_id": run_config["run_id"],
                "iteration_id": run_config["iteration_id"],
                "task_id": task_id,
                "arm": arm,
            }
        )
    )[:32]


def _attempt_id(execution_id: str, attempt_index: int) -> str:
    return "attempt-" + _sha256(
        f"{execution_id}\0{attempt_index}".encode("utf-8")
    )[:32]


def _idempotency_key(
    run_config: dict[str, Any],
    execution_id: str,
    attempt_index: int,
) -> str:
    return _sha256(
        f"{run_config['run_id']}\0{run_config['iteration_id']}\0{execution_id}\0{attempt_index}".encode(
            "utf-8"
        )
    )


def _valid_blinded_output_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("out-"):
        return False
    try:
        parsed = uuid.UUID(hex=value[4:])
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and parsed.hex == value[4:]


def _arm_request_errors(
    raw: bytes,
    request: dict[str, Any],
    *,
    execution_id: str,
    task_id: str,
    arm: str,
    execution_order: int,
    paired_input_sha256: str,
) -> list[str]:
    expected = {
        "schema_version": "officelife-track-b-arm-request-v1",
        "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
        "execution_id": execution_id,
        "task_id": task_id,
        "arm": arm,
        "execution_order": execution_order,
        "paired_input_sha256": paired_input_sha256,
        "memory_pack_present": arm == "memory_pack",
    }
    expected_keys = {*expected, "blinded_output_id"}
    if (
        set(request) != expected_keys
        or any(request.get(key) != value for key, value in expected.items())
        or not _valid_blinded_output_id(request.get("blinded_output_id"))
        or raw != _canonical_json(request, pretty=True)
    ):
        return ["arm-request# frozen_request_mismatch"]
    return []


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed


def _attempt_receipt_errors(
    raw: bytes,
    receipt: dict[str, Any],
    *,
    execution_id: str,
    attempt_index: int,
    arm: str,
    run_config: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    expected_keys = {
        "schema_version",
        "attempt_id",
        "attempt_index",
        "started_at",
        "finished_at",
        "status",
        "error_category",
        "retryable",
        "handler_result",
    }
    started_at = _parse_utc_timestamp(receipt.get("started_at"))
    finished_at = _parse_utc_timestamp(receipt.get("finished_at"))
    if (
        set(receipt) != expected_keys
        or receipt.get("schema_version")
        != "officelife-track-b-attempt-receipt-v1"
        or receipt.get("attempt_id") != _attempt_id(execution_id, attempt_index)
        or receipt.get("attempt_index") != attempt_index
        or started_at is None
        or finished_at is None
        or finished_at < started_at
        or raw != _canonical_json(receipt, pretty=True)
    ):
        errors.append("attempt-receipts# identity_or_shape_mismatch")
    status = receipt.get("status")
    if status == "handler_result":
        handler_result = receipt.get("handler_result")
        handler_errors = (
            _schema_errors(handler_result, "handlerResult", "attempt-receipt")
            if isinstance(handler_result, dict)
            else ["attempt-receipt# schema_invalid"]
        )
        errors.extend(handler_errors)
        if (
            receipt.get("error_category") is not None
            or receipt.get("retryable") is not False
            or handler_errors
        ):
            errors.append("attempt-receipts# handler_result_semantics_invalid")
        elif _handler_result_error(handler_result, arm, run_config) is not None:
            errors.append("attempt-receipts# handler_result_semantics_invalid")
    elif status == "infrastructure_error":
        category = receipt.get("error_category")
        retryable = receipt.get("retryable")
        if (
            receipt.get("handler_result") is not None
            or not _valid_id(category)
            or not isinstance(retryable, bool)
            or (retryable and category not in RETRYABLE_INFRASTRUCTURE_CATEGORIES)
        ):
            errors.append("attempt-receipts# infrastructure_semantics_invalid")
    else:
        errors.append("attempt-receipts# status_invalid")
    return errors


def _attempt_sequence_errors(
    receipts: list[dict[str, Any]],
    retry_count: int,
    *,
    require_terminal: bool = False,
) -> list[str]:
    if any(
        receipt.get("status") != "infrastructure_error"
        or receipt.get("retryable") is not True
        for receipt in receipts[:-1]
    ):
        return ["attempt-receipts# retry_sequence_invalid"]
    if require_terminal and receipts:
        final_receipt = receipts[-1]
        if (
            final_receipt.get("status") == "infrastructure_error"
            and final_receipt.get("retryable") is True
            and len(receipts) != retry_count + 1
        ):
            return ["attempt-receipts# retry_sequence_invalid"]
    return []


def _receipt_is_terminal(
    receipt: dict[str, Any],
    attempt_count: int,
    retry_count: int,
) -> bool:
    return (
        receipt.get("status") == "handler_result"
        or receipt.get("retryable") is False
        or attempt_count == retry_count + 1
    )


def _existing_attempt_names(arm_root: Path, retry_count: int) -> set[str]:
    attempts_root = arm_root / "attempts"
    if not attempts_root.exists():
        return set()
    try:
        info = attempts_root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ExecutorValidationError("stored attempt directory is unsafe")
        names = {path.name for path in attempts_root.iterdir()}
    except OSError as exc:
        raise ExecutorValidationError("stored attempt directory is unreadable") from exc
    allowed = {f"{index:03d}.json" for index in range(retry_count + 1)}
    if not names.issubset(allowed):
        raise ExecutorValidationError("stored attempt set conflicts with frozen retries")
    ordered = sorted(names)
    if ordered != [f"{index:03d}.json" for index in range(len(ordered))]:
        raise ExecutorValidationError("stored attempt set is not a contiguous prefix")
    return names


def _execute_arm(
    worker_root: Path,
    execution_root: Path,
    task: dict[str, Any],
    arm: str,
    execution_order: int,
    run_config: dict[str, Any],
    handler: ControlledAgentHandler,
    *,
    aggregate_record_limit: int,
    sleeper: Callable[[float], None],
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    task_id = str(task["task_id"])
    execution_id = _execution_id(run_config, task_id, arm)
    arm_root = execution_root / "arms" / execution_id
    result_relative = f"arms/{execution_id}/result.json"
    result_path = arm_root / "result.json"
    paired_input_sha256 = _paired_input_sha256(task, run_config)
    if os.path.lexists(result_path):
        output = _decode_json_object(
            _read_regular(execution_root, result_relative, MAX_JSON_BYTES)
        )
        errors: list[str] = []
        _validate_one_arm(
            execution_root,
            task,
            arm,
            execution_order,
            run_config,
            output,
            errors,
            aggregate_record_limit=aggregate_record_limit,
        )
        if errors:
            raise ExecutorValidationError("stored arm result is invalid or conflicts with frozen execution")
        return output

    request_relative = f"arms/{execution_id}/request.json"
    request_path = arm_root / "request.json"
    if os.path.lexists(request_path):
        request_raw = _read_regular(execution_root, request_relative, MAX_JSON_BYTES)
        request_record = _decode_json_object(request_raw)
        if _arm_request_errors(
            request_raw,
            request_record,
            execution_id=execution_id,
            task_id=task_id,
            arm=arm,
            execution_order=execution_order,
            paired_input_sha256=paired_input_sha256,
        ):
            raise ExecutorValidationError("stored arm request conflicts with frozen execution")
    else:
        request_record = {
            "schema_version": "officelife-track-b-arm-request-v1",
            "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
            "execution_id": execution_id,
            "task_id": task_id,
            "arm": arm,
            "execution_order": execution_order,
            "blinded_output_id": "out-" + uuid.uuid4().hex,
            "paired_input_sha256": paired_input_sha256,
            "memory_pack_present": arm == "memory_pack",
        }
        _write_new_file(request_path, _canonical_json(request_record, pretty=True))
    request_sha256 = _sha256(_canonical_json(request_record))

    generation = run_config["generation"]
    retry_count = generation["retry_count"]
    backoffs = generation["backoff_seconds"]
    existing_attempt_names = _existing_attempt_names(arm_root, retry_count)
    final_receipt: dict[str, Any] | None = None
    attempt_count = 0
    for attempt_index in range(retry_count + 1):
        attempt_count = attempt_index + 1
        attempt_id = _attempt_id(execution_id, attempt_index)
        attempt_path = arm_root / "attempts" / f"{attempt_index:03d}.json"
        if attempt_path.name in existing_attempt_names:
            attempt_relative = (
                f"arms/{execution_id}/attempts/{attempt_index:03d}.json"
            )
            receipt_raw = _read_regular(
                execution_root,
                attempt_relative,
                MAX_JSON_BYTES,
            )
            receipt = _decode_json_object(receipt_raw)
            if _attempt_receipt_errors(
                receipt_raw,
                receipt,
                execution_id=execution_id,
                attempt_index=attempt_index,
                arm=arm,
                run_config=run_config,
            ):
                raise ExecutorValidationError("stored attempt conflicts with frozen execution")
        else:
            workspace, run_artifact_paths = _materialize_workspace(
                worker_root,
                execution_root,
                task,
                execution_id,
                attempt_index,
            )
            idempotency_key = _idempotency_key(
                run_config,
                execution_id,
                attempt_index,
            )
            arm_request = ArmRequest(
                execution_id=execution_id,
                attempt_id=attempt_id,
                attempt_index=attempt_index,
                task_id=task_id,
                memory_mode=arm,
                execution_order=execution_order,
                idempotency_key=idempotency_key,
                paired_input_sha256=paired_input_sha256,
                request_sha256=request_sha256,
                memory_pack_present=arm == "memory_pack",
                workspace_root=workspace,
                input_path=workspace / "input",
                recent_context_path=workspace / "recent-context",
                tool_fixture_path=workspace / "tool-fixture",
                snapshot_path=workspace / "snapshot",
                run_artifact_paths=run_artifact_paths,
                run_config=deepcopy(run_config),
                task_input=deepcopy(task),
            )
            started_at = _format_time(clock())
            try:
                handler_result = handler.execute(arm_request)
            except HandlerInfrastructureError as exc:
                finished_at = _format_time(clock())
                retryable = exc.category in RETRYABLE_INFRASTRUCTURE_CATEGORIES
                receipt = _infrastructure_receipt(
                    attempt_id,
                    attempt_index,
                    started_at,
                    finished_at,
                    exc.category if retryable else "unclassified-infrastructure-error",
                    retryable=retryable,
                )
            except Exception:
                finished_at = _format_time(clock())
                receipt = _infrastructure_receipt(
                    attempt_id,
                    attempt_index,
                    started_at,
                    finished_at,
                    "unclassified-handler-exception",
                    retryable=False,
                )
            else:
                finished_at = _format_time(clock())
                try:
                    if not isinstance(handler_result, HandlerResult):
                        raise ExecutorValidationError(
                            "handler returned an unsupported result type"
                        )
                    result_value = handler_result.as_dict()
                    _require_schema(result_value, "handlerResult")
                    category = _handler_result_error(result_value, arm, run_config)
                    if category is None:
                        candidate = {
                            "schema_version": "officelife-track-b-attempt-receipt-v1",
                            "attempt_id": attempt_id,
                            "attempt_index": attempt_index,
                            "started_at": started_at,
                            "finished_at": finished_at,
                            "status": "handler_result",
                            "error_category": None,
                            "retryable": False,
                            "handler_result": result_value,
                        }
                        if not _handler_result_fits_artifact_envelope(
                            candidate,
                            request_record,
                            request_sha256,
                            attempt_count,
                            aggregate_record_limit,
                        ):
                            category = "handler_output_invalid"
                    if category is None:
                        receipt = candidate
                    else:
                        retryable = category in RETRYABLE_INFRASTRUCTURE_CATEGORIES
                        receipt = _infrastructure_receipt(
                            attempt_id,
                            attempt_index,
                            started_at,
                            finished_at,
                            category,
                            retryable=retryable,
                        )
                except Exception:
                    receipt = _infrastructure_receipt(
                        attempt_id,
                        attempt_index,
                        started_at,
                        finished_at,
                        "handler_output_invalid",
                        retryable=True,
                    )
            _write_new_file(attempt_path, _canonical_json(receipt, pretty=True))
        final_receipt = receipt
        if receipt.get("status") == "handler_result":
            break
        if receipt.get("retryable") is not True:
            break
        if attempt_index < retry_count:
            sleeper(float(backoffs[attempt_index]))

    if final_receipt is None:
        raise AssertionError("arm execution did not produce an attempt receipt")
    result = _finalize_arm_result(
        execution_root,
        request_record,
        request_sha256,
        final_receipt,
        attempt_count,
        aggregate_record_limit=aggregate_record_limit,
    )
    _write_new_file(result_path, _canonical_json(result, pretty=True))
    _remove_directory_tree(execution_root, f".work/{execution_id}")
    return result


def _materialize_workspace(
    worker_root: Path,
    execution_root: Path,
    task: dict[str, Any],
    execution_id: str,
    attempt_index: int,
) -> tuple[Path, dict[str, Path]]:
    workspace = execution_root / ".work" / execution_id / f"attempt-{attempt_index:03d}"
    _mkdir_private(workspace.parent)
    _remove_directory_tree(
        execution_root,
        f".work/{execution_id}/attempt-{attempt_index:03d}",
    )
    _mkdir_private(workspace)
    fields = {
        "input_artifact": "input",
        "recent_context_artifact": "recent-context",
        "tool_fixture_artifact": "tool-fixture",
        "snapshot_artifact": "snapshot",
    }
    for field_name, target_name in fields.items():
        reference = task[field_name]
        raw = _read_regular(worker_root, str(reference["path"]), MAX_ARTIFACT_BYTES)
        if _sha256(raw) != reference["sha256"] or len(raw) != reference["size_bytes"]:
            raise ExecutorValidationError("worker artifact changed before arm execution")
        _write_new_file(workspace / target_name, raw)
    worker_manifest = _decode_json_object(
        _read_regular(worker_root, "worker-manifest.json", MAX_JSON_BYTES)
    )
    by_role = _entries_by_role(worker_manifest, "worker")
    run_artifact_paths: dict[str, Path] = {}
    for role in sorted(WORKSPACE_RUN_ROLES):
        entry = by_role.get(role)
        if entry is None:
            raise ExecutorValidationError("worker run artifact disappeared before execution")
        raw = _read_regular(worker_root, str(entry["path"]), MAX_ARTIFACT_BYTES)
        if _sha256(raw) != entry["sha256"] or len(raw) != entry["size_bytes"]:
            raise ExecutorValidationError("worker run artifact changed before execution")
        target = workspace / "run" / role
        _write_new_file(target, raw)
        run_artifact_paths[role] = target
    return workspace, run_artifact_paths


def _handler_result_error(
    result: dict[str, Any],
    arm: str,
    run_config: dict[str, Any],
) -> str | None:
    if result["fallback_used"] is not False:
        return "fallback_forbidden"
    reader = run_config["reader_model"]
    expected = (
        reader["model_id"],
        reader["immutable_model_version"],
        reader["actual_upstream_provider"],
        reader["immutable_route"],
    )
    actual = (
        result["actual_model_id"],
        result["actual_model_version"],
        result["actual_upstream_provider"],
        result["actual_route"],
    )
    if actual != expected:
        return "provider_identity_mismatch"
    if arm == "no_memory" and result["memory_pack"] is not None:
        return "no_memory_treatment_contaminated"
    if arm == "memory_pack" and result["memory_pack"] is None:
        return "memory_pack_treatment_missing"
    outcome = result["outcome"]
    content = result["content"]
    if outcome in {"answer", "refusal"} and (not isinstance(content, str) or not content.strip()):
        return "handler_output_invalid"
    if outcome in {"product_timeout", "product_failure"} and content is not None:
        return "handler_output_invalid"
    if outcome == "product_timeout":
        if "product-timeout" not in run_config["generation"]["product_failure_categories"]:
            return "unclassified-product-timeout"
    if outcome == "product_failure":
        category = result.get("error_category")
        if category not in run_config["generation"]["product_failure_categories"]:
            return "unclassified-product-failure"
    elif result.get("error_category") is not None:
        return "unexpected-error-category"
    return None


def _infrastructure_receipt(
    attempt_id: str,
    attempt_index: int,
    started_at: str,
    finished_at: str,
    category: str,
    *,
    retryable: bool,
) -> dict[str, Any]:
    safe_category = category if _valid_id(category) else "invalid-error-category"
    return {
        "schema_version": "officelife-track-b-attempt-receipt-v1",
        "attempt_id": attempt_id,
        "attempt_index": attempt_index,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": "infrastructure_error",
        "error_category": safe_category,
        "retryable": retryable,
        "handler_result": None,
    }


def _arm_result_payload(
    request_record: dict[str, Any],
    request_sha256: str,
    receipt: dict[str, Any],
    attempt_count: int,
) -> tuple[dict[str, Any], str, bytes]:
    execution_id = str(request_record["execution_id"])
    handler_result = receipt.get("handler_result")
    if receipt.get("status") == "handler_result" and isinstance(handler_result, dict):
        outcome = handler_result["outcome"]
        content = handler_result["content"]
        error_category = handler_result["error_category"]
        actual_model_id = handler_result["actual_model_id"]
        actual_model_version = handler_result["actual_model_version"]
        actual_provider = handler_result["actual_upstream_provider"]
        actual_route = handler_result["actual_route"]
        fallback_used = handler_result["fallback_used"]
        usage = handler_result["usage"]
        private_trace = {
            "schema_version": "officelife-track-b-private-arm-trace-v1",
            "execution_id": execution_id,
            "task_id": request_record["task_id"],
            "arm": request_record["arm"],
            "memory_pack": handler_result["memory_pack"],
            "handler_trace": handler_result["trace"],
        }
    else:
        outcome = "infrastructure_error"
        content = None
        error_category = receipt.get("error_category") or "unknown-infrastructure-error"
        actual_model_id = None
        actual_model_version = None
        actual_provider = None
        actual_route = None
        fallback_used = None
        usage = {}
        private_trace = {
            "schema_version": "officelife-track-b-private-arm-trace-v1",
            "execution_id": execution_id,
            "task_id": request_record["task_id"],
            "arm": request_record["arm"],
            "memory_pack": None,
            "handler_trace": {"infrastructure_error": error_category},
        }
    trace_relative = f"traces/{execution_id}.json"
    trace_raw = _canonical_json(private_trace, pretty=True)
    result = {
        "schema_version": ARM_OUTPUT_SCHEMA_VERSION,
        "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
        "execution_id": execution_id,
        "task_id": request_record["task_id"],
        "arm": request_record["arm"],
        "execution_order": request_record["execution_order"],
        "blinded_output_id": request_record["blinded_output_id"],
        "paired_input_sha256": request_record["paired_input_sha256"],
        "request_sha256": request_sha256,
        "outcome": outcome,
        "content": content,
        "trace": {
            "path": trace_relative,
            "sha256": _sha256(trace_raw),
            "size_bytes": len(trace_raw),
        },
        "attempt_count": attempt_count,
        "error_category": error_category,
        "actual_model_id": actual_model_id,
        "actual_model_version": actual_model_version,
        "actual_upstream_provider": actual_provider,
        "actual_route": actual_route,
        "fallback_used": fallback_used,
        "usage": usage,
    }
    _require_schema(result, "armOutput")
    return result, trace_relative, trace_raw


def _blinded_output_payload(output: dict[str, Any]) -> dict[str, Any]:
    blinded = {
        "schema_version": BLINDED_OUTPUT_SCHEMA_VERSION,
        "blinded_output_id": output["blinded_output_id"],
        "outcome": output["outcome"],
        "content": output["content"],
    }
    _require_schema(blinded, "blindedOutput")
    return blinded


def _handler_result_fits_artifact_envelope(
    receipt: dict[str, Any],
    request_record: dict[str, Any],
    request_sha256: str,
    attempt_count: int,
    aggregate_record_limit: int,
) -> bool:
    try:
        if len(_canonical_json(receipt, pretty=True)) > MAX_JSON_BYTES:
            return False
        result, _trace_relative, trace_raw = _arm_result_payload(
            request_record,
            request_sha256,
            receipt,
            attempt_count,
        )
        if (
            len(trace_raw) > MAX_JSON_BYTES
            or len(_canonical_json(result, pretty=True)) > MAX_JSON_BYTES
            or len(_canonical_json(result)) > aggregate_record_limit
            or len(_canonical_json(_blinded_output_payload(result)))
            > aggregate_record_limit
        ):
            return False
    except Exception:
        return False
    return True


def _finalize_arm_result(
    execution_root: Path,
    request_record: dict[str, Any],
    request_sha256: str,
    receipt: dict[str, Any],
    attempt_count: int,
    *,
    aggregate_record_limit: int,
) -> dict[str, Any]:
    if not _handler_result_fits_artifact_envelope(
        receipt,
        request_record,
        request_sha256,
        attempt_count,
        aggregate_record_limit,
    ):
        raise ExecutorValidationError("stored attempt exceeds the artifact envelope")
    result, trace_relative, trace_raw = _arm_result_payload(
        request_record,
        request_sha256,
        receipt,
        attempt_count,
    )
    trace_path = execution_root / trace_relative
    if os.path.lexists(trace_path):
        if _read_regular(execution_root, trace_relative, MAX_JSON_BYTES) != trace_raw:
            raise ExecutorValidationError("existing trace conflicts with stored attempt")
    else:
        _write_new_file(trace_path, trace_raw)
    return result


def _write_aggregates(root: Path, outputs: list[dict[str, Any]]) -> None:
    record_limit = _aggregate_record_limit(len(outputs))
    arm_records = [_canonical_json(item) for item in outputs]
    blinded = [_blinded_output_payload(item) for item in outputs]
    blinded_records = [_canonical_json(item) for item in blinded]
    if any(
        len(record) > record_limit
        for records in (arm_records, blinded_records)
        for record in records
    ):
        raise ExecutorValidationError("arm output exceeds its aggregate record budget")
    unblinding = [
        {
            "schema_version": UNBLINDING_SCHEMA_VERSION,
            "blinded_output_id": item["blinded_output_id"],
            "task_id": item["task_id"],
            "arm": item["arm"],
            "execution_order": item["execution_order"],
            "execution_id": item["execution_id"],
        }
        for item in outputs
    ]
    for item in unblinding:
        _require_schema(item, "unblindingRecord")
    previous: str | None = None
    events: list[dict[str, Any]] = []
    for sequence, item in enumerate(outputs):
        result_raw = _canonical_json(item, pretty=True)
        event = {
            "schema_version": AUDIT_EVENT_SCHEMA_VERSION,
            "sequence": sequence,
            "event_type": "arm_finalized",
            "execution_id": item["execution_id"],
            "task_id": item["task_id"],
            "arm": item["arm"],
            "artifact_sha256": _sha256(result_raw),
            "previous_event_sha256": previous,
        }
        event_sha256 = _sha256(_canonical_json(event))
        event["event_sha256"] = event_sha256
        _require_schema(event, "auditEvent")
        events.append(event)
        previous = event_sha256
    aggregates = {
        "arm-outputs.jsonl": b"".join(arm_records),
        "blinded-outputs.jsonl": b"".join(blinded_records),
        "unblinding-map.jsonl": b"".join(
            _canonical_json(item) for item in unblinding
        ),
        "execution-audit.jsonl": b"".join(
            _canonical_json(item) for item in events
        ),
    }
    if any(len(raw) > MAX_AGGREGATE_BYTES for raw in aggregates.values()):
        raise ExecutorValidationError("execution aggregate exceeds its byte limit")
    for name, raw in aggregates.items():
        _replace_file(root / name, raw)


def _validate_one_arm(
    root: Path,
    task: dict[str, Any],
    arm: str,
    execution_order: int,
    run_config: dict[str, Any],
    output: dict[str, Any],
    errors: list[str],
    *,
    aggregate_record_limit: int,
) -> None:
    errors.extend(_schema_errors(output, "armOutput", "arm-output"))
    try:
        if (
            len(_canonical_json(output)) > aggregate_record_limit
            or len(_canonical_json(_blinded_output_payload(output)))
            > aggregate_record_limit
        ):
            errors.append("arm-output# aggregate_record_too_large")
    except (KeyError, TypeError, ValueError, ExecutorValidationError):
        errors.append("arm-output# aggregate_record_invalid")
    task_id = str(task["task_id"])
    execution_id = _execution_id(run_config, task_id, arm)
    paired_input_sha256 = _paired_input_sha256(task, run_config)
    expected_identity = {
        "execution_id": execution_id,
        "task_id": task_id,
        "arm": arm,
        "execution_order": execution_order,
        "paired_input_sha256": paired_input_sha256,
    }
    if any(output.get(key) != value for key, value in expected_identity.items()):
        errors.append("arm-output# frozen_identity_mismatch")

    result_relative = f"arms/{execution_id}/result.json"
    request_relative = f"arms/{execution_id}/request.json"
    try:
        result_raw = _read_regular(root, result_relative, MAX_JSON_BYTES)
        stored_result = _decode_json_object(result_raw)
        if result_raw != _canonical_json(stored_result, pretty=True):
            errors.append("arm-output# stored_result_not_canonical")
        if stored_result != output:
            errors.append("arm-output# stored_result_mismatch")
    except (OSError, ValueError, ExecutorValidationError):
        errors.append("arm-output# stored_result_missing")

    try:
        request_raw = _read_regular(root, request_relative, MAX_JSON_BYTES)
        request = _decode_json_object(request_raw)
    except (OSError, ValueError, ExecutorValidationError):
        errors.append("arm-request# missing_or_invalid")
        return
    errors.extend(
        _arm_request_errors(
            request_raw,
            request,
            execution_id=execution_id,
            task_id=task_id,
            arm=arm,
            execution_order=execution_order,
            paired_input_sha256=paired_input_sha256,
        )
    )
    if request.get("blinded_output_id") != output.get("blinded_output_id"):
        errors.append("arm-request# output_binding_mismatch")
    request_sha256 = _sha256(_canonical_json(request))
    if output.get("request_sha256") != request_sha256:
        errors.append("arm-output# request_sha256_mismatch")

    attempt_count = output.get("attempt_count")
    retry_count = run_config["generation"]["retry_count"]
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 1
        or attempt_count > retry_count + 1
    ):
        errors.append("attempt-receipts# count_mismatch")
        return
    attempts_relative = f"arms/{execution_id}/attempts"
    attempts_root = root / attempts_relative
    try:
        info = attempts_root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ExecutorValidationError("attempt receipt root is unsafe")
        actual_names = {path.name for path in attempts_root.iterdir()}
    except (OSError, ExecutorValidationError):
        errors.append("attempt-receipts# missing_or_invalid")
        return
    expected_names = {f"{index:03d}.json" for index in range(attempt_count)}
    if actual_names != expected_names:
        errors.append("attempt-receipts# count_mismatch")

    receipts: list[dict[str, Any]] = []
    for attempt_index in range(attempt_count):
        relative = f"{attempts_relative}/{attempt_index:03d}.json"
        try:
            raw = _read_regular(root, relative, MAX_JSON_BYTES)
            receipt = _decode_json_object(raw)
        except (OSError, ValueError, ExecutorValidationError):
            errors.append("attempt-receipts# missing_or_invalid")
            continue
        receipts.append(receipt)
        errors.extend(
            _attempt_receipt_errors(
                raw,
                receipt,
                execution_id=execution_id,
                attempt_index=attempt_index,
                arm=arm,
                run_config=run_config,
            )
        )

    if len(receipts) != attempt_count:
        return
    errors.extend(
        _attempt_sequence_errors(
            receipts,
            retry_count,
            require_terminal=True,
        )
    )
    final_receipt = receipts[-1]

    handler_result = final_receipt.get("handler_result")
    if final_receipt.get("status") == "handler_result" and isinstance(
        handler_result, dict
    ):
        expected_output = {
            "outcome": handler_result.get("outcome"),
            "content": handler_result.get("content"),
            "error_category": handler_result.get("error_category"),
            "actual_model_id": handler_result.get("actual_model_id"),
            "actual_model_version": handler_result.get("actual_model_version"),
            "actual_upstream_provider": handler_result.get("actual_upstream_provider"),
            "actual_route": handler_result.get("actual_route"),
            "fallback_used": handler_result.get("fallback_used"),
            "usage": handler_result.get("usage"),
        }
        expected_memory_pack = handler_result.get("memory_pack")
        expected_handler_trace = handler_result.get("trace")
    else:
        expected_output = {
            "outcome": "infrastructure_error",
            "content": None,
            "error_category": final_receipt.get("error_category")
            or "unknown-infrastructure-error",
            "actual_model_id": None,
            "actual_model_version": None,
            "actual_upstream_provider": None,
            "actual_route": None,
            "fallback_used": None,
            "usage": {},
        }
        expected_memory_pack = None
        expected_handler_trace = {
            "infrastructure_error": expected_output["error_category"]
        }
    if any(output.get(key) != value for key, value in expected_output.items()):
        errors.append("arm-output# final_receipt_mismatch")

    expected_trace_path = f"traces/{execution_id}.json"
    trace_reference = output.get("trace")
    if not isinstance(trace_reference, dict) or trace_reference.get(
        "path"
    ) != expected_trace_path:
        errors.append("arm-output# trace_reference_invalid")
        return
    try:
        trace_raw = _read_regular(root, expected_trace_path, MAX_JSON_BYTES)
        trace = _decode_json_object(trace_raw)
    except (OSError, ValueError, ExecutorValidationError):
        errors.append("arm-output# trace_missing")
        return
    expected_trace = {
        "schema_version": "officelife-track-b-private-arm-trace-v1",
        "execution_id": execution_id,
        "task_id": task_id,
        "arm": arm,
        "memory_pack": expected_memory_pack,
        "handler_trace": expected_handler_trace,
    }
    if (
        trace != expected_trace
        or trace_raw != _canonical_json(trace, pretty=True)
        or trace_reference.get("sha256") != _sha256(trace_raw)
        or trace_reference.get("size_bytes") != len(trace_raw)
    ):
        errors.append("arm-output# trace_semantics_mismatch")


def _validate_execution_semantics(
    root: Path,
    manifest: dict[str, Any],
    worker_report: dict[str, Any],
    worker: dict[str, Any] | None,
    entries: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    if manifest.get("worker_manifest_sha256") != worker_report.get("worker_manifest_sha256"):
        errors.append("execution-manifest# worker_manifest_sha256_mismatch")
    if worker is None:
        errors.append("execution-manifest# worker_bundle_unavailable")
        return
    tasks = worker["tasks"]
    run_config = worker["run_config"]
    try:
        aggregate_record_limit = _aggregate_record_limit(
            len(tasks) * len(ARM_NAMES)
        )
    except ExecutorValidationError:
        errors.append("execution-manifest# aggregate_envelope_invalid")
        return
    if (
        manifest.get("run_id") != run_config.get("run_id")
        or manifest.get("iteration_id") != run_config.get("iteration_id")
    ):
        errors.append("execution-manifest# worker_run_identity_mismatch")
    if (
        manifest.get("expected_task_count") != len(tasks)
        or manifest.get("expected_arm_count") != len(tasks) * len(ARM_NAMES)
    ):
        errors.append("execution-manifest# worker_task_count_mismatch")
    try:
        binding_raw = _read_regular(
            root,
            "execution-binding.json",
            MAX_JSON_BYTES,
        )
        binding = _decode_json_object(binding_raw)
        errors.extend(
            _schema_errors(
                binding,
                "executionBinding",
                "execution-binding",
            )
        )
        if (
            binding_raw != _canonical_json(binding, pretty=True)
            or binding != _build_execution_binding(worker)
        ):
            errors.append("execution-binding# worker_binding_mismatch")
    except (OSError, ValueError, ExecutorValidationError):
        errors.append("execution-binding# missing_or_invalid")
    required = {
        "execution-binding.json",
        "arm-outputs.jsonl",
        "blinded-outputs.jsonl",
        "unblinding-map.jsonl",
        "execution-audit.jsonl",
    }
    if not required.issubset({str(entry.get("path")) for entry in entries.values()}):
        errors.append("execution-manifest# aggregate_artifact_missing")
        return
    try:
        outputs = _decode_canonical_jsonl(
            _read_regular(root, "arm-outputs.jsonl", MAX_AGGREGATE_BYTES),
            line_limit=aggregate_record_limit,
        )
        blinded = _decode_canonical_jsonl(
            _read_regular(root, "blinded-outputs.jsonl", MAX_AGGREGATE_BYTES),
            line_limit=aggregate_record_limit,
        )
        unblinding = _decode_canonical_jsonl(
            _read_regular(root, "unblinding-map.jsonl", MAX_AGGREGATE_BYTES),
            line_limit=aggregate_record_limit,
        )
        audit = _decode_canonical_jsonl(
            _read_regular(root, "execution-audit.jsonl", MAX_AGGREGATE_BYTES),
            line_limit=aggregate_record_limit,
        )
    except (OSError, ValueError, ExecutorValidationError):
        errors.append("execution-manifest# aggregate_artifact_invalid")
        return
    for item in outputs:
        errors.extend(_schema_errors(item, "armOutput", "arm-outputs"))
    for item in blinded:
        errors.extend(_schema_errors(item, "blindedOutput", "blinded-outputs"))
    for item in unblinding:
        errors.extend(_schema_errors(item, "unblindingRecord", "unblinding-map"))
    for item in audit:
        errors.extend(_schema_errors(item, "auditEvent", "execution-audit"))

    expected_arms: list[tuple[dict[str, Any], str, int]] = []
    for task in tasks:
        task_id = str(task["task_id"])
        expected_arms.extend(
            (task, arm, execution_order)
            for execution_order, arm in enumerate(_arm_order(run_config, task_id))
        )
    if (
        len(outputs) != len(expected_arms)
        or len(blinded) != len(outputs)
        or len(unblinding) != len(outputs)
    ):
        errors.append("execution-manifest# arm_count_mismatch")
    for index, (task, arm, execution_order) in enumerate(expected_arms):
        if index >= len(outputs):
            break
        output = outputs[index]
        if output.get("task_id") != task.get("task_id"):
            errors.append("arm-outputs# worker_task_mismatch")
        _validate_one_arm(
            root,
            task,
            arm,
            execution_order,
            run_config,
            output,
            errors,
            aggregate_record_limit=aggregate_record_limit,
        )
    pairs = [(item.get("task_id"), item.get("arm")) for item in outputs]
    if len(set(pairs)) != len(pairs):
        errors.append("arm-outputs# duplicate_task_arm")
    by_task: dict[Any, set[Any]] = {}
    for task_id, arm in pairs:
        by_task.setdefault(task_id, set()).add(arm)
    if any(arms != set(ARM_NAMES) for arms in by_task.values()):
        errors.append("arm-outputs# incomplete_pair")

    output_by_blind = {item.get("blinded_output_id"): item for item in outputs}
    if len(output_by_blind) != len(outputs):
        errors.append("arm-outputs# duplicate_blinded_output_id")
    expected_blinded = [
        {
            "schema_version": BLINDED_OUTPUT_SCHEMA_VERSION,
            "blinded_output_id": output.get("blinded_output_id"),
            "outcome": output.get("outcome"),
            "content": output.get("content"),
        }
        for output in outputs
    ]
    if blinded != expected_blinded:
        errors.append("blinded-outputs# mapping_mismatch")
    expected_unblinding = [
        {
            "schema_version": UNBLINDING_SCHEMA_VERSION,
            "blinded_output_id": output.get("blinded_output_id"),
            "task_id": output.get("task_id"),
            "arm": output.get("arm"),
            "execution_order": output.get("execution_order"),
            "execution_id": output.get("execution_id"),
        }
        for output in outputs
    ]
    if unblinding != expected_unblinding:
        errors.append("unblinding-map# mapping_mismatch")

    previous: str | None = None
    for sequence, event in enumerate(audit):
        body = dict(event)
        event_sha = body.pop("event_sha256", None)
        if (
            event.get("sequence") != sequence
            or event.get("previous_event_sha256") != previous
            or _sha256(_canonical_json(body)) != event_sha
        ):
            errors.append("execution-audit# chain_invalid")
            break
        if sequence >= len(outputs):
            errors.append("execution-audit# result_count_mismatch")
            break
        output = outputs[sequence]
        if any(
            event.get(field) != output.get(field)
            for field in ("execution_id", "task_id", "arm")
        ):
            errors.append("execution-audit# result_identity_mismatch")
        if event.get("artifact_sha256") != _sha256(
            _canonical_json(output, pretty=True)
        ):
            errors.append("execution-audit# artifact_binding_mismatch")
        previous = str(event_sha)
    if len(audit) != len(outputs):
        errors.append("execution-audit# result_count_mismatch")
    if not audit or manifest.get("audit_chain_head") != previous:
        errors.append("execution-manifest# audit_chain_head_mismatch")

    incomplete = any(item.get("outcome") == "infrastructure_error" for item in outputs)
    expected_status = "incomplete" if incomplete else "complete"
    if manifest.get("execution_status") != expected_status:
        errors.append("execution-manifest# execution_status_mismatch")
    expected_completed = sum(item.get("outcome") != "infrastructure_error" for item in outputs)
    if manifest.get("completed_arm_count") != expected_completed:
        errors.append("execution-manifest# completed_arm_count_mismatch")



def _build_worker_run_config(
    run_manifest: dict[str, Any],
    executor_config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": WORKER_RUN_CONFIG_SCHEMA_VERSION,
        "executor_contract_version": EXECUTOR_CONTRACT_VERSION,
        "run_id": run_manifest["run_id"],
        "iteration_id": run_manifest["iteration_id"],
        "agent_turn_contract": run_manifest["agent"]["agent_turn_contract"],
        "citefold": deepcopy(run_manifest["citefold"]),
        "reader_model": deepcopy(run_manifest["models"]["reader"]),
        "system_artifacts": {
            name: deepcopy(reference)
            for name, reference in run_manifest["system_artifacts"].items()
            if name != "evaluator_prompt"
        },
        "memory": deepcopy(run_manifest["memory"]),
        "generation": deepcopy(run_manifest["generation"]),
        "provider_policy": deepcopy(run_manifest["provider_policy"]),
        "randomization": deepcopy(run_manifest["randomization"]),
        "executor_config": deepcopy(executor_config),
    }


def _validate_frozen_executor_inputs(
    run_manifest: dict[str, Any],
    executor_config: dict[str, Any],
) -> None:
    if run_manifest.get("agent", {}).get("agent_turn_contract") != "agent-turn-v1":
        raise ExecutorValidationError("executor requires agent-turn-v1")
    generation = run_manifest.get("generation", {})
    randomization = run_manifest.get("randomization", {})
    if generation.get("fallback_policy") != "none" or generation.get("fallback_routes") != []:
        raise ExecutorValidationError("executor forbids fallback")
    if randomization.get("algorithm") != "hmac-sha256-v1":
        raise ExecutorValidationError("executor requires hmac-sha256-v1 arm order")
    if randomization.get("task_order_policy") != "fixed-dataset-order":
        raise ExecutorValidationError("executor supports only fixed-dataset-order")
    if randomization.get("output_blinding_algorithm") != "uuid-v4-v1":
        raise ExecutorValidationError("executor requires uuid-v4-v1 output blinding")
    if run_manifest.get("models", {}).get("reader", {}).get("enabled") is not True:
        raise ExecutorValidationError("reader model must be enabled")
    if executor_config.get("snapshot_adapter_id") != "opaque-pass-through-v1":
        raise ExecutorValidationError("this draft only supports the non-qualifying opaque snapshot adapter")


def _validate_frozen_worker_config(run_config: dict[str, Any]) -> None:
    pseudo_manifest = {
        "agent": {"agent_turn_contract": run_config.get("agent_turn_contract")},
        "generation": run_config.get("generation"),
        "randomization": run_config.get("randomization"),
        "models": {"reader": run_config.get("reader_model")},
    }
    _validate_frozen_executor_inputs(pseudo_manifest, run_config.get("executor_config", {}))


def _arm_order(run_config: dict[str, Any], task_id: str) -> tuple[str, str]:
    randomization = run_config["randomization"]
    seed = randomization["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ExecutorValidationError("randomization seed must be an integer")
    key = str(seed).encode("ascii")
    message = b"\0".join(
        (
            b"officelife-track-b-arm-order-v1",
            str(run_config["run_id"]).encode("utf-8"),
            str(run_config["iteration_id"]).encode("utf-8"),
            task_id.encode("utf-8"),
        )
    )
    first_bit = hmac.new(key, message, hashlib.sha256).digest()[0] & 1
    return ARM_NAMES if first_bit == 0 else tuple(reversed(ARM_NAMES))


def _nonqualification_reasons(config: dict[str, Any]) -> list[str]:
    reasons = {"controlled_executor_draft"}
    if config.get("handler_protocol") == "callable-test-v1":
        reasons.add("callable_handler_test_only")
    if config.get("snapshot_adapter_id") == "opaque-pass-through-v1":
        reasons.add("opaque_snapshot_adapter")
    if config.get("worker_isolation") != "os-sandbox-required":
        reasons.add("filesystem_isolation_not_enforced")
    return sorted(reasons)


def _validate_inventory(
    root: Path,
    manifest: dict[str, Any],
    manifest_name: str,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        errors.append("inventory# missing")
        return {}
    entries: dict[str, dict[str, Any]] = {}
    paths: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            errors.append("inventory# entry_not_object")
            continue
        role = entry.get("role")
        relative = entry.get("path")
        if not _valid_id(role) or not _safe_relative_path(relative):
            errors.append("inventory# unsafe_role_or_path")
            continue
        if role in entries:
            errors.append("inventory# duplicate_role")
        if relative in paths:
            errors.append("inventory# duplicate_path")
        if str(relative).casefold() in folded:
            errors.append("inventory# casefold_path_collision")
        entries[str(role)] = entry
        paths[str(relative)] = entry
        folded.add(str(relative).casefold())
    try:
        discovered = _scan_regular_files(root, exclude={manifest_name})
    except ExecutorValidationError:
        errors.append("inventory# unsafe_filesystem_entry")
        return entries
    if set(paths) != discovered:
        errors.append("inventory# exhaustive_file_set_mismatch")
    for relative, entry in paths.items():
        try:
            raw = _read_regular(root, relative, MAX_ARTIFACT_BYTES)
        except (OSError, ExecutorValidationError):
            errors.append("inventory# file_unreadable")
            continue
        if entry.get("size_bytes") != len(raw):
            errors.append(f"{relative}# inventory_size_mismatch")
        if entry.get("sha256") != _sha256(raw):
            errors.append(f"{relative}# inventory_sha256_mismatch")
    return entries


def _execution_inventory(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for relative in sorted(_scan_regular_files(root, exclude={"execution-manifest.json"})):
        role = "execution-file-" + _sha256(relative.encode("utf-8"))[:24]
        media = "application/x-ndjson" if relative.endswith(".jsonl") else "application/json"
        entries.append(
            _worker_entry(
                root,
                relative,
                role,
                media,
                source_bundle="derived",
                source_role=None,
                source_access_class="derived",
            )
        )
    return entries


def _worker_entry(
    root: Path,
    relative: str,
    role: str,
    media_type: str,
    *,
    source_bundle: str,
    source_role: str | None,
    source_access_class: str,
) -> dict[str, Any]:
    raw = (root / relative).read_bytes()
    return {
        "path": relative,
        "role": role,
        "sha256": _sha256(raw),
        "size_bytes": len(raw),
        "media_type": media_type,
        "source_bundle": source_bundle,
        "source_role": source_role,
        "source_access_class": source_access_class,
    }


def _entries_by_role(manifest: dict[str, Any], bundle: str) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ExecutorValidationError(f"{bundle} manifest has no file inventory")
    result: dict[str, dict[str, Any]] = {}
    for entry in files:
        if not isinstance(entry, dict) or not _valid_id(entry.get("role")):
            raise ExecutorValidationError(f"{bundle} manifest has an invalid inventory role")
        role = str(entry["role"])
        if role in result:
            raise ExecutorValidationError(f"{bundle} manifest has duplicate inventory roles")
        result[role] = entry
    return result


def _entries_by_path(manifest: dict[str, Any], bundle: str) -> dict[str, dict[str, Any]]:
    by_role = _entries_by_role(manifest, bundle)
    result: dict[str, dict[str, Any]] = {}
    for entry in by_role.values():
        relative = entry.get("path")
        if not _safe_relative_path(relative) or relative in result:
            raise ExecutorValidationError(f"{bundle} manifest has an invalid inventory path")
        result[str(relative)] = entry
    return result


def _read_inventory_file(root: Path, entry: dict[str, Any]) -> bytes:
    relative = entry.get("path")
    if not _safe_relative_path(relative):
        raise ExecutorValidationError("inventory path is unsafe")
    raw = _read_regular(root, str(relative), MAX_ARTIFACT_BYTES)
    if entry.get("sha256") != _sha256(raw) or entry.get("size_bytes") != len(raw):
        raise ExecutorValidationError("inventory file changed")
    return raw


def _reference_matches(reference: Any, entry: dict[str, Any]) -> bool:
    return (
        isinstance(reference, dict)
        and reference.get("path") == entry.get("path")
        and reference.get("sha256") == entry.get("sha256")
        and reference.get("size_bytes") == entry.get("size_bytes")
    )


def _read_regular(root: Path, relative: str, limit: int) -> bytes:
    if not _safe_relative_path(relative):
        raise ExecutorValidationError("unsafe relative path")
    root_resolved = root.resolve(strict=True)
    current = root_resolved
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ExecutorValidationError("artifact parent is not a real directory")
    path = current / parts[-1]
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ExecutorValidationError("artifact is not a unique regular file")
    if before.st_size > limit:
        raise ExecutorValidationError("artifact exceeds its size limit")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if opened.st_ino != before.st_ino or opened.st_dev != before.st_dev:
            raise ExecutorValidationError("artifact changed while opening")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > limit:
        raise ExecutorValidationError("artifact exceeds its size limit")
    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ExecutorValidationError("artifact changed while reading")
    return raw


def _scan_regular_files(root: Path, *, exclude: set[str]) -> set[str]:
    result: set[str] = set()
    inode_keys: set[tuple[int, int]] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(dirnames):
            info = (base / name).lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ExecutorValidationError("bundle contains an unsafe directory")
        for name in filenames:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if relative in exclude:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ExecutorValidationError("bundle contains an unsafe file")
            inode = (info.st_dev, info.st_ino)
            if inode in inode_keys:
                raise ExecutorValidationError("bundle contains a hard-link collision")
            inode_keys.add(inode)
            result.add(relative)
    return result


def _remove_directory_tree(root: Path, relative: str) -> None:
    if not _safe_relative_path(relative):
        raise ExecutorValidationError("unsafe private cleanup path")
    current = root.resolve(strict=True)
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ExecutorValidationError("private cleanup parent is unsafe")
    target = current / parts[-1]
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ExecutorValidationError("private cleanup target is unsafe")
    shutil.rmtree(target)


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if "\\" in value or "\x00" in value or value.startswith("/"):
        return False
    parts = PurePosixPath(value).parts
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def _resolved_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(f"{label} is not a directory")
    return resolved


def _require_distinct_roots(roots: tuple[Path, ...]) -> None:
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise ExecutorValidationError("private input and output roots must not overlap")


def _mkdir_private(path: Path) -> None:
    target = path.expanduser()
    if not target.is_absolute():
        target = target.absolute()
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            os.chmod(current, 0o700)
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ExecutorValidationError(
                f"private output path contains an unsafe directory: {current.name}"
            )


def _write_new_file(path: Path, raw: bytes) -> None:
    _mkdir_private(path.parent)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def _replace_file(path: Path, raw: bytes) -> None:
    _mkdir_private(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_new_file(temporary, raw)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    value = _decode_json(raw)
    if not isinstance(value, dict):
        raise ExecutorValidationError("JSON document root must be an object")
    return value


def _decode_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutorValidationError("JSON must be UTF-8") from exc
    if text.startswith("\ufeff") or "\r" in text:
        raise ExecutorValidationError("JSON encoding is not canonical")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExecutorValidationError("JSON is invalid") from exc
    _validate_json_tree(value)
    return value


def _decode_canonical_jsonl(
    raw: bytes,
    *,
    line_limit: int | None = None,
) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n") or b"\r" in raw or b"\n\n" in raw:
        raise ExecutorValidationError("JSONL encoding is not canonical")
    result: list[dict[str, Any]] = []
    for line in raw[:-1].split(b"\n"):
        if line_limit is not None and len(line) > line_limit:
            raise ExecutorValidationError("JSONL record exceeds its byte limit")
        value = _decode_json_object(line)
        if _canonical_json(value).rstrip(b"\n") != line:
            raise ExecutorValidationError("JSONL record is not canonically encoded")
        result.append(value)
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutorValidationError("JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ExecutorValidationError(f"non-finite number is forbidden: {value}")


def _validate_json_tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ExecutorValidationError("JSON nesting is too deep")
    if isinstance(value, float) and not math.isfinite(value):
        raise ExecutorValidationError("non-finite number is forbidden")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExecutorValidationError("JSON object key is not a string")
            _validate_json_tree(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_json_tree(item, depth + 1)


def _canonical_json(value: Any, *, pretty: bool = False) -> bytes:
    _validate_json_tree(value)
    if pretty:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return (text + "\n").encode("utf-8")


def _schema() -> dict[str, Any]:
    return _decode_json_object(EXECUTOR_SCHEMA_PATH.read_bytes())


def _schema_errors(value: Any, definition: str, location: str) -> list[str]:
    schema = _schema()
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/$defs/{definition}",
        "$defs": schema["$defs"],
    }
    validator = Draft202012Validator(wrapper)
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    return [f"{location}# schema_invalid" for _ in errors[:20]]


def _require_schema(value: Any, definition: str) -> None:
    if _schema_errors(value, definition, definition):
        raise ExecutorValidationError(f"{definition} failed its frozen schema")


def _task_input_validator() -> Draft202012Validator:
    schemas, registry = _schema_runtime()
    return Draft202012Validator(schemas["task-input"], registry=registry)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_time(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ExecutorValidationError("clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutorValidationError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


__all__ = [
    "EXECUTOR_CONFIG_ROLE",
    "EXECUTOR_CONTRACT_VERSION",
    "ArmRequest",
    "ControlledAgentHandler",
    "ExecutorValidationError",
    "HandlerInfrastructureError",
    "HandlerResult",
    "execute_worker_bundle",
    "prepare_worker_bundle",
    "validate_execution_bundle",
    "validate_worker_bundle",
]
