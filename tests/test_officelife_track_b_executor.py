from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmarks.officelife_track_b_executor as executor_module
from benchmarks.officelife_track_b_executor import (
    EXECUTOR_CONFIG_ROLE,
    MAX_JSON_BYTES,
    ArmRequest,
    ExecutorValidationError,
    HandlerInfrastructureError,
    HandlerResult,
    execute_worker_bundle,
    prepare_worker_bundle,
    validate_execution_bundle,
    validate_worker_bundle,
)
from tests.test_officelife_track_b_contract import (
    build_dataset_bundle,
    build_run_bundle,
    canonical_line,
    inventory_entry,
    sha256,
    write_json,
)


def add_executor_config(run_root: Path) -> dict:
    config = {
        "schema_version": "officelife-track-b-executor-config-v1",
        "executor_contract_version": "officelife-track-b-executor-v1",
        "handler_id": "synthetic-handler-v1",
        "handler_protocol": "callable-test-v1",
        "snapshot_adapter_id": "opaque-pass-through-v1",
        "agent_id": "track-b-agent",
        "session_id_policy": "sha256-task-v1",
        "worker_isolation": "logical-test-only",
    }
    path = run_root / "artifacts/executor-config.json"
    write_json(path, config)
    manifest_path = run_root / "sealed-run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        inventory_entry(
            run_root,
            "artifacts/executor-config.json",
            EXECUTOR_CONFIG_ROLE,
            artifact_kind="config",
            schema_version=config["schema_version"],
            access_class="run_config",
            media_type="application/json",
        )
    )
    write_json(manifest_path, manifest)
    return config


def rewrite_executor_config(run_root: Path, mutate) -> None:
    config_path = run_root / "artifacts/executor-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    mutate(config)
    write_json(config_path, config)
    manifest_path = run_root / "sealed-run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["role"] == EXECUTOR_CONFIG_ROLE)
    entry["sha256"] = sha256(config_path)
    entry["size_bytes"] = config_path.stat().st_size
    write_json(manifest_path, manifest)


def refresh_manifest_entry(root: Path, manifest_name: str, relative: str) -> None:
    manifest_path = root / manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == relative)
    entry["sha256"] = sha256(root / relative)
    entry["size_bytes"] = (root / relative).stat().st_size
    write_json(manifest_path, manifest)


class RecordingHandler:
    handler_id = "synthetic-handler-v1"

    def __init__(self, *, mutate_snapshot: bool = False) -> None:
        self.mutate_snapshot = mutate_snapshot
        self.requests: list[ArmRequest] = []

    def execute(self, request: ArmRequest) -> HandlerResult:
        self.requests.append(request)
        visible = sorted(
            str(path.relative_to(request.workspace_root))
            for path in request.workspace_root.rglob("*")
            if path.is_file()
        )
        self.assertions(request, visible)
        if self.mutate_snapshot:
            request.snapshot_path.write_bytes(request.snapshot_path.read_bytes() + b" changed")
        return HandlerResult(
            outcome="answer",
            content=f"answer for {request.memory_mode}",
            actual_model_id="provider-a/model-2026-01-01",
            actual_model_version="model-2026-01-01",
            actual_upstream_provider="provider-a",
            actual_route="provider-a/route-01",
            fallback_used=False,
            memory_pack=(
                {
                    "contract_version": "agent-turn-v1",
                    "coverage": "supported",
                    "context_markdown": "# MemoryPack\n",
                }
                if request.memory_mode == "memory_pack"
                else None
            ),
            trace={"visible_files": visible},
            usage={"provider_tokens": 10, "reader_cost": 0.001},
        )

    def assertions(self, request: ArmRequest, visible: list[str]) -> None:
        if any("label" in item.lower() for item in visible):
            raise AssertionError(f"label file exposed to handler: {visible}")
        if request.memory_mode == "no_memory" and request.memory_pack_present:
            raise AssertionError("no_memory request claims a MemoryPack")
        if request.memory_mode == "memory_pack" and not request.memory_pack_present:
            raise AssertionError("memory_pack request is missing its treatment marker")


class RetryOnceHandler(RecordingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.attempts: dict[tuple[str, str], int] = {}

    def execute(self, request: ArmRequest) -> HandlerResult:
        key = (request.task_id, request.memory_mode)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] == 1:
            self.requests.append(request)
            raise HandlerInfrastructureError("provider-transport")
        return super().execute(request)


class MismatchedProviderHandler(RecordingHandler):
    def execute(self, request: ArmRequest) -> HandlerResult:
        result = super().execute(request)
        return HandlerResult(
            **{
                **result.as_dict(),
                "actual_upstream_provider": "provider-b",
            }
        )


class NeverCallHandler(RecordingHandler):
    def execute(self, request: ArmRequest) -> HandlerResult:
        raise AssertionError("a completed execution must not call the handler again")


class UnexpectedExceptionHandler(RecordingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    def execute(self, request: ArmRequest) -> HandlerResult:
        self.call_count += 1
        raise RuntimeError("private handler failure")


class InterruptSecondHandler(RecordingHandler):
    def __init__(self, handler_id: str = "synthetic-handler-v1") -> None:
        super().__init__()
        self.handler_id = handler_id
        self.call_count = 0

    def execute(self, request: ArmRequest) -> HandlerResult:
        self.call_count += 1
        if self.call_count == 2:
            raise KeyboardInterrupt("simulated crash before second arm")
        return super().execute(request)


class SchemaInvalidResultHandler(RecordingHandler):
    def execute(self, request: ArmRequest) -> HandlerResult:
        result = super().execute(request)
        return HandlerResult(
            **{
                **result.as_dict(),
                "trace": [],
            }
        )


class OversizedTraceHandler(RecordingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.oversized_blob = "x" * (MAX_JSON_BYTES + 1)

    def execute(self, request: ArmRequest) -> HandlerResult:
        result = super().execute(request)
        return HandlerResult(
            **{
                **result.as_dict(),
                "trace": {"blob": self.oversized_blob},
            }
        )


class LargeContentHandler(RecordingHandler):
    def execute(self, request: ArmRequest) -> HandlerResult:
        result = super().execute(request)
        return HandlerResult(
            **{
                **result.as_dict(),
                "content": "x" * 3000,
            }
        )


class OfficeLifeTrackBExecutorTest(unittest.TestCase):
    def build_inputs(self, root: Path) -> tuple[Path, Path, Path]:
        dataset_root = root / "dataset"
        run_root = root / "run"
        worker_root = root / "worker"
        dataset_root.mkdir()
        run_root.mkdir()
        build_dataset_bundle(dataset_root)
        build_run_bundle(run_root, dataset_root)
        add_executor_config(run_root)
        return dataset_root, run_root, worker_root

    def test_prepare_creates_an_exhaustive_label_free_worker_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            task_label_sha = sha256(dataset_root / "task-labels.jsonl")

            prepared = prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            report = validate_worker_bundle(worker_root)
            paths = sorted(
                str(path.relative_to(worker_root))
                for path in worker_root.rglob("*")
                if path.is_file()
            )
            manifest_text = (worker_root / "worker-manifest.json").read_text(encoding="utf-8")

        self.assertTrue(prepared["passed"])
        self.assertTrue(report["passed"])
        self.assertNotIn("task-labels.jsonl", paths)
        self.assertFalse(any("label" in path.lower() for path in paths))
        self.assertFalse(any(path.startswith("governance/") for path in paths))
        self.assertNotIn(task_label_sha, manifest_text)
        self.assertIn("task-inputs.jsonl", paths)
        self.assertIn("snapshots/snapshot-01.tar.zst", paths)
        self.assertFalse(report["qualification_eligible"])

    def test_worker_task_jsonl_uses_the_frozen_contract_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            original_read = executor_module._read_regular
            task_limits: list[int] = []

            def recording_read(bundle_root: Path, relative: str, limit: int) -> bytes:
                if relative == "task-inputs.jsonl":
                    task_limits.append(limit)
                return original_read(bundle_root, relative, limit)

            with patch.object(
                executor_module,
                "_read_regular",
                side_effect=recording_read,
            ):
                loaded = executor_module._load_worker_bundle(worker_root)

        self.assertTrue(loaded["tasks"])
        self.assertTrue(task_limits)
        self.assertIn(executor_module.MAX_JSONL_BYTES, task_limits)
        self.assertNotIn(executor_module.MAX_JSON_BYTES, task_limits)

    def test_callable_worker_runs_paired_arms_from_independent_clones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            snapshot = worker_root / "snapshots/snapshot-01.tar.zst"
            source_snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            handler = RecordingHandler(mutate_snapshot=True)

            result = execute_worker_bundle(
                worker_root,
                execution_root,
                handler,
                sleeper=lambda _seconds: None,
            )
            validation = validate_execution_bundle(worker_root, execution_root)
            arm_outputs = [
                json.loads(line)
                for line in (execution_root / "arm-outputs.jsonl").read_text().splitlines()
            ]
            blinded = [
                json.loads(line)
                for line in (execution_root / "blinded-outputs.jsonl").read_text().splitlines()
            ]
            post_snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()

        self.assertTrue(result["passed"])
        self.assertTrue(validation["passed"])
        self.assertEqual(2, len(handler.requests))
        self.assertEqual({"no_memory", "memory_pack"}, {item.memory_mode for item in handler.requests})
        self.assertEqual(1, len({item.paired_input_sha256 for item in handler.requests}))
        self.assertNotEqual(handler.requests[0].workspace_root, handler.requests[1].workspace_root)
        self.assertEqual(source_snapshot_sha, post_snapshot_sha)
        self.assertEqual(2, len(arm_outputs))
        self.assertEqual(1, len({item["paired_input_sha256"] for item in arm_outputs}))
        self.assertTrue(all(item["outcome"] == "answer" for item in arm_outputs))
        self.assertTrue(all(set(item) == {"schema_version", "blinded_output_id", "outcome", "content"} for item in blinded))
        self.assertFalse(result["qualification_eligible"])
        self.assertIn("callable_handler_test_only", result["nonqualification_reasons"])
        self.assertIn("opaque_snapshot_adapter", result["nonqualification_reasons"])

    def test_frozen_retry_is_idempotent_and_completed_resume_skips_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            handler = RetryOnceHandler()
            first = execute_worker_bundle(
                worker_root,
                execution_root,
                handler,
                sleeper=lambda _seconds: None,
            )
            second = execute_worker_bundle(
                worker_root,
                execution_root,
                NeverCallHandler(),
                sleeper=lambda _seconds: None,
            )

        self.assertTrue(first["passed"])
        self.assertTrue(second["passed"])
        self.assertEqual({2}, set(handler.attempts.values()))
        self.assertEqual(first["execution_manifest_sha256"], second["execution_manifest_sha256"])

    def test_provider_drift_exhausts_frozen_retries_and_marks_run_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            handler = MismatchedProviderHandler()

            result = execute_worker_bundle(
                worker_root,
                execution_root,
                handler,
                sleeper=lambda _seconds: None,
            )
            outputs = [
                json.loads(line)
                for line in (execution_root / "arm-outputs.jsonl").read_text().splitlines()
            ]

        self.assertFalse(result["passed"])
        self.assertEqual("incomplete", result["execution_status"])
        self.assertEqual(6, len(handler.requests))
        self.assertTrue(all(item["outcome"] == "infrastructure_error" for item in outputs))
        self.assertTrue(all(item["error_category"] == "provider_identity_mismatch" for item in outputs))

    def test_tampered_execution_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
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
            trace = next((execution_root / "traces").glob("*.json"))
            trace.write_text("{}\n", encoding="utf-8")
            report = validate_execution_bundle(worker_root, execution_root)

        self.assertFalse(report["passed"])
        self.assertTrue(any("inventory_sha256_mismatch" in error for error in report["errors"]))

    def test_worker_rejects_extra_files_symlinks_and_resealed_config_drift(self) -> None:
        with self.subTest("extra-file"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root, run_root, worker_root = self.build_inputs(root)
                prepare_worker_bundle(
                    dataset_root,
                    run_root,
                    worker_root,
                    enforce_minimum_dataset_gates=False,
                )
                (worker_root / "extra.txt").write_text("extra", encoding="utf-8")
                report = validate_worker_bundle(worker_root)
            self.assertFalse(report["passed"])
            self.assertTrue(any("exhaustive_file_set_mismatch" in error for error in report["errors"]))

        with self.subTest("symlink"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root, run_root, worker_root = self.build_inputs(root)
                prepare_worker_bundle(
                    dataset_root,
                    run_root,
                    worker_root,
                    enforce_minimum_dataset_gates=False,
                )
                (worker_root / "unsafe-link").symlink_to(worker_root / "task-inputs.jsonl")
                report = validate_worker_bundle(worker_root)
            self.assertFalse(report["passed"])
            self.assertTrue(any("unsafe_filesystem_entry" in error for error in report["errors"]))

        with self.subTest("resealed-run-config-drift"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root, run_root, worker_root = self.build_inputs(root)
                prepare_worker_bundle(
                    dataset_root,
                    run_root,
                    worker_root,
                    enforce_minimum_dataset_gates=False,
                )
                config_path = worker_root / "worker-run-config.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config["generation"]["timeout_seconds"] = 999.0
                write_json(config_path, config)
                refresh_manifest_entry(
                    worker_root,
                    "worker-manifest.json",
                    "worker-run-config.json",
                )
                report = validate_worker_bundle(worker_root)
            self.assertFalse(report["passed"])
            self.assertTrue(any("sealed_projection_mismatch" in error for error in report["errors"]))

        with self.subTest("derived-role-path-alias"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset_root, run_root, worker_root = self.build_inputs(root)
                prepare_worker_bundle(
                    dataset_root,
                    run_root,
                    worker_root,
                    enforce_minimum_dataset_gates=False,
                )
                original_path = worker_root / "task-inputs.jsonl"
                alternate_path = worker_root / "alternate-task-inputs.jsonl"
                alternate_path.write_bytes(original_path.read_bytes())
                manifest_path = worker_root / "worker-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                original_entry = next(
                    item for item in manifest["files"] if item["role"] == "task-inputs"
                )
                original_entry["role"] = "shadow-task-inputs"
                manifest["files"].append(
                    {
                        **original_entry,
                        "path": "alternate-task-inputs.jsonl",
                        "role": "task-inputs",
                        "sha256": sha256(alternate_path),
                        "size_bytes": alternate_path.stat().st_size,
                    }
                )
                write_json(manifest_path, manifest)

                report = validate_worker_bundle(worker_root)

            self.assertFalse(report["passed"])
            self.assertTrue(any("canonical_path_mismatch" in error for error in report["errors"]))

    def test_resealed_audit_chain_tamper_fails_semantic_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
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
            audit_path = execution_root / "execution-audit.jsonl"
            events = [json.loads(line) for line in audit_path.read_text().splitlines()]
            events[0]["event_sha256"] = "0" * 64
            audit_path.write_bytes(b"".join(canonical_line(item) for item in events))
            refresh_manifest_entry(
                execution_root,
                "execution-manifest.json",
                "execution-audit.jsonl",
            )
            report = validate_execution_bundle(worker_root, execution_root)

        self.assertFalse(report["passed"])
        self.assertTrue(any("chain_invalid" in error for error in report["errors"]))

    def test_resealed_execution_cannot_replace_worker_tasks_or_detach_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
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

            outputs = [
                json.loads(line)
                for line in (execution_root / "arm-outputs.jsonl").read_text().splitlines()
            ]
            for output in outputs:
                output["task_id"] = "task-forged"
                result_relative = f"arms/{output['execution_id']}/result.json"
                write_json(execution_root / result_relative, output)
                refresh_manifest_entry(
                    execution_root,
                    "execution-manifest.json",
                    result_relative,
                )
            (execution_root / "arm-outputs.jsonl").write_bytes(
                b"".join(canonical_line(item) for item in outputs)
            )
            refresh_manifest_entry(
                execution_root,
                "execution-manifest.json",
                "arm-outputs.jsonl",
            )

            mappings = [
                json.loads(line)
                for line in (execution_root / "unblinding-map.jsonl").read_text().splitlines()
            ]
            for mapping in mappings:
                mapping["task_id"] = "task-forged"
            (execution_root / "unblinding-map.jsonl").write_bytes(
                b"".join(canonical_line(item) for item in mappings)
            )
            refresh_manifest_entry(
                execution_root,
                "execution-manifest.json",
                "unblinding-map.jsonl",
            )

            events = [
                json.loads(line)
                for line in (execution_root / "execution-audit.jsonl").read_text().splitlines()
            ]
            previous = None
            output_by_execution = {item["execution_id"]: item for item in outputs}
            for event in events:
                event["task_id"] = "task-forged"
                event["artifact_sha256"] = hashlib.sha256(
                    json.dumps(
                        output_by_execution[event["execution_id"]],
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        indent=2,
                    ).encode("utf-8")
                    + b"\n"
                ).hexdigest()
                event["previous_event_sha256"] = previous
                body = dict(event)
                body.pop("event_sha256")
                event["event_sha256"] = hashlib.sha256(canonical_line(body)).hexdigest()
                previous = event["event_sha256"]
            (execution_root / "execution-audit.jsonl").write_bytes(
                b"".join(canonical_line(item) for item in events)
            )
            manifest_path = execution_root / "execution-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["audit_chain_head"] = previous
            write_json(manifest_path, manifest)
            refresh_manifest_entry(
                execution_root,
                "execution-manifest.json",
                "execution-audit.jsonl",
            )

            report = validate_execution_bundle(worker_root, execution_root)

        self.assertFalse(report["passed"])
        self.assertTrue(
            any(
                marker in error
                for error in report["errors"]
                for marker in ("worker_task_mismatch", "request_binding_mismatch")
            )
        )

    def test_a_validly_rehashed_audit_chain_still_binds_each_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
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
            events = [
                json.loads(line)
                for line in (execution_root / "execution-audit.jsonl").read_text().splitlines()
            ]
            events[0]["artifact_sha256"] = "0" * 64
            previous = None
            for event in events:
                event["previous_event_sha256"] = previous
                body = dict(event)
                body.pop("event_sha256")
                event["event_sha256"] = hashlib.sha256(canonical_line(body)).hexdigest()
                previous = event["event_sha256"]
            audit_path = execution_root / "execution-audit.jsonl"
            audit_path.write_bytes(b"".join(canonical_line(item) for item in events))
            manifest_path = execution_root / "execution-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["audit_chain_head"] = previous
            write_json(manifest_path, manifest)
            refresh_manifest_entry(
                execution_root,
                "execution-manifest.json",
                "execution-audit.jsonl",
            )
            report = validate_execution_bundle(worker_root, execution_root)

        self.assertFalse(report["passed"])
        self.assertTrue(any("artifact_binding_mismatch" in error for error in report["errors"]))

    def test_unexpected_handler_exception_is_terminal_across_crash_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            first_handler = UnexpectedExceptionHandler()
            with patch(
                "benchmarks.officelife_track_b_executor._finalize_arm_result",
                side_effect=RuntimeError("simulated crash after attempt receipt"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    execute_worker_bundle(
                        worker_root,
                        execution_root,
                        first_handler,
                        sleeper=lambda _seconds: None,
                    )
            resumed_handler = UnexpectedExceptionHandler()
            result = execute_worker_bundle(
                worker_root,
                execution_root,
                resumed_handler,
                sleeper=lambda _seconds: None,
            )

        self.assertEqual(1, first_handler.call_count)
        self.assertEqual(1, resumed_handler.call_count)
        self.assertFalse(result["passed"])
        self.assertEqual("incomplete", result["execution_status"])

    def test_partial_resume_rejects_a_conflicting_request_before_handler_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            interrupted = RecordingHandler()
            with patch.object(
                interrupted,
                "execute",
                side_effect=KeyboardInterrupt("simulated crash after request"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    execute_worker_bundle(
                        worker_root,
                        execution_root,
                        interrupted,
                        sleeper=lambda _seconds: None,
                    )
            request_path = next((execution_root / "arms").glob("*/request.json"))
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["memory_pack_present"] = not request["memory_pack_present"]
            write_json(request_path, request)
            resumed = RecordingHandler()

            with self.assertRaisesRegex(ExecutorValidationError, "request"):
                execute_worker_bundle(
                    worker_root,
                    execution_root,
                    resumed,
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual([], resumed.requests)

    def test_partial_resume_rejects_a_conflicting_attempt_before_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            with patch(
                "benchmarks.officelife_track_b_executor._finalize_arm_result",
                side_effect=RuntimeError("simulated crash after attempt receipt"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    execute_worker_bundle(
                        worker_root,
                        execution_root,
                        RecordingHandler(),
                        sleeper=lambda _seconds: None,
                    )
            receipt_path = next((execution_root / "arms").glob("*/attempts/000.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["finished_at"] = 0
            write_json(receipt_path, receipt)
            resumed = RecordingHandler()

            with self.assertRaisesRegex(ExecutorValidationError, "attempt"):
                execute_worker_bundle(
                    worker_root,
                    execution_root,
                    resumed,
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual([], resumed.requests)

    def test_partial_execution_rejects_symlinked_output_directories_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            outside = root / "outside"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            execution_root.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            (execution_root / "arms").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ExecutorValidationError):
                execute_worker_bundle(
                    worker_root,
                    execution_root,
                    RecordingHandler(),
                    sleeper=lambda _seconds: None,
                )
            self.assertEqual([], list(outside.iterdir()))

    def test_partial_execution_is_bound_to_the_original_worker_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root_a, worker_root_a = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root_a,
                worker_root_a,
                enforce_minimum_dataset_gates=False,
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_worker_bundle(
                    worker_root_a,
                    execution_root,
                    InterruptSecondHandler(),
                    sleeper=lambda _seconds: None,
                )

            run_root_b = root / "run-b"
            worker_root_b = root / "worker-b"
            run_root_b.mkdir()
            build_run_bundle(run_root_b, dataset_root)
            add_executor_config(run_root_b)
            rewrite_executor_config(
                run_root_b,
                lambda config: config.__setitem__(
                    "handler_id",
                    "synthetic-handler-b-v1",
                ),
            )
            prepare_worker_bundle(
                dataset_root,
                run_root_b,
                worker_root_b,
                enforce_minimum_dataset_gates=False,
            )
            resumed = RecordingHandler()
            resumed.handler_id = "synthetic-handler-b-v1"

            with self.assertRaisesRegex(ExecutorValidationError, "worker binding"):
                execute_worker_bundle(
                    worker_root_b,
                    execution_root,
                    resumed,
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual([], resumed.requests)

    def test_resume_preflights_every_existing_arm_before_any_handler_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_worker_bundle(
                    worker_root,
                    execution_root,
                    InterruptSecondHandler(),
                    sleeper=lambda _seconds: None,
                )
            arm_roots = list((execution_root / "arms").iterdir())
            completed_root = next(path for path in arm_roots if (path / "result.json").is_file())
            pending_root = next(path for path in arm_roots if not (path / "result.json").exists())
            request_path = pending_root / "request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["memory_pack_present"] = not request["memory_pack_present"]
            write_json(request_path, request)
            shutil.rmtree(completed_root)
            resumed = RecordingHandler()

            with self.assertRaisesRegex(ExecutorValidationError, "request"):
                execute_worker_bundle(
                    worker_root,
                    execution_root,
                    resumed,
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual([], resumed.requests)

    def test_resume_preflights_terminal_receipt_size_before_any_handler_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_worker_bundle(
                    worker_root,
                    execution_root,
                    InterruptSecondHandler(),
                    sleeper=lambda _seconds: None,
                )
            arm_roots = list((execution_root / "arms").iterdir())
            completed_root = next(path for path in arm_roots if (path / "result.json").is_file())
            pending_root = next(path for path in arm_roots if not (path / "result.json").exists())
            request = json.loads((pending_root / "request.json").read_text(encoding="utf-8"))
            memory_pack = (
                {
                    "contract_version": "agent-turn-v1",
                    "coverage": "supported",
                    "context_markdown": "# MemoryPack\n",
                }
                if request["arm"] == "memory_pack"
                else None
            )
            receipt = {
                "schema_version": "officelife-track-b-attempt-receipt-v1",
                "attempt_id": executor_module._attempt_id(request["execution_id"], 0),
                "attempt_index": 0,
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "status": "handler_result",
                "error_category": None,
                "retryable": False,
                "handler_result": {
                    "outcome": "answer",
                    "content": "x" * 3000,
                    "actual_model_id": "provider-a/model-2026-01-01",
                    "actual_model_version": "model-2026-01-01",
                    "actual_upstream_provider": "provider-a",
                    "actual_route": "provider-a/route-01",
                    "fallback_used": False,
                    "memory_pack": memory_pack,
                    "trace": {},
                    "usage": {},
                    "error_category": None,
                },
            }
            write_json(pending_root / "attempts/000.json", receipt)
            completed_execution_id = completed_root.name
            shutil.rmtree(completed_root)
            (execution_root / "traces" / f"{completed_execution_id}.json").unlink()
            resumed = RecordingHandler()

            with patch.object(executor_module, "MAX_AGGREGATE_BYTES", 4096), patch.object(
                executor_module,
                "MIN_AGGREGATE_RECORD_BYTES",
                512,
            ):
                with self.assertRaisesRegex(ExecutorValidationError, "artifact envelope"):
                    execute_worker_bundle(
                        worker_root,
                        execution_root,
                        resumed,
                        sleeper=lambda _seconds: None,
                    )

        self.assertEqual([], resumed.requests)

    def test_schema_invalid_handler_result_uses_frozen_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            handler = SchemaInvalidResultHandler()

            result = execute_worker_bundle(
                worker_root,
                execution_root,
                handler,
                sleeper=lambda _seconds: None,
            )
            outputs = [
                json.loads(line)
                for line in (execution_root / "arm-outputs.jsonl").read_text().splitlines()
            ]

        self.assertTrue(result["validation_passed"])
        self.assertFalse(result["passed"])
        self.assertEqual(6, len(handler.requests))
        self.assertTrue(
            all(item["error_category"] == "handler_output_invalid" for item in outputs)
        )
        self.assertTrue(all(item["attempt_count"] == 3 for item in outputs))

    def test_oversized_handler_trace_finishes_as_a_valid_incomplete_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            handler = OversizedTraceHandler()

            result = execute_worker_bundle(
                worker_root,
                execution_root,
                handler,
                sleeper=lambda _seconds: None,
            )
            resumed = execute_worker_bundle(
                worker_root,
                execution_root,
                NeverCallHandler(),
                sleeper=lambda _seconds: None,
            )
            outputs = [
                json.loads(line)
                for line in (execution_root / "arm-outputs.jsonl").read_text().splitlines()
            ]

        self.assertTrue(result["validation_passed"])
        self.assertFalse(result["passed"])
        self.assertEqual(result, resumed)
        self.assertEqual(6, len(handler.requests))
        self.assertTrue(
            all(item["error_category"] == "handler_output_invalid" for item in outputs)
        )

    def test_aggregate_budget_rejects_large_outputs_before_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            handler = LargeContentHandler()

            with patch.object(executor_module, "MAX_AGGREGATE_BYTES", 4096), patch.object(
                executor_module,
                "MIN_AGGREGATE_RECORD_BYTES",
                512,
            ):
                result = execute_worker_bundle(
                    worker_root,
                    execution_root,
                    handler,
                    sleeper=lambda _seconds: None,
                )
                validation = validate_execution_bundle(worker_root, execution_root)

            aggregate_sizes = {
                name: (execution_root / name).stat().st_size
                for name in (
                    "arm-outputs.jsonl",
                    "blinded-outputs.jsonl",
                    "unblinding-map.jsonl",
                    "execution-audit.jsonl",
                )
            }

        self.assertTrue(result["validation_passed"])
        self.assertFalse(result["passed"])
        self.assertTrue(validation["validation_passed"])
        self.assertEqual(6, len(handler.requests))
        self.assertTrue(all(size <= 4096 for size in aggregate_sizes.values()))

    def test_aggregate_capacity_fails_before_creating_or_calling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            handler = RecordingHandler()

            with patch.object(executor_module, "MAX_AGGREGATE_BYTES", 4096), patch.object(
                executor_module,
                "MIN_AGGREGATE_RECORD_BYTES",
                4096,
            ):
                with self.assertRaisesRegex(ExecutorValidationError, "too many arms"):
                    execute_worker_bundle(
                        worker_root,
                        execution_root,
                        handler,
                        sleeper=lambda _seconds: None,
                    )

            destination_exists = execution_root.exists()

        self.assertFalse(destination_exists)
        self.assertEqual([], handler.requests)

    def test_stale_sibling_lock_does_not_block_crash_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            lock_path = execution_root.with_name(f".{execution_root.name}.executor.lock")
            lock_path.write_text("stale\n", encoding="utf-8")
            lock_path.chmod(0o600)

            result = execute_worker_bundle(
                worker_root,
                execution_root,
                RecordingHandler(),
                sleeper=lambda _seconds: None,
            )
            lock_survived = lock_path.is_file()
            lock_mode = stat.S_IMODE(lock_path.stat().st_mode)

        self.assertTrue(result["passed"])
        self.assertTrue(lock_survived)
        self.assertEqual(0o600, lock_mode)

    def test_active_sibling_lock_rejects_a_second_executor_before_handler_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )
            execution_root.mkdir(mode=0o700)
            lock_path = execution_root.with_name(f".{execution_root.name}.executor.lock")
            lock_descriptor = executor_module.os.open(
                lock_path,
                executor_module.os.O_RDWR | executor_module.os.O_CREAT,
                0o600,
            )
            executor_module.fcntl.flock(
                lock_descriptor,
                executor_module.fcntl.LOCK_EX | executor_module.fcntl.LOCK_NB,
            )
            handler = RecordingHandler()
            try:
                with self.assertRaisesRegex(ExecutorValidationError, "another executor"):
                    execute_worker_bundle(
                        worker_root,
                        execution_root,
                        handler,
                        sleeper=lambda _seconds: None,
                    )
            finally:
                executor_module.fcntl.flock(
                    lock_descriptor,
                    executor_module.fcntl.LOCK_UN,
                )
                executor_module.os.close(lock_descriptor)

        self.assertEqual([], handler.requests)

    def test_private_bundle_directories_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            execution_root = root / "execution"
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
            directory_modes = {
                stat.S_IMODE(path.stat().st_mode)
                for bundle in (worker_root, execution_root)
                for path in (bundle, *[item for item in bundle.rglob("*") if item.is_dir()])
            }

        self.assertEqual({0o700}, directory_modes)

    def test_callable_executor_rejects_external_process_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root, run_root, worker_root = self.build_inputs(root)
            rewrite_executor_config(
                run_root,
                lambda config: config.update(
                    {
                        "handler_protocol": "external-process-v1",
                        "worker_isolation": "os-sandbox-required",
                    }
                ),
            )
            prepare_worker_bundle(
                dataset_root,
                run_root,
                worker_root,
                enforce_minimum_dataset_gates=False,
            )

            with self.assertRaisesRegex(ExecutorValidationError, "callable test adapter"):
                execute_worker_bundle(
                    worker_root,
                    root / "execution",
                    RecordingHandler(),
                )

    def test_prepare_requires_a_frozen_executor_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_root = root / "dataset"
            run_root = root / "run"
            dataset_root.mkdir()
            run_root.mkdir()
            build_dataset_bundle(dataset_root)
            build_run_bundle(run_root, dataset_root)

            with self.assertRaisesRegex(ValueError, "executor config"):
                prepare_worker_bundle(
                    dataset_root,
                    run_root,
                    root / "worker",
                    enforce_minimum_dataset_gates=False,
                )


if __name__ == "__main__":
    unittest.main()
