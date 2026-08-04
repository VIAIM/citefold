from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from jsonschema import Draft202012Validator

from citefold import Citefold, MemoryPack, MemoryScope, inspect_store, restore_store, verify_backup
from citefold.core import MIN_MEMORY_PACK_TOKEN_BUDGET


LATENCY_ASSAY_VERSION = "officelife-track-b-latency-v1"
LATENCY_CONFIG_SCHEMA_VERSION = "officelife-track-b-latency-config-v1"
LATENCY_MANIFEST_SCHEMA_VERSION = "officelife-track-b-latency-manifest-v1"
LATENCY_SUMMARY_SCHEMA_VERSION = "officelife-track-b-latency-summary-v1"
LATENCY_AUDIT_SCHEMA_VERSION = "officelife-track-b-latency-audit-v1"
LATENCY_SAMPLE_SCHEMA_VERSION = "officelife-track-b-latency-sample-v1"
LATENCY_VALIDATION_SCHEMA_VERSION = "officelife-track-b-latency-validation-v1"

QUERY_COUNT = 100
WARMUP_PASSES = 1
MEASURED_PASSES = 10
MEASURED_COUNT = QUERY_COUNT * MEASURED_PASSES
STORED_OBSERVATION_COUNT = 1000
LATENCY_GATE_MS = 300.0

MAX_CONFIG_BYTES = 1024 * 1024
MAX_QUERY_BYTES = 4 * 1024 * 1024
MAX_QUERY_LINE_BYTES = 64 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_DISTRIBUTION_BYTES = 4 * 1024 * 1024 * 1024
MAX_FIXTURE_BYTES = 4 * 1024 * 1024 * 1024
MAX_JSON_DEPTH = 64

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
SAFE_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,254}\Z", re.ASCII)

BUNDLE_FILES = {
    "latency-config": ("latency-config.json", "application/json"),
    "queries": ("queries.jsonl", "application/x-ndjson"),
    "raw-durations": ("raw-durations.jsonl", "application/x-ndjson"),
    "latency-summary": ("latency-summary.json", "application/json"),
    "latency-audit": ("latency-audit.json", "application/json"),
}
NONQUALIFICATION_REASONS = (
    "custodian_signature_missing",
    "os_sandbox_attestation_missing",
    "reference_environment_attestation_missing",
    "release_runtime_binding_unverified",
)
QUALIFICATION_SCHEMA_PATH = (
    Path(__file__).with_name("schemas")
    / "officelife_track_b_qualification"
    / "v1"
    / "qualification.schema.json"
)


class LatencyAssayError(ValueError):
    """Raised when the controlled latency assay must fail closed."""


def run_latency_assay(
    fixture_archive: Path,
    query_path: Path,
    config_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Run the frozen local Track B recall-latency measurement.

    The function deliberately offers no engine or restore adapter injection.
    Tests may patch module boundaries, but the public production path always
    restores a verified Citefold backup and calls ``Citefold.recall`` with
    ``openrouter=None``.
    """

    fixture = _input_path(fixture_archive, "fixture archive")
    queries_source = _input_path(query_path, "query set")
    config_source = _input_path(config_path, "latency config")
    destination = _output_path(output_root)

    config_raw = _read_unique_file(config_source, MAX_CONFIG_BYTES, "latency config")
    config = _decode_json_object(config_raw)
    if config_raw != _canonical_json(config, pretty=True):
        raise LatencyAssayError("latency config must use canonical pretty JSON encoding")
    scope = _validate_config(config, fixture.name, queries_source.name)

    release_filename = str(config["release_distribution"]["filename"])
    release_path = _input_path(config_source.parent / release_filename, "release distribution")

    fixture_digest, fixture_size = _hash_unique_file(
        fixture,
        MAX_FIXTURE_BYTES,
        "fixture archive",
    )
    release_digest, release_size = _hash_unique_file(
        release_path,
        MAX_DISTRIBUTION_BYTES,
        "release distribution",
    )
    queries_raw = _read_unique_file(queries_source, MAX_QUERY_BYTES, "query set")
    queries_digest = _sha256(queries_raw)
    _require_digest_match(config["fixture"], fixture_digest, "fixture archive")
    _require_digest_match(config["queries"], queries_digest, "query set")
    _require_digest_match(config["release_distribution"], release_digest, "release distribution")

    queries = _decode_queries(queries_raw)
    query_order_sha256 = _query_order_sha256(queries)
    verified = verify_backup(fixture)
    if verified.verified is not True or verified.sha256 != fixture_digest:
        raise LatencyAssayError("fixture backup verification did not preserve its SHA-256 binding")

    _prepare_output_parent(destination)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.build-", dir=str(destination.parent))
    )
    generated_at = _utc_now()
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{destination.name}.fixture-",
            dir=str(destination.parent),
        ) as restore_tmp:
            restored_root = Path(restore_tmp) / "store"
            restored = restore_store(restored_root, fixture)
            if restored.status != "restored" or restored.fingerprint != verified.fingerprint:
                raise LatencyAssayError("fixture restore did not match the verified backup fingerprint")
            engine = Citefold(restored_root, openrouter=None)
            observation_count = _validate_restored_fixture(engine, scope)
            samples = _run_measurements(engine, scope, queries, time.perf_counter_ns)

        if _read_unique_file(config_source, MAX_CONFIG_BYTES, "latency config") != config_raw:
            raise LatencyAssayError("latency config changed during the assay")
        if _read_unique_file(queries_source, MAX_QUERY_BYTES, "query set") != queries_raw:
            raise LatencyAssayError("query set changed during the assay")
        final_fixture_digest, final_fixture_size = _hash_unique_file(
            fixture,
            MAX_FIXTURE_BYTES,
            "fixture archive",
        )
        final_release_digest, final_release_size = _hash_unique_file(
            release_path,
            MAX_DISTRIBUTION_BYTES,
            "release distribution",
        )
        if (final_fixture_digest, final_fixture_size) != (fixture_digest, fixture_size):
            raise LatencyAssayError("fixture archive changed during the assay")
        if (final_release_digest, final_release_size) != (release_digest, release_size):
            raise LatencyAssayError("release distribution changed during the assay")

        durations_ms = [sample["duration_ns"] / 1_000_000.0 for sample in samples]
        p50_ms = nearest_rank(durations_ms, 50)
        p95_ms = nearest_rank(durations_ms, 95)
        gate_passed = p95_ms <= LATENCY_GATE_MS
        summary = _summary(
            observation_count=observation_count,
            p50_ms=p50_ms,
            p95_ms=p95_ms,
            gate_passed=gate_passed,
        )
        audit = _audit(
            generated_at=generated_at,
            fixture_digest=fixture_digest,
            fixture_size=fixture_size,
            fixture_fingerprint=str(verified.fingerprint),
            fixture_schema_version=verified.schema_version,
            fixture_file_count=verified.file_count,
            fixture_total_bytes=verified.total_bytes,
            release_digest=release_digest,
            release_size=release_size,
            queries_digest=queries_digest,
            query_order_sha256=query_order_sha256,
        )

        _write_new_file(temporary / "latency-config.json", config_raw)
        _write_new_file(temporary / "queries.jsonl", queries_raw)
        _write_new_file(
            temporary / "raw-durations.jsonl",
            b"".join(_canonical_json(sample) for sample in samples),
        )
        _write_new_file(
            temporary / "latency-summary.json",
            _canonical_json(summary, pretty=True),
        )
        _write_new_file(
            temporary / "latency-audit.json",
            _canonical_json(audit, pretty=True),
        )

        manifest = {
            "schema_version": LATENCY_MANIFEST_SCHEMA_VERSION,
            "latency_assay_version": LATENCY_ASSAY_VERSION,
            "generated_at": generated_at,
            "fixture": {
                "filename": fixture.name,
                "sha256": fixture_digest,
                "size_bytes": fixture_size,
                "backup_fingerprint": str(verified.fingerprint),
            },
            "queries": {
                "sha256": queries_digest,
                "query_count": len(queries),
                "query_order_sha256": query_order_sha256,
            },
            "release_distribution": {
                "filename": release_filename,
                "sha256": release_digest,
                "size_bytes": release_size,
            },
            "scope": scope.as_record(),
            "measurement": {
                "stored_observations": observation_count,
                "warmup_passes": WARMUP_PASSES,
                "warmup_iterations": QUERY_COUNT,
                "measured_passes": MEASURED_PASSES,
                "measured_iterations": len(samples),
                "clock": "time.perf_counter_ns",
                "public_call": "Citefold.recall",
                "response_materialization": "canonical-json-sha256-v1",
            },
            "files": _bundle_inventory(temporary),
            "measurement_complete": True,
            "qualification_eligible": False,
            "claimable": False,
            "nonqualification_reasons": list(NONQUALIFICATION_REASONS),
        }
        _write_new_file(
            temporary / "latency-manifest.json",
            _canonical_json(manifest, pretty=True),
        )
        validation = validate_latency_bundle(temporary)
        if not validation["passed"]:
            raise LatencyAssayError("generated latency bundle failed self-validation")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return validate_latency_bundle(destination)


def validate_latency_bundle(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    manifest_raw: bytes | None = None
    manifest: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    try:
        bundle = _bundle_root(root)
        manifest_raw = _read_bundle_file(
            bundle,
            "latency-manifest.json",
            MAX_JSON_BYTES,
        )
        manifest = _decode_json_object(manifest_raw)
        if manifest_raw != _canonical_json(manifest, pretty=True):
            errors.append("latency-manifest.json# noncanonical_json")
        entries = _validate_inventory(bundle, manifest, errors)
        summary = _validate_bundle_semantics(bundle, manifest, entries, errors)
    except (OSError, LatencyAssayError, ValueError):
        errors.append("latency_bundle# unreadable_or_invalid")

    validation_passed = not errors
    measurement_complete = validation_passed and summary.get("measurement_complete") is True
    reasons = manifest.get("nonqualification_reasons")
    if not isinstance(reasons, list):
        reasons = []
    return {
        "schema_version": LATENCY_VALIDATION_SCHEMA_VERSION,
        "latency_assay_version": LATENCY_ASSAY_VERSION,
        "artifact_scope": "private-controlled-recall-latency",
        "private": True,
        "validation_passed": validation_passed,
        "measurement_complete": measurement_complete,
        "sample_count": summary.get("sample_count", 0) if validation_passed else 0,
        "p50_ms": summary.get("p50_ms") if validation_passed else None,
        "p95_ms": summary.get("p95_ms") if validation_passed else None,
        "gate_passed": bool(summary.get("gate_passed")) if measurement_complete else False,
        "qualification_eligible": False,
        "claimable": False,
        "nonqualification_reasons": list(reasons),
        "errors": sorted(set(errors)),
        "latency_manifest_sha256": _sha256(manifest_raw) if manifest_raw is not None else None,
        "passed": validation_passed and measurement_complete,
    }


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise ValueError("percentile must be a finite number in (0, 100]")
    numeric_percentile = float(percentile)
    if not math.isfinite(numeric_percentile) or not 0.0 < numeric_percentile <= 100.0:
        raise ValueError("percentile must be a finite number in (0, 100]")
    ordered: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("nearest-rank values must be finite nonnegative numbers")
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            raise ValueError("nearest-rank values must be finite nonnegative numbers")
        ordered.append(number)
    if not ordered:
        raise ValueError("nearest-rank requires at least one value")
    ordered.sort()
    rank = max(1, math.ceil((numeric_percentile / 100.0) * len(ordered)))
    return ordered[rank - 1]


def _validate_config(
    config: dict[str, Any],
    fixture_filename: str,
    query_filename: str,
) -> MemoryScope:
    _require_unified_schema(config, "latencyConfig", "latency config")
    _require_exact_keys(
        config,
        {
            "schema_version",
            "fixture",
            "queries",
            "release_distribution",
            "scope",
            "reference_environment",
        },
        "latency config",
    )
    if config["schema_version"] != LATENCY_CONFIG_SCHEMA_VERSION:
        raise LatencyAssayError("latency config schema version is not supported")
    _validate_artifact_reference(config["fixture"], "fixture")
    _validate_artifact_reference(config["queries"], "queries")
    _validate_artifact_reference(config["release_distribution"], "release distribution")
    if config["fixture"]["filename"] != fixture_filename:
        raise LatencyAssayError("fixture filename does not match the supplied archive")
    if config["queries"]["filename"] != query_filename:
        raise LatencyAssayError("query filename does not match the supplied query set")
    release_filename = config["release_distribution"]["filename"]
    if not (
        str(release_filename).endswith(".whl")
        or str(release_filename).endswith(".tar.gz")
    ):
        raise LatencyAssayError("release distribution must be a wheel or source archive")

    scope_value = config["scope"]
    _require_exact_keys(
        scope_value,
        {"tenant_id", "user_id", "namespace", "agent_id", "session_id"},
        "latency config scope",
    )
    try:
        scope = MemoryScope(**scope_value)
    except (TypeError, ValueError) as exc:
        raise LatencyAssayError("latency config has an invalid complete MemoryScope") from exc

    environment = config["reference_environment"]
    _require_exact_keys(
        environment,
        {
            "hardware_model",
            "cpu_model",
            "logical_cpu_count",
            "memory_bytes",
            "storage_type",
            "filesystem_type",
            "operating_system",
            "operating_system_version",
            "python_implementation",
            "python_version",
        },
        "reference environment",
    )
    for field_name in (
        "hardware_model",
        "cpu_model",
        "operating_system",
        "operating_system_version",
        "python_implementation",
        "python_version",
    ):
        _safe_environment_string(environment[field_name], field_name)
    for field_name in ("logical_cpu_count", "memory_bytes"):
        value = environment[field_name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise LatencyAssayError(f"reference environment {field_name} must be a positive integer")
    if environment["storage_type"] not in {"local-ssd", "local-hdd", "local-other"}:
        raise LatencyAssayError("reference environment storage_type must identify local storage")
    if not _valid_id(environment["filesystem_type"]):
        raise LatencyAssayError("reference environment filesystem_type is unsafe")
    return scope


def _validate_artifact_reference(value: Any, label: str) -> None:
    _require_exact_keys(value, {"filename", "sha256"}, f"{label} reference")
    if not _safe_filename(value["filename"]):
        raise LatencyAssayError(f"{label} filename is unsafe")
    if not _is_sha256(value["sha256"]):
        raise LatencyAssayError(f"{label} SHA-256 is invalid")


def _safe_environment_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise LatencyAssayError(f"reference environment {field_name} is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LatencyAssayError(f"reference environment {field_name} contains control characters")


def _decode_queries(raw: bytes) -> list[dict[str, Any]]:
    records = _decode_canonical_jsonl(raw, MAX_QUERY_LINE_BYTES)
    if len(records) != QUERY_COUNT:
        raise LatencyAssayError("query set must contain exactly 100 queries")
    query_ids: set[str] = set()
    for record in records:
        _require_exact_keys(record, {"query_id", "query", "mode", "token_budget"}, "query")
        query_id = record["query_id"]
        if not _valid_id(query_id):
            raise LatencyAssayError("query_id must be a safe identifier")
        if query_id in query_ids:
            raise LatencyAssayError("query_id values must be unique")
        query_ids.add(query_id)
        query = record["query"]
        if (
            not isinstance(query, str)
            or not query.strip()
            or query != query.strip()
            or len(query) > 8192
            or "\x00" in query
            or "\r" in query
            or "\n" in query
        ):
            raise LatencyAssayError("query must be a bounded single-line non-empty string")
        if record["mode"] not in {"text", "voice"}:
            raise LatencyAssayError("query mode must be text or voice")
        token_budget = record["token_budget"]
        if (
            isinstance(token_budget, bool)
            or not isinstance(token_budget, int)
            or token_budget < MIN_MEMORY_PACK_TOKEN_BUDGET
            or token_budget > 1_000_000
        ):
            raise LatencyAssayError(
                f"query token_budget must be between {MIN_MEMORY_PACK_TOKEN_BUDGET} and 1000000"
            )
    return records


def _validate_restored_fixture(engine: Citefold, scope: MemoryScope) -> int:
    status = inspect_store(engine.root)
    if status.state != "current" or status.scope_count != 1:
        raise LatencyAssayError("fixture must contain exactly one current Citefold user scope")
    scope_root = engine.store.scope_root(scope)
    if not scope_root.is_dir():
        raise LatencyAssayError("fixture does not contain the configured MemoryScope user root")
    observations = engine.store.observations(scope)
    if len(observations) != STORED_OBSERVATION_COUNT:
        raise LatencyAssayError("fixture must contain exactly 1000 finalized observations")
    expected_scope = scope.as_record()
    for observation_id, observation in observations.items():
        if not isinstance(observation, dict) or observation.get("observation_id") != observation_id:
            raise LatencyAssayError("fixture observation identity is invalid")
        if observation.get("scope") != expected_scope:
            raise LatencyAssayError("fixture observation escapes the configured complete MemoryScope")
        metadata = observation.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("final") is not True:
            raise LatencyAssayError("fixture must contain exactly 1000 finalized observations")
        is_voice = metadata.get("mode") == "voice" or observation.get("modality") == "audio_transcript"
        if is_voice and (
            metadata.get("partial") is True
            or metadata.get("asr_state") in {"partial", "interim"}
            or metadata.get("lifecycle_state") in {"partial", "interim"}
        ):
            raise LatencyAssayError("fixture must not persist partial voice observations")
    return len(observations)


def _run_measurements(
    engine: Citefold,
    scope: MemoryScope,
    queries: list[dict[str, Any]],
    clock_ns: Callable[[], int],
) -> list[dict[str, Any]]:
    for query in queries:
        pack = engine.recall(
            scope,
            query["query"],
            mode=query["mode"],
            token_budget=query["token_budget"],
            include_archived=False,
        )
        _memory_pack_digest(pack, scope)

    samples: list[dict[str, Any]] = []
    order = 0
    for pass_index in range(1, MEASURED_PASSES + 1):
        for query_index, query in enumerate(queries, start=1):
            started = clock_ns()
            pack = engine.recall(
                scope,
                query["query"],
                mode=query["mode"],
                token_budget=query["token_budget"],
                include_archived=False,
            )
            response_digest = _memory_pack_digest(pack, scope)
            finished = clock_ns()
            if (
                isinstance(started, bool)
                or not isinstance(started, int)
                or isinstance(finished, bool)
                or not isinstance(finished, int)
                or finished < started
            ):
                raise LatencyAssayError("clock_ns must return monotonic integer nanoseconds")
            order += 1
            samples.append(
                {
                    "schema_version": LATENCY_SAMPLE_SCHEMA_VERSION,
                    "pass_index": pass_index,
                    "query_index": query_index,
                    "measurement_order": order,
                    "query_id": query["query_id"],
                    "duration_ns": finished - started,
                    "response_sha256": response_digest,
                }
            )
    if len(samples) != MEASURED_COUNT:
        raise AssertionError("frozen latency loop did not produce exactly 1000 samples")
    return samples


def _memory_pack_digest(pack: MemoryPack, scope: MemoryScope) -> str:
    if not isinstance(pack, MemoryPack):
        raise LatencyAssayError("Citefold.recall did not return a MemoryPack")
    if pack.identity_scope != scope.as_record():
        raise LatencyAssayError("MemoryPack identity scope changed during the assay")
    value = {
        "markdown": pack.markdown,
        "selected_nodes": [
            {"node_id": node.node_id, "path": node.path, "reason": node.reason}
            for node in pack.selected_nodes
        ],
        "identity_scope": dict(pack.identity_scope),
        "confirmed": pack.confirmed,
        "user_reported": pack.user_reported,
        "preferences": pack.preferences,
        "open_tasks": pack.open_tasks,
        "procedures": pack.procedures,
        "episodes": pack.episodes,
        "conflicts": pack.conflicts,
        "pending_inferences": pack.pending_inferences,
        "unknowns": pack.unknowns,
        "citations": pack.citations,
        "coverage": pack.coverage,
    }
    return _sha256(_canonical_json(value))


def _summary(
    *,
    observation_count: int,
    p50_ms: float,
    p95_ms: float,
    gate_passed: bool,
) -> dict[str, Any]:
    return {
        "schema_version": LATENCY_SUMMARY_SCHEMA_VERSION,
        "latency_assay_version": LATENCY_ASSAY_VERSION,
        "stored_observations": observation_count,
        "processes": 1,
        "threads": 1,
        "concurrency": 1,
        "provider_calls_in_measured_interval": 0,
        "model_calls_in_measured_interval": 0,
        "ingestion_calls_in_measured_interval": 0,
        "consolidation_calls_in_measured_interval": 0,
        "warmup_passes": WARMUP_PASSES,
        "warmup_iterations": QUERY_COUNT,
        "measured_passes": MEASURED_PASSES,
        "measured_iterations": MEASURED_COUNT,
        "sample_count": MEASURED_COUNT,
        "percentile_method": "nearest-rank",
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "gate_threshold_ms": LATENCY_GATE_MS,
        "gate_passed": gate_passed,
        "measurement_complete": True,
        "qualification_eligible": False,
        "claimable": False,
        "nonqualification_reasons": list(NONQUALIFICATION_REASONS),
    }


def _audit(
    *,
    generated_at: str,
    fixture_digest: str,
    fixture_size: int,
    fixture_fingerprint: str,
    fixture_schema_version: int | None,
    fixture_file_count: int,
    fixture_total_bytes: int,
    release_digest: str,
    release_size: int,
    queries_digest: str,
    query_order_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": LATENCY_AUDIT_SCHEMA_VERSION,
        "latency_assay_version": LATENCY_ASSAY_VERSION,
        "generated_at": generated_at,
        "controls": {
            "fixture_backup_verified": True,
            "fixture_restored_to_disposable_root": True,
            "single_configured_scope": True,
            "all_observations_final": True,
            "partial_voice_observations": 0,
            "public_recall_entrypoint": "Citefold.recall",
            "openrouter_disabled": True,
            "fixed_query_order": True,
            "warmup_passes": WARMUP_PASSES,
            "measured_passes": MEASURED_PASSES,
            "response_materialized_before_clock_end": True,
        },
        "bindings": {
            "fixture_sha256": fixture_digest,
            "fixture_size_bytes": fixture_size,
            "fixture_backup_fingerprint": fixture_fingerprint,
            "fixture_schema_version": fixture_schema_version,
            "fixture_file_count": fixture_file_count,
            "fixture_total_bytes": fixture_total_bytes,
            "queries_sha256": queries_digest,
            "query_order_sha256": query_order_sha256,
            "release_distribution_sha256": release_digest,
            "release_distribution_size_bytes": release_size,
        },
        "attestations": {
            "os_sandbox": "missing",
            "network_isolation": "not_independently_attested",
            "external_process_and_thread_count": "not_independently_attested",
        },
        "measurement_complete": True,
        "qualification_eligible": False,
        "claimable": False,
        "nonqualification_reasons": list(NONQUALIFICATION_REASONS),
    }


def _validate_inventory(
    root: Path,
    manifest: dict[str, Any],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        errors.append("inventory# missing")
        return {}
    entries: dict[str, dict[str, Any]] = {}
    paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            errors.append("inventory# entry_not_object")
            continue
        if set(entry) != {"path", "role", "sha256", "size_bytes", "media_type"}:
            errors.append("inventory# entry_shape_invalid")
            continue
        role = entry.get("role")
        relative = entry.get("path")
        if not _valid_id(role) or not _safe_relative_path(relative):
            errors.append("inventory# unsafe_role_or_path")
            continue
        if role in entries or relative in paths:
            errors.append("inventory# duplicate_role_or_path")
        entries[str(role)] = entry
        paths.add(str(relative))

    expected_by_role = {
        role: {"path": relative, "media_type": media_type}
        for role, (relative, media_type) in BUNDLE_FILES.items()
    }
    if set(entries) != set(expected_by_role):
        errors.append("inventory# required_roles_mismatch")
    for role, expected in expected_by_role.items():
        entry = entries.get(role)
        if entry is not None and (
            entry.get("path") != expected["path"]
            or entry.get("media_type") != expected["media_type"]
        ):
            errors.append(f"inventory#{role}_binding_mismatch")

    try:
        discovered = _scan_flat_bundle(root, exclude={"latency-manifest.json"})
    except LatencyAssayError:
        errors.append("inventory# unsafe_filesystem_entry")
        return entries
    if paths != discovered:
        errors.append("inventory# exhaustive_file_set_mismatch")
    for entry in entries.values():
        relative = str(entry["path"])
        limit = MAX_RAW_BYTES if relative == "raw-durations.jsonl" else MAX_JSON_BYTES
        if relative == "queries.jsonl":
            limit = MAX_QUERY_BYTES
        try:
            raw = _read_bundle_file(root, relative, limit)
        except (OSError, LatencyAssayError):
            errors.append(f"{relative}# unreadable_or_unsafe")
            continue
        if entry.get("size_bytes") != len(raw):
            errors.append(f"{relative}# size_mismatch")
        if entry.get("sha256") != _sha256(raw):
            errors.append(f"{relative}# sha256_mismatch")
    return entries


def _validate_bundle_semantics(
    root: Path,
    manifest: dict[str, Any],
    entries: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    _validate_manifest_shape(manifest, errors)
    documents: dict[str, Any] = {}
    for role in ("latency-config", "latency-summary", "latency-audit"):
        entry = entries.get(role)
        if entry is None:
            continue
        try:
            raw = _read_bundle_file(root, str(entry["path"]), MAX_JSON_BYTES)
            value = _decode_json_object(raw)
            if raw != _canonical_json(value, pretty=True):
                errors.append(f"{entry['path']}# noncanonical_json")
            documents[role] = value
        except (OSError, LatencyAssayError):
            errors.append(f"{entry['path']}# invalid_json")
    try:
        query_raw = _read_bundle_file(root, "queries.jsonl", MAX_QUERY_BYTES)
        queries = _decode_queries(query_raw)
    except (OSError, LatencyAssayError):
        queries = []
        query_raw = b""
        errors.append("queries.jsonl# invalid")
    try:
        raw_durations = _decode_canonical_jsonl(
            _read_bundle_file(root, "raw-durations.jsonl", MAX_RAW_BYTES),
            MAX_QUERY_LINE_BYTES,
        )
    except (OSError, LatencyAssayError):
        raw_durations = []
        errors.append("raw-durations.jsonl# invalid")

    config = documents.get("latency-config", {})
    if isinstance(config, dict):
        try:
            _validate_config(
                config,
                str(config.get("fixture", {}).get("filename", "")),
                str(config.get("queries", {}).get("filename", "")),
            )
        except (LatencyAssayError, AttributeError):
            errors.append("latency-config.json# semantic_invalid")
    summary = documents.get("latency-summary", {})
    audit = documents.get("latency-audit", {})
    _validate_samples(raw_durations, queries, errors)

    if len(raw_durations) == MEASURED_COUNT:
        durations_ms = [record["duration_ns"] / 1_000_000.0 for record in raw_durations]
        expected_p50 = nearest_rank(durations_ms, 50)
        expected_p95 = nearest_rank(durations_ms, 95)
    else:
        expected_p50 = None
        expected_p95 = None
    _validate_summary(summary, expected_p50, expected_p95, errors)
    _validate_audit(audit, errors)
    _validate_cross_bindings(
        manifest,
        config,
        summary,
        audit,
        query_raw,
        queries,
        errors,
    )
    return summary if isinstance(summary, dict) else {}


def _validate_manifest_shape(manifest: dict[str, Any], errors: list[str]) -> None:
    _append_unified_schema_errors(
        manifest,
        "latencyManifest",
        "latency-manifest.json",
        errors,
    )
    expected = {
        "schema_version",
        "latency_assay_version",
        "generated_at",
        "fixture",
        "queries",
        "release_distribution",
        "scope",
        "measurement",
        "files",
        "measurement_complete",
        "qualification_eligible",
        "claimable",
        "nonqualification_reasons",
    }
    if set(manifest) != expected:
        errors.append("latency-manifest.json# shape_invalid")
    if (
        manifest.get("schema_version") != LATENCY_MANIFEST_SCHEMA_VERSION
        or manifest.get("latency_assay_version") != LATENCY_ASSAY_VERSION
        or manifest.get("measurement_complete") is not True
        or manifest.get("qualification_eligible") is not False
        or manifest.get("claimable") is not False
        or manifest.get("nonqualification_reasons") != list(NONQUALIFICATION_REASONS)
    ):
        errors.append("latency-manifest.json# qualification_state_invalid")
    measurement = manifest.get("measurement")
    expected_measurement = {
        "stored_observations": STORED_OBSERVATION_COUNT,
        "warmup_passes": WARMUP_PASSES,
        "warmup_iterations": QUERY_COUNT,
        "measured_passes": MEASURED_PASSES,
        "measured_iterations": MEASURED_COUNT,
        "clock": "time.perf_counter_ns",
        "public_call": "Citefold.recall",
        "response_materialization": "canonical-json-sha256-v1",
    }
    if measurement != expected_measurement:
        errors.append("latency-manifest.json# measurement_contract_invalid")


def _validate_samples(
    samples: list[dict[str, Any]],
    queries: list[dict[str, Any]],
    errors: list[str],
) -> None:
    if len(samples) != MEASURED_COUNT or len(queries) != QUERY_COUNT:
        errors.append("raw-durations.jsonl# record_count_invalid")
        return
    expected_keys = {
        "schema_version",
        "pass_index",
        "query_index",
        "measurement_order",
        "query_id",
        "duration_ns",
        "response_sha256",
    }
    for index, sample in enumerate(samples):
        pass_index = index // QUERY_COUNT + 1
        query_index = index % QUERY_COUNT + 1
        query = queries[query_index - 1]
        if set(sample) != expected_keys:
            errors.append("raw-durations.jsonl# sample_shape_invalid")
            continue
        _append_unified_schema_errors(
            sample,
            "latencySample",
            "raw-durations.jsonl",
            errors,
        )
        duration = sample.get("duration_ns")
        if (
            sample.get("schema_version") != LATENCY_SAMPLE_SCHEMA_VERSION
            or sample.get("pass_index") != pass_index
            or sample.get("query_index") != query_index
            or sample.get("measurement_order") != index + 1
            or sample.get("query_id") != query["query_id"]
            or isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration < 0
            or not _is_sha256(sample.get("response_sha256"))
        ):
            errors.append("raw-durations.jsonl# sample_binding_invalid")


def _validate_summary(
    summary: Any,
    expected_p50: float | None,
    expected_p95: float | None,
    errors: list[str],
) -> None:
    if not isinstance(summary, dict):
        errors.append("latency-summary.json# missing")
        return
    _append_unified_schema_errors(
        summary,
        "latencySummary",
        "latency-summary.json",
        errors,
    )
    expected_keys = set(
        _summary(
            observation_count=STORED_OBSERVATION_COUNT,
            p50_ms=0.0,
            p95_ms=0.0,
            gate_passed=True,
        )
    )
    if set(summary) != expected_keys:
        errors.append("latency-summary.json# shape_invalid")
    fixed = _summary(
        observation_count=STORED_OBSERVATION_COUNT,
        p50_ms=summary.get("p50_ms"),
        p95_ms=summary.get("p95_ms"),
        gate_passed=bool(summary.get("gate_passed")),
    )
    if summary != fixed:
        errors.append("latency-summary.json# fixed_controls_invalid")
    if expected_p50 is None or expected_p95 is None:
        return
    if summary.get("p50_ms") != expected_p50 or summary.get("p95_ms") != expected_p95:
        errors.append("latency-summary.json# percentile_mismatch")
    if summary.get("gate_passed") is not (expected_p95 <= LATENCY_GATE_MS):
        errors.append("latency-summary.json# gate_mismatch")


def _validate_audit(audit: Any, errors: list[str]) -> None:
    if not isinstance(audit, dict):
        errors.append("latency-audit.json# missing")
        return
    _append_unified_schema_errors(
        audit,
        "latencyAudit",
        "latency-audit.json",
        errors,
    )
    expected_keys = {
        "schema_version",
        "latency_assay_version",
        "generated_at",
        "controls",
        "bindings",
        "attestations",
        "measurement_complete",
        "qualification_eligible",
        "claimable",
        "nonqualification_reasons",
    }
    if set(audit) != expected_keys:
        errors.append("latency-audit.json# shape_invalid")
    expected_controls = {
        "fixture_backup_verified": True,
        "fixture_restored_to_disposable_root": True,
        "single_configured_scope": True,
        "all_observations_final": True,
        "partial_voice_observations": 0,
        "public_recall_entrypoint": "Citefold.recall",
        "openrouter_disabled": True,
        "fixed_query_order": True,
        "warmup_passes": WARMUP_PASSES,
        "measured_passes": MEASURED_PASSES,
        "response_materialized_before_clock_end": True,
    }
    expected_attestations = {
        "os_sandbox": "missing",
        "network_isolation": "not_independently_attested",
        "external_process_and_thread_count": "not_independently_attested",
    }
    if (
        audit.get("schema_version") != LATENCY_AUDIT_SCHEMA_VERSION
        or audit.get("latency_assay_version") != LATENCY_ASSAY_VERSION
        or audit.get("controls") != expected_controls
        or audit.get("attestations") != expected_attestations
        or audit.get("measurement_complete") is not True
        or audit.get("qualification_eligible") is not False
        or audit.get("claimable") is not False
        or audit.get("nonqualification_reasons") != list(NONQUALIFICATION_REASONS)
    ):
        errors.append("latency-audit.json# controls_invalid")


def _validate_cross_bindings(
    manifest: dict[str, Any],
    config: dict[str, Any],
    summary: dict[str, Any],
    audit: dict[str, Any],
    query_raw: bytes,
    queries: list[dict[str, Any]],
    errors: list[str],
) -> None:
    fixture = manifest.get("fixture", {})
    query_binding = manifest.get("queries", {})
    release = manifest.get("release_distribution", {})
    bindings = audit.get("bindings", {}) if isinstance(audit, dict) else {}
    if not isinstance(config, dict) or not isinstance(bindings, dict):
        errors.append("latency_bundle# binding_documents_missing")
        return
    config_fixture = config.get("fixture", {})
    config_queries = config.get("queries", {})
    config_release = config.get("release_distribution", {})
    query_sha256 = _sha256(query_raw) if query_raw else None
    order_sha256 = _query_order_sha256(queries) if len(queries) == QUERY_COUNT else None
    if (
        fixture.get("filename") != config_fixture.get("filename")
        or fixture.get("sha256") != config_fixture.get("sha256")
        or fixture.get("sha256") != bindings.get("fixture_sha256")
        or fixture.get("size_bytes") != bindings.get("fixture_size_bytes")
        or fixture.get("backup_fingerprint") != bindings.get("fixture_backup_fingerprint")
        or not _is_sha256(fixture.get("backup_fingerprint"))
    ):
        errors.append("latency_bundle# fixture_binding_mismatch")
    if (
        query_binding.get("sha256") != config_queries.get("sha256")
        or query_binding.get("sha256") != query_sha256
        or query_binding.get("sha256") != bindings.get("queries_sha256")
        or query_binding.get("query_count") != QUERY_COUNT
        or query_binding.get("query_order_sha256") != order_sha256
        or query_binding.get("query_order_sha256") != bindings.get("query_order_sha256")
    ):
        errors.append("latency_bundle# query_binding_mismatch")
    if (
        release.get("filename") != config_release.get("filename")
        or release.get("sha256") != config_release.get("sha256")
        or release.get("sha256") != bindings.get("release_distribution_sha256")
        or release.get("size_bytes") != bindings.get("release_distribution_size_bytes")
    ):
        errors.append("latency_bundle# release_binding_mismatch")
    if manifest.get("scope") != config.get("scope"):
        errors.append("latency_bundle# scope_binding_mismatch")
    if manifest.get("generated_at") != audit.get("generated_at"):
        errors.append("latency_bundle# timestamp_binding_mismatch")
    if (
        manifest.get("measurement_complete") is not summary.get("measurement_complete")
        or manifest.get("qualification_eligible") is not False
        or summary.get("qualification_eligible") is not False
        or audit.get("qualification_eligible") is not False
        or manifest.get("claimable") is not False
        or summary.get("claimable") is not False
        or audit.get("claimable") is not False
    ):
        errors.append("latency_bundle# qualification_state_mismatch")


def _bundle_inventory(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for role, (relative, media_type) in sorted(BUNDLE_FILES.items()):
        raw = _read_bundle_file(
            root,
            relative,
            MAX_RAW_BYTES if relative == "raw-durations.jsonl" else MAX_QUERY_BYTES,
        )
        entries.append(
            {
                "path": relative,
                "role": role,
                "sha256": _sha256(raw),
                "size_bytes": len(raw),
                "media_type": media_type,
            }
        )
    return sorted(entries, key=lambda item: item["path"])


def _require_digest_match(reference: dict[str, Any], actual: str, label: str) -> None:
    if reference.get("sha256") != actual:
        raise LatencyAssayError(f"{label} SHA-256 does not match the frozen config")


def _query_order_sha256(queries: list[dict[str, Any]]) -> str:
    return _sha256(b"\0".join(str(query["query_id"]).encode("utf-8") for query in queries))


def _require_exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise LatencyAssayError(f"{label} has an invalid field set")


def _input_path(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise LatencyAssayError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LatencyAssayError(f"{label} must be a unique regular file")
    return candidate


def _output_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    if os.path.lexists(candidate):
        raise FileExistsError(f"latency output already exists: {candidate.name}")
    return candidate.parent.resolve(strict=False) / candidate.name


def _prepare_output_parent(destination: Path) -> None:
    if os.path.lexists(destination):
        raise FileExistsError(f"latency output already exists: {destination.name}")
    _mkdir_private(destination.parent)


def _bundle_root(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    info = candidate.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LatencyAssayError("latency bundle root must be a real directory")
    return candidate


def _hash_unique_file(path: Path, limit: int, label: str) -> tuple[str, int]:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise LatencyAssayError(f"{label} must be a unique regular file")
    if before.st_size > limit:
        raise LatencyAssayError(f"{label} exceeds its size limit")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise LatencyAssayError(f"{label} changed while opening")
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise LatencyAssayError(f"{label} exceeds its size limit")
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise LatencyAssayError(f"{label} changed while reading")
    return digest.hexdigest(), total


def _read_unique_file(path: Path, limit: int, label: str) -> bytes:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise LatencyAssayError(f"{label} must be a unique regular file")
    if before.st_size > limit:
        raise LatencyAssayError(f"{label} exceeds its size limit")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise LatencyAssayError(f"{label} changed while opening")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > limit:
        raise LatencyAssayError(f"{label} exceeds its size limit")
    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise LatencyAssayError(f"{label} changed while reading")
    return raw


def _read_bundle_file(root: Path, relative: str, limit: int) -> bytes:
    if not _safe_relative_path(relative):
        raise LatencyAssayError("unsafe latency bundle path")
    parts = PurePosixPath(relative).parts
    current = root
    for part in parts[:-1]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise LatencyAssayError("latency bundle parent is unsafe")
    return _read_unique_file(current / parts[-1], limit, "latency bundle artifact")


def _scan_flat_bundle(root: Path, *, exclude: set[str]) -> set[str]:
    result: set[str] = set()
    inodes: set[tuple[int, int]] = set()
    for path in root.iterdir():
        relative = path.name
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LatencyAssayError("latency bundle contains an unsafe filesystem entry")
        inode = (info.st_dev, info.st_ino)
        if inode in inodes:
            raise LatencyAssayError("latency bundle contains a hard-link collision")
        inodes.add(inode)
        if relative not in exclude:
            result.add(relative)
    return result


def _mkdir_private(path: Path) -> None:
    target = path if path.is_absolute() else path.absolute()
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
            raise LatencyAssayError("latency output parent contains an unsafe directory")


def _write_new_file(path: Path, raw: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    value = _decode_json(raw)
    if not isinstance(value, dict):
        raise LatencyAssayError("JSON document root must be an object")
    return value


def _decode_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LatencyAssayError("JSON must be UTF-8") from exc
    if text.startswith("\ufeff") or "\r" in text:
        raise LatencyAssayError("JSON encoding is not canonical")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise LatencyAssayError("JSON is invalid") from exc
    _validate_json_tree(value)
    return value


def _decode_canonical_jsonl(raw: bytes, line_limit: int) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n") or b"\r" in raw or b"\n\n" in raw:
        raise LatencyAssayError("JSONL encoding is not canonical")
    records: list[dict[str, Any]] = []
    for line in raw[:-1].split(b"\n"):
        if len(line) > line_limit:
            raise LatencyAssayError("JSONL record exceeds its byte limit")
        record = _decode_json_object(line)
        if _canonical_json(record).rstrip(b"\n") != line:
            raise LatencyAssayError("JSONL record is not canonically encoded")
        records.append(record)
    return records


@lru_cache(maxsize=None)
def _unified_schema_validator(definition: str) -> Draft202012Validator:
    schema = _decode_json_object(QUALIFICATION_SCHEMA_PATH.read_bytes())
    if definition not in schema.get("$defs", {}):
        raise LatencyAssayError(f"unified qualification schema is missing {definition}")
    return Draft202012Validator(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": f"#/$defs/{definition}",
            "$defs": schema["$defs"],
        }
    )


def _require_unified_schema(value: Any, definition: str, label: str) -> None:
    try:
        error = next(_unified_schema_validator(definition).iter_errors(value), None)
    except (OSError, ValueError, LatencyAssayError) as exc:
        raise LatencyAssayError("unified qualification schema is unavailable") from exc
    if error is not None:
        raise LatencyAssayError(f"{label} failed the unified qualification schema")


def _append_unified_schema_errors(
    value: Any,
    definition: str,
    location: str,
    errors: list[str],
) -> None:
    try:
        invalid = next(_unified_schema_validator(definition).iter_errors(value), None)
    except (OSError, ValueError, LatencyAssayError):
        errors.append(f"{location}# unified_schema_unavailable")
        return
    if invalid is not None:
        errors.append(f"{location}# unified_schema_invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LatencyAssayError("JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise LatencyAssayError(f"non-finite number is forbidden: {value}")


def _validate_json_tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise LatencyAssayError("JSON nesting is too deep")
    if isinstance(value, float) and not math.isfinite(value):
        raise LatencyAssayError("non-finite number is forbidden")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LatencyAssayError("JSON object key is not a string")
            _validate_json_tree(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_tree(item, depth + 1)


def _canonical_json(value: Any, *, pretty: bool = False) -> bytes:
    _validate_json_tree(value)
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    return (text + "\n").encode("utf-8")


def _safe_filename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and SAFE_FILENAME_PATTERN.fullmatch(value) is not None
        and value not in {".", ".."}
        and Path(value).name == value
    )


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    parts = PurePosixPath(value).parts
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or validate the controlled OfficeLifeMemoryBench Track B latency assay."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("fixture_archive", type=Path)
    run_parser.add_argument("query_path", type=Path)
    run_parser.add_argument("config_path", type=Path)
    run_parser.add_argument("output_root", type=Path)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("bundle_root", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "run":
        report = run_latency_assay(
            args.fixture_archive,
            args.query_path,
            args.config_path,
            args.output_root,
        )
    else:
        report = validate_latency_bundle(args.bundle_root)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 4


__all__ = [
    "LATENCY_ASSAY_VERSION",
    "LATENCY_CONFIG_SCHEMA_VERSION",
    "LatencyAssayError",
    "main",
    "nearest_rank",
    "run_latency_assay",
    "validate_latency_bundle",
]


if __name__ == "__main__":
    raise SystemExit(main())
