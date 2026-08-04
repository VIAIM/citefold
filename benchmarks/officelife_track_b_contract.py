from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


CONTRACT_VERSION = "officelife-track-b-artifact-contract-v1"
PROTOCOL_VERSION = "1.0"
EXECUTION_PROFILE_VERSION = "officelife-track-b-execution-profile-v1"

USER_SCHEMA_VERSION = "officelife-track-b-user-v1"
EVENT_SCHEMA_VERSION = "officelife-track-b-event-v1"
TASK_INPUT_SCHEMA_VERSION = "officelife-track-b-task-input-v1"
TASK_LABEL_SCHEMA_VERSION = "officelife-track-b-task-label-v1"
DATASET_MANIFEST_SCHEMA_VERSION = "officelife-track-b-dataset-manifest-v1"
SEALED_RUN_MANIFEST_SCHEMA_VERSION = "officelife-track-b-sealed-run-manifest-v1"
REPORT_SCHEMA_VERSION = "officelife-track-b-contract-preflight-v1"

SCHEMA_DIRECTORY = Path(__file__).with_name("schemas") / "officelife_track_b" / "v1"
SCHEMA_FILES = {
    "common": "common.schema.json",
    "user": "user.schema.json",
    "event": "event.schema.json",
    "task-input": "task-input.schema.json",
    "task-label": "task-label.schema.json",
    "dataset-manifest": "dataset-manifest.schema.json",
    "sealed-run-manifest": "sealed-run-manifest.schema.json",
}
RECORD_SCHEMAS = {
    USER_SCHEMA_VERSION: "user",
    EVENT_SCHEMA_VERSION: "event",
    TASK_INPUT_SCHEMA_VERSION: "task-input",
    TASK_LABEL_SCHEMA_VERSION: "task-label",
}
DATASET_CORE_ROLES = {
    "users": ("users.jsonl", USER_SCHEMA_VERSION, "generator_input"),
    "events": ("events.jsonl", EVENT_SCHEMA_VERSION, "generator_input"),
    "task-inputs": (
        "task-inputs.jsonl",
        TASK_INPUT_SCHEMA_VERSION,
        "generator_input",
    ),
    "task-labels": (
        "task-labels.jsonl",
        TASK_LABEL_SCHEMA_VERSION,
        "custodian_only",
    ),
}
SPLITS = ("development", "validation", "hidden_test")
SURFACES = (
    "text_chat",
    "realtime_voice",
    "third_party_agents",
    "cross_channel",
)
SOURCE_SURFACES = ("text_chat", "realtime_voice", "third_party_agents")
SCENARIO_FAMILIES = (
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
)
HARM_FAMILIES = {
    "no_evidence",
    "stale_or_superseded",
    "correction",
    "deletion",
    "scope_isolation",
}
FORBIDDEN_CHECK_TYPES = {
    "fact": {"fact", "staleness", "deletion"},
    "action": {"action", "staleness", "deletion"},
    "citation": {"citation", "staleness", "deletion"},
    "scope": {"scope"},
}
MEMORY_REQUIREMENTS = ("required", "optional", "absent")
MODEL_ROLES = (
    "reader",
    "observation",
    "asr",
    "vision",
    "consolidation",
    "embedding",
    "secondary_judge",
)
RUN_REQUIRED_ROLES = {
    "citefold-distribution",
    "migration-report",
    "agent-dependency-lock",
    "system-prompt",
    "task-template",
    "memory-pack-placement-template",
    "evaluator-prompt",
    "tool-definitions",
    "tool-schemas",
    "recent-context-builder",
    "qualification-plan",
}
GOVERNANCE_ROLES = {
    "consent_policy": "consent-policy",
    "deidentification_policy": "deidentification-policy",
    "annotation_codebook": "annotation-codebook",
    "prohibited_identifier_scan": "prohibited-identifiers-scan",
    "access_control_policy": "access-control-policy",
    "retention_policy": "retention-policy",
    "withdrawal_policy": "withdrawal-policy",
    "identity_mapping_commitment": "identity-mapping-commitment",
}
SAFE_REPORT_ROLES = (
    set(DATASET_CORE_ROLES)
    | set(GOVERNANCE_ROLES.values())
    | RUN_REQUIRED_ROLES
)
TASK_ARTIFACT_ROLE_PREFIXES = {
    "input_artifact": "task-input-payload-",
    "recent_context_artifact": "recent-context-payload-",
    "tool_fixture_artifact": "tool-fixture-payload-",
}

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_JSONL_BYTES = 256 * 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_ERRORS = 200
ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z", re.ASCII)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9._:/+-]{1,128}\Z", re.ASCII)
UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z\Z",
    re.ASCII,
)
EMPTY_TEXT_FINGERPRINT = "text:<empty>"


class _DuplicateKey(ValueError):
    pass


class _NonFiniteNumber(ValueError):
    pass


class _BundleReadError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def schema_paths() -> dict[str, Path]:
    return {
        name: SCHEMA_DIRECTORY / filename
        for name, filename in SCHEMA_FILES.items()
    }


@lru_cache(maxsize=1)
def _schema_runtime() -> tuple[dict[str, dict[str, Any]], Registry[Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource[Any]]] = []
    for name, path in schema_paths().items():
        raw = path.read_bytes()
        schema = _decode_json_object(raw)
        Draft202012Validator.check_schema(schema)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id.startswith("urn:viaim:"):
            raise ValueError(f"{name} schema must use a trusted urn:viaim: $id")
        schemas[name] = schema
        resources.append((schema_id, Resource.from_contents(schema)))
    known_ids = {schema["$id"] for schema in schemas.values()}
    for name, schema in schemas.items():
        for reference in _walk_schema_references(schema):
            base = reference.split("#", 1)[0]
            if base and base not in known_ids:
                raise ValueError(f"{name} schema contains an unregistered $ref")
    return schemas, Registry().with_resources(resources)


def validate_schema_bundle() -> list[str]:
    try:
        schemas, registry = _schema_runtime()
        for name, schema in schemas.items():
            validator = Draft202012Validator(schema, registry=registry)
            list(validator.iter_errors({}))
    except Exception:
        return ["schemas# invalid_or_unresolvable_schema_bundle"]
    return []


def _walk_schema_references(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                yield item
            else:
                yield from _walk_schema_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_schema_references(item)


@lru_cache(maxsize=1)
def _known_report_fields() -> frozenset[str]:
    schemas, _ = _schema_runtime()
    fields: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                fields.update(str(name) for name in properties)
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for schema in schemas.values():
        collect(schema)
    return frozenset(fields)


class _BundleReader:
    def __init__(self, root: Path, role: str, errors: list[str]) -> None:
        self.root = root
        self.role = role
        self.errors = errors

    def scan_files(self, excluded: set[str]) -> set[str]:
        result: set[str] = set()
        casefolded: dict[str, str] = {}
        inodes: dict[tuple[int, int], str] = {}
        try:
            root_stat = os.lstat(self.root)
        except OSError:
            _add_error(self.errors, "root_unreadable", self.role)
            return result
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            _add_error(self.errors, "root_must_be_real_directory", self.role)
            return result
        for current, directory_names, file_names in os.walk(
            self.root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            for directory_name in list(directory_names):
                directory_path = current_path / directory_name
                try:
                    item_stat = os.lstat(directory_path)
                except OSError:
                    _add_error(self.errors, "directory_unreadable", self.role)
                    directory_names.remove(directory_name)
                    continue
                if stat.S_ISLNK(item_stat.st_mode):
                    _add_error(self.errors, "symlink_forbidden", self.role)
                    directory_names.remove(directory_name)
            for file_name in file_names:
                path = current_path / file_name
                relative = path.relative_to(self.root).as_posix()
                if relative in excluded:
                    continue
                try:
                    item_stat = os.lstat(path)
                except OSError:
                    _add_error(self.errors, "file_unreadable", self.role)
                    continue
                if stat.S_ISLNK(item_stat.st_mode):
                    _add_error(self.errors, "symlink_forbidden", self.role)
                    continue
                if not stat.S_ISREG(item_stat.st_mode):
                    _add_error(self.errors, "non_regular_file_forbidden", self.role)
                    continue
                if item_stat.st_nlink != 1:
                    _add_error(self.errors, "hardlink_forbidden", self.role)
                    continue
                if not _safe_relative_path(relative):
                    _add_error(self.errors, "unsafe_discovered_path", self.role)
                    continue
                folded = relative.casefold()
                if folded in casefolded and casefolded[folded] != relative:
                    _add_error(self.errors, "casefold_path_collision", self.role)
                else:
                    casefolded[folded] = relative
                inode = (item_stat.st_dev, item_stat.st_ino)
                if inode in inodes and inodes[inode] != relative:
                    _add_error(self.errors, "file_identity_collision", self.role)
                else:
                    inodes[inode] = relative
                result.add(relative)
        return result

    def read_bytes(self, relative: str, *, limit: int) -> tuple[bytes, os.stat_result]:
        if not _safe_relative_path(relative):
            raise _BundleReadError("unsafe_relative_path")
        flags = os.O_RDONLY
        directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            root_fd = os.open(self.root, directory_flags | nofollow)
            descriptors.append(root_fd)
            parent_fd = root_fd
            parts = PurePosixPath(relative).parts
            for part in parts[:-1]:
                child_fd = os.open(
                    part,
                    directory_flags | nofollow,
                    dir_fd=parent_fd,
                )
                descriptors.append(child_fd)
                parent_fd = child_fd
            file_fd = os.open(parts[-1], flags | nofollow, dir_fd=parent_fd)
            descriptors.append(file_fd)
            item_stat = os.fstat(file_fd)
            if not stat.S_ISREG(item_stat.st_mode):
                raise _BundleReadError("non_regular_file_forbidden")
            if item_stat.st_nlink != 1:
                raise _BundleReadError("hardlink_forbidden")
            if item_stat.st_size > limit:
                raise _BundleReadError("file_size_limit_exceeded")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(file_fd, min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise _BundleReadError("file_size_limit_exceeded")
            final_stat = os.fstat(file_fd)
            if (
                final_stat.st_dev != item_stat.st_dev
                or final_stat.st_ino != item_stat.st_ino
                or final_stat.st_size != item_stat.st_size
                or final_stat.st_mtime_ns != item_stat.st_mtime_ns
                or final_stat.st_nlink != item_stat.st_nlink
            ):
                raise _BundleReadError("file_changed_during_read")
            return b"".join(chunks), final_stat
        except _BundleReadError:
            raise
        except OSError as exc:
            if exc.errno in {
                errno.ELOOP,
                errno.EMLINK,
            }:
                raise _BundleReadError("symlink_forbidden") from exc
            raise _BundleReadError("file_unreadable") from exc
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    value = _decode_json(raw)
    if not isinstance(value, dict):
        raise ValueError("root_not_object")
    return value


def _decode_json(raw: bytes) -> Any:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("bom_forbidden")
    if b"\r" in raw:
        raise ValueError("cr_forbidden")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid_utf8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (_DuplicateKey, _NonFiniteNumber):
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid_json") from exc
    _validate_json_tree(value)
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate_key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise _NonFiniteNumber("non_finite_number")


def _validate_json_tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("json_depth_limit_exceeded")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("non_string_key")
            _validate_json_tree(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_json_tree(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non_finite_number")


def _load_manifest(
    reader: _BundleReader,
    filename: str,
    role: str,
    errors: list[str],
) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        raw, _ = reader.read_bytes(filename, limit=MAX_MANIFEST_BYTES)
        if not raw.endswith(b"\n"):
            _add_error(errors, "final_newline_required", role)
        return _decode_json_object(raw), raw
    except _DuplicateKey:
        _add_error(errors, "duplicate_json_key", role)
    except _NonFiniteNumber:
        _add_error(errors, "non_finite_number", role)
    except _BundleReadError as exc:
        _add_error(errors, exc.code, role)
    except ValueError as exc:
        _add_error(errors, _safe_parse_code(exc), role)
    return None, None


def _load_jsonl(
    raw: bytes,
    role: str,
    schema_name: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not raw:
        _add_error(errors, "empty_jsonl_forbidden", role)
        return records
    if raw.startswith(b"\xef\xbb\xbf"):
        _add_error(errors, "bom_forbidden", role)
        return records
    if b"\r" in raw:
        _add_error(errors, "cr_forbidden", role)
        return records
    if not raw.endswith(b"\n"):
        _add_error(errors, "final_newline_required", role)
        return records
    for line_number, line in enumerate(raw[:-1].split(b"\n"), start=1):
        location = f"{role}:{line_number}"
        if not line:
            _add_error(errors, "blank_jsonl_line", location)
            continue
        if len(line) > MAX_JSONL_LINE_BYTES:
            _add_error(errors, "jsonl_line_size_limit_exceeded", location)
            continue
        try:
            record = _decode_json_object(line)
        except _DuplicateKey:
            _add_error(errors, "duplicate_json_key", location)
            continue
        except _NonFiniteNumber:
            _add_error(errors, "non_finite_number", location)
            continue
        except ValueError as exc:
            _add_error(errors, _safe_parse_code(exc), location)
            continue
        canonical = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if line != canonical:
            _add_error(errors, "noncanonical_jsonl_record", location)
        if _validate_instance(record, schema_name, location, errors):
            records.append(record)
    return records


def _validate_instance(
    value: Any,
    schema_name: str,
    location: str,
    errors: list[str],
) -> bool:
    try:
        schemas, registry = _schema_runtime()
        validator = Draft202012Validator(schemas[schema_name], registry=registry)
        schema_errors = sorted(
            validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
    except Exception:
        _add_error(errors, "schema_runtime_failure", location)
        return False
    for error in schema_errors:
        pointer = _json_pointer(error.absolute_path)
        _add_error(errors, f"schema_{error.validator}", f"{location}{pointer}")
    _validate_semantic_scalars(value, location, errors)
    return not schema_errors


def _validate_semantic_scalars(
    value: Any,
    location: str,
    errors: list[str],
) -> None:
    timestamp_fields = {
        "occurred_at",
        "available_at",
        "invalidated_at",
        "task_timestamp",
        "history_cutoff",
        "start_inclusive",
        "end_exclusive",
        "frozen_at",
        "sealed_at",
        "event_occurred_at_min",
        "event_occurred_at_max",
        "event_available_at_min",
        "event_available_at_max",
        "task_timestamp_min",
        "task_timestamp_max",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            child = _child_location(location, key)
            if key in timestamp_fields and item is not None and _parse_utc(item) is None:
                _add_error(errors, "invalid_timestamp", child)
            _validate_semantic_scalars(item, child, errors)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_semantic_scalars(
                item,
                _child_location(location, index),
                errors,
            )
    elif isinstance(value, bool):
        return
    elif isinstance(value, int) and abs(value) > (2**63 - 1):
        _add_error(errors, "integer_out_of_range", location)
    elif isinstance(value, float) and not math.isfinite(value):
        _add_error(errors, "non_finite_number", location)


def _json_pointer(parts: Iterable[Any]) -> str:
    encoded = [_pointer_part(part) for part in parts]
    return "#" + ("/" + "/".join(encoded) if encoded else "")


def _child_location(location: str, part: Any) -> str:
    separator = "/" if "#" in location else "#/"
    return f"{location}{separator}{_pointer_part(part)}"


def _safe_parse_code(exc: ValueError) -> str:
    code = str(exc)
    allowed = {
        "bom_forbidden",
        "cr_forbidden",
        "invalid_utf8",
        "invalid_json",
        "json_depth_limit_exceeded",
        "non_string_key",
        "root_not_object",
    }
    return code if code in allowed else "invalid_json"


def _safe_relative_path(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or not value.isascii()
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or value.lower().startswith("file:")
    ):
        return False
    path = PurePosixPath(value)
    parts = path.parts
    if not parts or str(path) != value:
        return False
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if parts[0].endswith(":"):
        return False
    return all(
        bool(re.fullmatch(r"[A-Za-z0-9_.-]+", part, flags=re.ASCII))
        for part in parts
    )


def _add_error(errors: list[str], code: str, location: str) -> None:
    if len(errors) < MAX_ERRORS:
        errors.append(f"{location}# {code}")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalize_text_for_fingerprint(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _normalize_json_for_fingerprint(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_json_for_fingerprint(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_json_for_fingerprint(item) for item in value]
    if isinstance(value, str):
        return _normalize_text_for_fingerprint(value)
    return value


def _artifact_content_fingerprint(
    raw: bytes,
    _artifact_kind: Any,
    _media_type: Any,
) -> str:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return f"binary:{_sha256(raw)}"
    if "\x00" in text:
        return f"binary:{_sha256(raw)}"
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
        _validate_json_tree(value)
    except (
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        _DuplicateKey,
        _NonFiniteNumber,
    ):
        value = None
        parsed_json = False
    else:
        parsed_json = True
    if parsed_json:
        value = _normalize_json_for_fingerprint(value)
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"json:{_sha256(canonical)}"
    normalized_text = _normalize_text_for_fingerprint(text)
    if not normalized_text:
        return EMPTY_TEXT_FINGERPRINT
    normalized = normalized_text.encode("utf-8")
    return f"text:{_sha256(normalized)}"


def _valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value not in {".", ".."}
        and ID_PATTERN.fullmatch(value) is not None
    )


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed


def _scope_key(value: Any) -> tuple[str, str, str, tuple[str, ...]] | None:
    if not isinstance(value, dict):
        return None
    tenant_id = value.get("tenant_id")
    user_id = value.get("user_id")
    namespace = value.get("namespace")
    authorizations = value.get("connected_agent_authorization_ids")
    if (
        not all(_valid_id(item) for item in (tenant_id, user_id, namespace))
        or not isinstance(authorizations, list)
        or any(not _valid_id(item) for item in authorizations)
    ):
        return None
    return (
        str(tenant_id),
        str(user_id),
        str(namespace),
        tuple(sorted(str(item) for item in authorizations)),
    )


def _artifact_ref_key(value: Any) -> tuple[str, str, int] | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    sha256 = value.get("sha256")
    size_bytes = value.get("size_bytes")
    if (
        not _safe_relative_path(path)
        or not _valid_sha256(sha256)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        return None
    return str(path), str(sha256), size_bytes


def _validate_inventory(
    reader: _BundleReader,
    manifest: dict[str, Any],
    *,
    manifest_filename: str,
    bundle_role: str,
    errors: list[str],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, tuple[str, int]],
    dict[str, str],
]:
    inventory = manifest.get("files")
    if not isinstance(inventory, list):
        _add_error(errors, "inventory_missing", f"{bundle_role}-manifest#/files")
        inventory = []
    entries_by_role: dict[str, dict[str, Any]] = {}
    entries_by_path: dict[str, dict[str, Any]] = {}
    casefolded_paths: dict[str, str] = {}
    for index, item in enumerate(inventory):
        location = f"{bundle_role}-manifest#/files/{index}"
        if not isinstance(item, dict):
            _add_error(errors, "inventory_entry_not_object", location)
            continue
        role = item.get("role")
        relative = item.get("path")
        if not _valid_id(role):
            _add_error(errors, "invalid_inventory_role", f"{location}/role")
            continue
        if role in entries_by_role:
            _add_error(errors, "duplicate_inventory_role", f"{location}/role")
        else:
            entries_by_role[str(role)] = item
        if not _safe_relative_path(relative):
            _add_error(errors, "unsafe_relative_path", f"{location}/path")
            continue
        relative = str(relative)
        if relative == manifest_filename:
            _add_error(errors, "manifest_cannot_inventory_itself", f"{location}/path")
        if relative in entries_by_path:
            _add_error(errors, "duplicate_inventory_path", f"{location}/path")
        else:
            entries_by_path[relative] = item
        folded = relative.casefold()
        if folded in casefolded_paths and casefolded_paths[folded] != relative:
            _add_error(errors, "casefold_path_collision", f"{location}/path")
        else:
            casefolded_paths[folded] = relative

    discovered = reader.scan_files({manifest_filename})
    declared = set(entries_by_path)
    for _ in sorted(discovered - declared):
        _add_error(errors, "undeclared_file", bundle_role)
    for _ in sorted(declared - discovered):
        _add_error(errors, "declared_file_missing", bundle_role)

    records_by_role: dict[str, list[dict[str, Any]]] = {}
    actual_by_path: dict[str, tuple[str, int]] = {}
    content_fingerprints_by_path: dict[str, str] = {}
    for relative in sorted(declared & discovered):
        entry = entries_by_path[relative]
        role = entry.get("role")
        if not isinstance(role, str):
            continue
        report_role = _report_role(role, bundle_role)
        artifact_kind = entry.get("artifact_kind")
        limit = MAX_JSONL_BYTES if artifact_kind == "jsonl-records" else MAX_ARTIFACT_BYTES
        try:
            raw, file_stat = reader.read_bytes(relative, limit=limit)
        except _BundleReadError as exc:
            _add_error(errors, exc.code, report_role)
            continue
        digest = _sha256(raw)
        actual_by_path[relative] = (digest, len(raw))
        content_fingerprints_by_path[relative] = _artifact_content_fingerprint(
            raw,
            artifact_kind,
            entry.get("media_type"),
        )
        if entry.get("size_bytes") != len(raw) or file_stat.st_size != len(raw):
            _add_error(errors, "inventory_size_mismatch", report_role)
        if entry.get("sha256") != digest:
            _add_error(errors, "inventory_sha256_mismatch", report_role)
        media_type = entry.get("media_type")
        if artifact_kind != "jsonl-records" and (
            artifact_kind == "json-document"
            or media_type == "application/json"
            or (isinstance(media_type, str) and media_type.endswith("+json"))
        ):
            try:
                document = _decode_json(raw)
                if not isinstance(document, (dict, list)):
                    _add_error(errors, "json_document_root_invalid", report_role)
            except _DuplicateKey:
                _add_error(errors, "duplicate_json_key", report_role)
            except _NonFiniteNumber:
                _add_error(errors, "non_finite_number", report_role)
            except ValueError as exc:
                _add_error(errors, _safe_parse_code(exc), report_role)
        if artifact_kind == "jsonl-records":
            schema_version = entry.get("schema_version")
            schema_name = RECORD_SCHEMAS.get(schema_version)
            records: list[dict[str, Any]] = []
            if role not in DATASET_CORE_ROLES:
                _add_error(errors, "unexpected_jsonl_role", report_role)
            elif schema_name is None:
                _add_error(errors, "unknown_record_schema", report_role)
            else:
                records = _load_jsonl(raw, report_role, schema_name, errors)
            if entry.get("record_count") != len(records):
                _add_error(errors, "inventory_record_count_mismatch", report_role)
            records_by_role[role] = records
        elif entry.get("record_count") != 0:
            _add_error(errors, "non_jsonl_record_count_must_be_zero", report_role)
    return (
        entries_by_role,
        records_by_role,
        actual_by_path,
        content_fingerprints_by_path,
    )


def _canonical_json_content_fingerprint(value: Any) -> str:
    normalized = _normalize_json_for_fingerprint(value)
    raw = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(raw)


def _collect_json_content_signals(
    value: Any,
    subtree_fingerprints: set[str],
    text_values: set[str],
    depth: int = 0,
) -> None:
    if depth > MAX_JSON_DEPTH:
        return
    if isinstance(value, (dict, list)):
        subtree_fingerprints.add(_canonical_json_content_fingerprint(value))
        children = value.values() if isinstance(value, dict) else value
        for item in children:
            _collect_json_content_signals(
                item,
                subtree_fingerprints,
                text_values,
                depth + 1,
            )
    elif isinstance(value, str):
        normalized = _normalize_text_for_fingerprint(value)
        if len(normalized) >= 32:
            text_values.add(normalized)
        try:
            nested = json.loads(
                value,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
            _validate_json_tree(nested)
        except (
            ValueError,
            RecursionError,
            _DuplicateKey,
            _NonFiniteNumber,
        ):
            return
        if isinstance(nested, (dict, list)):
            _collect_json_content_signals(
                nested,
                subtree_fingerprints,
                text_values,
                depth + 1,
            )


def _validate_private_content_separation(
    private_reader: _BundleReader,
    private_manifest: dict[str, Any],
    exposed_reader: _BundleReader,
    exposed_entries_by_role: dict[str, dict[str, Any]],
    errors: list[str],
    *,
    exposure_error_code: str,
    exposure_location: str,
    private_change_code: str,
    exposed_change_code: str,
) -> None:
    private_files = private_manifest.get("files")
    if not isinstance(private_files, list):
        return
    private_entries = [
        entry
        for entry in private_files
        if isinstance(entry, dict)
        and entry.get("access_class") in {"custodian_only", "identity_vault"}
        and _safe_relative_path(entry.get("path"))
    ]
    raw_fragments: set[bytes] = set()
    normalized_text_fragments: set[str] = set()
    private_json_roots: set[str] = set()

    for entry in private_entries:
        path = str(entry["path"])
        limit = (
            MAX_JSONL_BYTES
            if entry.get("artifact_kind") == "jsonl-records"
            else MAX_ARTIFACT_BYTES
        )
        try:
            raw, _ = private_reader.read_bytes(path, limit=limit)
        except _BundleReadError as exc:
            _add_error(errors, exc.code, "dataset-private-file")
            continue
        if entry.get("sha256") != _sha256(raw) or entry.get("size_bytes") != len(raw):
            _add_error(
                errors,
                private_change_code,
                exposure_location,
            )
            continue
        if raw:
            raw_fragments.add(raw)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            text = ""
        normalized_text = _normalize_text_for_fingerprint(text)
        if normalized_text:
            normalized_text_fragments.add(normalized_text)

        json_values: list[Any] = []
        if entry.get("artifact_kind") == "jsonl-records":
            for line in raw.splitlines():
                if line:
                    raw_fragments.add(line)
                try:
                    json_values.append(_decode_json(line))
                except (
                    ValueError,
                    _DuplicateKey,
                    _NonFiniteNumber,
                ):
                    continue
        else:
            try:
                json_values.append(_decode_json(raw))
            except (
                ValueError,
                _DuplicateKey,
                _NonFiniteNumber,
            ):
                pass
        for value in json_values:
            if isinstance(value, (dict, list)):
                private_json_roots.add(
                    _canonical_json_content_fingerprint(value)
                )

    for entry in exposed_entries_by_role.values():
        path = entry.get("path")
        if not _safe_relative_path(path):
            continue
        limit = (
            MAX_JSONL_BYTES
            if entry.get("artifact_kind") == "jsonl-records"
            else MAX_ARTIFACT_BYTES
        )
        try:
            raw, _ = exposed_reader.read_bytes(str(path), limit=limit)
        except _BundleReadError as exc:
            _add_error(errors, exc.code, "run-file")
            continue
        if entry.get("sha256") != _sha256(raw) or entry.get("size_bytes") != len(raw):
            _add_error(
                errors,
                exposed_change_code,
                exposure_location,
            )
            continue
        if any(fragment in raw for fragment in raw_fragments):
            _add_error(
                errors,
                exposure_error_code,
                exposure_location,
            )
            return
        try:
            run_text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            run_text = ""
        normalized_run_text = _normalize_text_for_fingerprint(run_text)
        if any(
            fragment in normalized_run_text
            for fragment in normalized_text_fragments
        ):
            _add_error(
                errors,
                exposure_error_code,
                exposure_location,
            )
            return
        try:
            run_value = _decode_json(raw)
        except (
            ValueError,
            _DuplicateKey,
            _NonFiniteNumber,
        ):
            continue
        run_subtrees: set[str] = set()
        run_text_values: set[str] = set()
        _collect_json_content_signals(
            run_value,
            run_subtrees,
            run_text_values,
        )
        if private_json_roots & run_subtrees or any(
            private_fragment in run_text_value
            for private_fragment in normalized_text_fragments
            for run_text_value in run_text_values
        ):
            _add_error(
                errors,
                exposure_error_code,
                exposure_location,
            )
            return


def _validate_inventory_references(
    value: Any,
    entries_by_role: dict[str, dict[str, Any]],
    entries_by_path: dict[str, dict[str, Any]],
    errors: list[str],
    *,
    location: str,
    ignore_files: bool = True,
) -> None:
    if isinstance(value, dict):
        if ignore_files and location.endswith("#/files"):
            return
        role = value.get("file_role")
        digest = value.get("sha256")
        if isinstance(role, str) and isinstance(digest, str):
            entry = entries_by_role.get(role)
            if entry is None:
                _add_error(errors, "unknown_file_role_reference", location)
            elif entry.get("sha256") != digest:
                _add_error(errors, "file_role_sha256_mismatch", location)
        artifact_ref = _artifact_ref_key(value)
        if artifact_ref is not None:
            path, sha256, size_bytes = artifact_ref
            entry = entries_by_path.get(path)
            if entry is None:
                _add_error(errors, "unknown_artifact_path_reference", location)
            elif (
                entry.get("sha256") != sha256
                or entry.get("size_bytes") != size_bytes
            ):
                _add_error(errors, "artifact_reference_mismatch", location)
        for key, item in value.items():
            child = f"{location}/{_pointer_part(key)}"
            _validate_inventory_references(
                item,
                entries_by_role,
                entries_by_path,
                errors,
                location=child,
                ignore_files=ignore_files,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_inventory_references(
                item,
                entries_by_role,
                entries_by_path,
                errors,
                location=f"{location}/{index}",
                ignore_files=ignore_files,
            )


def _pointer_part(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    field = str(value)
    if field not in _known_report_fields():
        return "<unknown>"
    return field.replace("~", "~0").replace("/", "~1")


def _report_role(role: Any, bundle_role: str) -> str:
    if isinstance(role, str) and role in SAFE_REPORT_ROLES:
        return role
    return f"{bundle_role}-file"


def _validate_dataset_semantics(
    manifest: dict[str, Any],
    entries_by_role: dict[str, dict[str, Any]],
    records_by_role: dict[str, list[dict[str, Any]]],
    content_fingerprints_by_path: dict[str, str],
    errors: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    for role, (expected_path, expected_schema, expected_access) in DATASET_CORE_ROLES.items():
        entry = entries_by_role.get(role)
        if entry is None:
            _add_error(errors, "required_core_role_missing", "dataset-manifest")
            continue
        if entry.get("path") != expected_path:
            _add_error(errors, "core_role_path_mismatch", role)
        if entry.get("schema_version") != expected_schema:
            _add_error(errors, "core_role_schema_mismatch", role)
        if entry.get("access_class") != expected_access:
            _add_error(errors, "core_role_access_class_mismatch", role)
        if entry.get("artifact_kind") != "jsonl-records":
            _add_error(errors, "core_role_kind_mismatch", role)

    entries_by_path = {
        str(entry.get("path")): entry
        for entry in entries_by_role.values()
        if _safe_relative_path(entry.get("path"))
    }
    private_fingerprints = {
        (entry.get("sha256"), entry.get("size_bytes"))
        for entry in entries_by_role.values()
        if entry.get("access_class") in {"custodian_only", "identity_vault"}
    }
    if any(
        entry.get("access_class") in {"generator_input", "executor_input"}
        and (entry.get("sha256"), entry.get("size_bytes")) in private_fingerprints
        for entry in entries_by_role.values()
    ):
        _add_error(
            errors,
            "private_content_exposed_across_access_classes",
            "dataset-manifest#/files",
        )
    _validate_inventory_references(
        manifest.get("governance"),
        entries_by_role,
        entries_by_path,
        errors,
        location="dataset-manifest#/governance",
    )
    governance = manifest.get("governance")
    if isinstance(governance, dict):
        for role_name, expected_file_role in sorted(GOVERNANCE_ROLES.items()):
            reference = governance.get(role_name)
            if not isinstance(reference, dict):
                continue
            file_role = reference.get("file_role")
            entry = entries_by_role.get(str(file_role))
            if file_role != expected_file_role:
                _add_error(errors, "governance_file_role_mismatch", role_name)
            expected_access = (
                "identity_vault"
                if role_name == "identity_mapping_commitment"
                else "governance"
            )
            if entry is not None and entry.get("access_class") != expected_access:
                _add_error(errors, "governance_access_class_mismatch", role_name)

    users = records_by_role.get("users", [])
    events = records_by_role.get("events", [])
    task_inputs = records_by_role.get("task-inputs", [])
    task_labels = records_by_role.get("task-labels", [])

    users_by_id = _unique_records(users, "user_id", "users", errors)
    events_by_id = _unique_records(events, "event_id", "events", errors)
    inputs_by_id = _unique_records(task_inputs, "task_id", "task-inputs", errors)
    labels_by_id = _unique_records(task_labels, "task_id", "task-labels", errors)

    for _ in sorted(set(inputs_by_id) - set(labels_by_id)):
        _add_error(errors, "task_label_missing", "task-pairing")
    for _ in sorted(set(labels_by_id) - set(inputs_by_id)):
        _add_error(errors, "task_input_missing", "task-pairing")

    allowed_scopes: dict[str, set[tuple[str, str, str, tuple[str, ...]]]] = {}
    for user_id, user in users_by_id.items():
        scopes = user.get("allowed_scopes")
        parsed_scopes = {
            parsed
            for item in scopes if (parsed := _scope_key(item)) is not None
        } if isinstance(scopes, list) else set()
        allowed_scopes[user_id] = parsed_scopes
        if any(scope[1] != user_id for scope in parsed_scopes):
            _add_error(errors, "allowed_scope_user_mismatch", "users")

    event_times: dict[str, datetime] = {}
    event_available: dict[str, datetime] = {}
    transport_keys: set[tuple[str, str, str]] = set()
    conversation_owners: dict[
        str,
        tuple[str, Any, tuple[str, str, str, tuple[str, ...]] | None],
    ] = {}
    referenced_generator_paths: set[str] = set()
    referenced_executor_paths: set[str] = set()
    artifact_id_bindings: dict[str, tuple[str, str, int, Any]] = {}

    def bind_artifact_id(reference: Any, location: str) -> None:
        if not isinstance(reference, dict) or not _valid_id(reference.get("artifact_id")):
            return
        key = _artifact_ref_key(reference)
        if key is None:
            return
        artifact_id = str(reference["artifact_id"])
        binding = (*key, reference.get("media_type"))
        prior = artifact_id_bindings.setdefault(artifact_id, binding)
        if prior != binding:
            _add_error(errors, "artifact_id_rebound", location)
    for event_id, event in events_by_id.items():
        user_id = event.get("user_id")
        if user_id not in users_by_id:
            _add_error(errors, "event_user_unknown", "events")
            continue
        scope = _scope_key(event.get("scope"))
        if scope not in allowed_scopes.get(str(user_id), set()):
            _add_error(errors, "event_scope_not_allowed", "events")
        conversation_id = event.get("conversation_id")
        if _valid_id(conversation_id):
            owner = (str(user_id), users_by_id[str(user_id)].get("split"), scope)
            prior_owner = conversation_owners.setdefault(str(conversation_id), owner)
            if prior_owner != owner:
                _add_error(errors, "conversation_crosses_split_or_scope", "events")
        occurred_at = _parse_utc(event.get("occurred_at"))
        available_at = _parse_utc(event.get("available_at"))
        if occurred_at is None:
            _add_error(errors, "invalid_timestamp", "events#/occurred_at")
        else:
            event_times[event_id] = occurred_at
        if available_at is None:
            _add_error(errors, "invalid_timestamp", "events#/available_at")
        else:
            event_available[event_id] = available_at
        if occurred_at is not None and available_at is not None and available_at < occurred_at:
            _add_error(errors, "availability_precedes_occurrence", "events")
        source_surface = event.get("source_surface")
        source_record_id = event.get("source_record_id")
        transport_key = (str(user_id), str(source_surface), str(source_record_id))
        if transport_key in transport_keys:
            _add_error(errors, "duplicate_source_record", "events")
        transport_keys.add(transport_key)
        _validate_event_lifecycle(event, errors)
        payload_references = event.get("payload_refs", [])
        payload_reference_items = (
            payload_references if isinstance(payload_references, list) else []
        )
        for reference in payload_reference_items:
            bind_artifact_id(reference, "events#/payload_refs")
            ref = _artifact_ref_key(reference)
            if ref is None:
                continue
            entry = entries_by_path.get(ref[0])
            if entry is None:
                _add_error(errors, "event_payload_unknown", "events#/payload_refs")
            elif entry.get("access_class") != "generator_input":
                _add_error(errors, "event_payload_not_generator_input", "events#/payload_refs")
            elif not str(entry.get("role", "")).startswith("event-payload-"):
                _add_error(errors, "event_payload_role_invalid", "events#/payload_refs")
            elif not _reference_matches_entry(reference, entry):
                _add_error(errors, "event_payload_reference_mismatch", "events#/payload_refs")
            else:
                referenced_generator_paths.add(ref[0])

    _validate_invalidation_graph(
        events_by_id,
        event_times,
        event_available,
        errors,
    )

    task_cutoffs: dict[str, datetime] = {}
    for task_id, task_input in inputs_by_id.items():
        user_id = task_input.get("user_id")
        user = users_by_id.get(str(user_id))
        if user is None:
            _add_error(errors, "task_user_unknown", "task-inputs")
            continue
        if task_input.get("split") != user.get("split"):
            _add_error(errors, "task_split_mismatch", "task-inputs")
        scope = _scope_key(task_input.get("allowed_scope"))
        if scope not in allowed_scopes.get(str(user_id), set()):
            _add_error(errors, "task_scope_not_allowed", "task-inputs")
        conversation_id = task_input.get("conversation_id")
        if _valid_id(conversation_id):
            owner = (str(user_id), task_input.get("split"), scope)
            prior_owner = conversation_owners.setdefault(str(conversation_id), owner)
            if prior_owner != owner:
                _add_error(errors, "conversation_crosses_split_or_scope", "task-inputs")
        task_timestamp = _parse_utc(task_input.get("task_timestamp"))
        history_cutoff = _parse_utc(task_input.get("history_cutoff"))
        if task_timestamp is None or history_cutoff is None:
            _add_error(errors, "invalid_timestamp", "task-inputs")
        elif history_cutoff > task_timestamp:
            _add_error(errors, "cutoff_after_task_timestamp", "task-inputs")
        else:
            task_cutoffs[task_id] = history_cutoff
        for field_name, expected_role_prefix in TASK_ARTIFACT_ROLE_PREFIXES.items():
            reference = task_input.get(field_name)
            if reference is None:
                continue
            bind_artifact_id(reference, f"task-inputs#/{field_name}")
            ref = _artifact_ref_key(reference)
            if ref is None:
                continue
            entry = entries_by_path.get(ref[0])
            if entry is None:
                _add_error(errors, "task_artifact_unknown", f"task-inputs#/{field_name}")
            elif entry.get("access_class") != "generator_input":
                _add_error(
                    errors,
                    "task_artifact_not_generator_input",
                    f"task-inputs#/{field_name}",
                )
            elif not str(entry.get("role", "")).startswith(expected_role_prefix):
                _add_error(
                    errors,
                    "task_artifact_role_invalid",
                    f"task-inputs#/{field_name}",
                )
            elif not _reference_matches_entry(reference, entry):
                _add_error(
                    errors,
                    "task_artifact_reference_mismatch",
                    f"task-inputs#/{field_name}",
                )
            else:
                referenced_generator_paths.add(ref[0])

        snapshot_reference = task_input.get("snapshot_artifact")
        bind_artifact_id(snapshot_reference, "task-inputs#/snapshot_artifact")
        snapshot_ref = _artifact_ref_key(snapshot_reference)
        if snapshot_ref is not None:
            entry = entries_by_path.get(snapshot_ref[0])
            expected_snapshot_id = f"snapshot-{snapshot_ref[1]}"
            if entry is None:
                _add_error(
                    errors,
                    "snapshot_artifact_unknown",
                    "task-inputs#/snapshot_artifact",
                )
            elif entry.get("access_class") != "executor_input":
                _add_error(
                    errors,
                    "snapshot_artifact_not_executor_input",
                    "task-inputs#/snapshot_artifact",
                )
            elif entry.get("artifact_kind") != "package":
                _add_error(
                    errors,
                    "snapshot_artifact_kind_invalid",
                    "task-inputs#/snapshot_artifact",
                )
            elif entry.get("role") != expected_snapshot_id:
                _add_error(
                    errors,
                    "snapshot_artifact_role_invalid",
                    "task-inputs#/snapshot_artifact",
                )
            elif task_input.get("snapshot_id") != expected_snapshot_id:
                _add_error(
                    errors,
                    "snapshot_id_not_content_addressed",
                    "task-inputs#/snapshot_id",
                )
            elif not isinstance(snapshot_reference, dict) or snapshot_reference.get(
                "artifact_id"
            ) != task_input.get("snapshot_id"):
                _add_error(
                    errors,
                    "snapshot_artifact_id_mismatch",
                    "task-inputs#/snapshot_artifact/artifact_id",
                )
            elif not _reference_matches_entry(snapshot_reference, entry):
                _add_error(
                    errors,
                    "snapshot_artifact_reference_mismatch",
                    "task-inputs#/snapshot_artifact",
                )
            else:
                referenced_executor_paths.add(snapshot_ref[0])

    task_fingerprints: dict[str, tuple[str, ...]] = {}
    for task_id in sorted(inputs_by_id):
        fingerprint = _task_counting_fingerprint(
            inputs_by_id[task_id],
            content_fingerprints_by_path,
        )
        if fingerprint is None:
            continue
        task_fingerprints[task_id] = fingerprint
    if len(_representative_task_ids(inputs_by_id, task_fingerprints)) != len(
        task_fingerprints
    ):
        _add_error(
            errors,
            "duplicate_task_counting_fingerprint",
            "task-inputs",
        )

    for role, entry in entries_by_role.items():
        path = entry.get("path")
        access_class = entry.get("access_class")
        if (
            access_class == "generator_input"
            and role not in DATASET_CORE_ROLES
            and path not in referenced_generator_paths
        ):
            _add_error(errors, "unreferenced_generator_input", "dataset-manifest#/files")
        if access_class == "executor_input" and path not in referenced_executor_paths:
            _add_error(errors, "unreferenced_executor_input", "dataset-manifest#/files")

    for task_id in sorted(set(inputs_by_id) & set(labels_by_id)):
        _validate_task_pair(
            task_id,
            inputs_by_id[task_id],
            labels_by_id[task_id],
            users_by_id,
            events_by_id,
            event_available,
            task_cutoffs,
            errors,
        )

    actual_counts = _calculate_declared_counts(
        users_by_id,
        events_by_id,
        inputs_by_id,
        labels_by_id,
        event_times,
        event_available,
        task_fingerprints,
        content_fingerprints_by_path,
    )
    if manifest.get("declared_counts") != actual_counts:
        _add_error(errors, "declared_counts_mismatch", "dataset-manifest#/declared_counts")
    minimum_gates = _minimum_dataset_gates(
        users_by_id,
        events_by_id,
        inputs_by_id,
        labels_by_id,
        event_times,
        event_available,
        task_fingerprints,
        content_fingerprints_by_path,
    )
    separation = {
        "generator_input_file_count": sum(
            entry.get("access_class") == "generator_input"
            for entry in entries_by_role.values()
        ),
        "custodian_only_file_count": sum(
            entry.get("access_class") == "custodian_only"
            for entry in entries_by_role.values()
        ),
        "task_inputs_and_labels_physically_separate": (
            entries_by_role.get("task-inputs", {}).get("path")
            != entries_by_role.get("task-labels", {}).get("path")
            and entries_by_role.get("task-inputs", {}).get("sha256")
            != entries_by_role.get("task-labels", {}).get("sha256")
            and entries_by_role.get("task-labels", {}).get("access_class")
            == "custodian_only"
        ),
    }
    return minimum_gates, separation


def _unique_records(
    records: list[dict[str, Any]],
    id_field: str,
    role: str,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        identifier = record.get(id_field)
        if not _valid_id(identifier):
            continue
        identifier = str(identifier)
        if identifier in result:
            _add_error(errors, f"duplicate_{id_field}", role)
        else:
            result[identifier] = record
    return result


def _reference_matches_entry(
    reference: dict[str, Any],
    entry: dict[str, Any],
) -> bool:
    return (
        reference.get("path") == entry.get("path")
        and reference.get("sha256") == entry.get("sha256")
        and reference.get("size_bytes") == entry.get("size_bytes")
        and reference.get("media_type") == entry.get("media_type")
    )


def _validate_event_lifecycle(event: dict[str, Any], errors: list[str]) -> None:
    surface = event.get("source_surface")
    lifecycle = event.get("lifecycle_state")
    asr_state = event.get("asr_state")
    memory_bearing = event.get("memory_bearing")
    recallable = event.get("recallable")
    if surface == "realtime_voice":
        if asr_state not in {"partial", "final"}:
            _add_error(errors, "voice_asr_state_invalid", "events#/asr_state")
        if asr_state == "partial" and not (
            lifecycle in {"partial", "tombstoned"}
            and memory_bearing is False
            and recallable is False
        ):
            _add_error(errors, "partial_asr_must_be_ephemeral", "events")
        if asr_state == "final" and lifecycle not in {"finalized", "tombstoned"}:
            _add_error(errors, "final_asr_lifecycle_invalid", "events")
    elif asr_state != "not_applicable":
        _add_error(errors, "non_voice_asr_must_be_not_applicable", "events#/asr_state")
    if lifecycle in {"partial", "tombstoned"} and recallable is not False:
        _add_error(errors, "nonfinal_event_cannot_be_recallable", "events")
    if lifecycle == "finalized" and memory_bearing is True and recallable is not True:
        _add_error(errors, "final_memory_event_must_be_recallable", "events")


def _validate_invalidation_graph(
    events_by_id: dict[str, dict[str, Any]],
    event_times: dict[str, datetime],
    event_available: dict[str, datetime],
    errors: list[str],
) -> None:
    edges: dict[str, str] = {}
    for event_id, event in events_by_id.items():
        invalidating_id = event.get("invalidated_by_event_id")
        if invalidating_id is None:
            continue
        target = events_by_id.get(str(invalidating_id))
        if target is None:
            _add_error(errors, "invalidating_event_unknown", "events")
            continue
        if (
            event.get("user_id") != target.get("user_id")
            or _scope_key(event.get("scope")) != _scope_key(target.get("scope"))
        ):
            _add_error(errors, "invalidation_scope_mismatch", "events")
        source_time = event_times.get(event_id)
        source_available = event_available.get(event_id)
        target_time = event_times.get(str(invalidating_id))
        target_available = event_available.get(str(invalidating_id))
        invalidated_at = _parse_utc(event.get("invalidated_at"))
        if (
            source_time is not None
            and target_time is not None
            and target_time <= source_time
        ):
            _add_error(errors, "invalidating_event_not_later", "events")
        if (
            invalidated_at is None
            or (source_time is not None and invalidated_at < source_time)
            or (source_available is not None and invalidated_at < source_available)
            or (target_time is not None and invalidated_at > target_time)
        ):
            _add_error(errors, "invalidation_timestamp_invalid", "events")
        if target_available is None or (
            target_time is not None and target_available < target_time
        ):
            _add_error(errors, "invalidating_event_availability_invalid", "events")
        if target.get("asr_state") == "partial" or target.get("lifecycle_state") == "partial":
            _add_error(errors, "partial_event_cannot_invalidate", "events")
        edges[event_id] = str(invalidating_id)
    for start in edges:
        seen: set[str] = set()
        current = start
        while current in edges:
            if current in seen:
                _add_error(errors, "invalidation_cycle", "events")
                break
            seen.add(current)
            current = edges[current]


def _validate_task_pair(
    task_id: str,
    task_input: dict[str, Any],
    label: dict[str, Any],
    users_by_id: dict[str, dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    event_available: dict[str, datetime],
    task_cutoffs: dict[str, datetime],
    errors: list[str],
) -> None:
    if label.get("user_id") != task_input.get("user_id"):
        _add_error(errors, "task_label_user_mismatch", "task-pairing")
    if _scope_key(label.get("allowed_scope")) != _scope_key(task_input.get("allowed_scope")):
        _add_error(errors, "task_label_scope_mismatch", "task-pairing")
    memberships = label.get("surface_memberships")
    if isinstance(memberships, list) and task_input.get("execution_surface") not in memberships:
        _add_error(errors, "execution_surface_membership_missing", "task-labels")
    source_surfaces = label.get("source_surfaces")
    if isinstance(memberships, list) and "cross_channel" in memberships:
        if not isinstance(source_surfaces, list) or all(
            surface == task_input.get("execution_surface") for surface in source_surfaces
        ):
            _add_error(errors, "cross_channel_membership_invalid", "task-labels")

    reference_fields = (
        "relevant_source_event_ids",
        "superseded_event_ids",
        "deleted_event_ids",
    )
    reference_sets = {
        field_name: set(label.get(field_name, []))
        if isinstance(label.get(field_name), list)
        else set()
        for field_name in reference_fields
    }
    for left_index, left_name in enumerate(reference_fields):
        for right_name in reference_fields[left_index + 1 :]:
            if reference_sets[left_name] & reference_sets[right_name]:
                _add_error(errors, "event_reference_sets_overlap", "task-labels")
    cutoff = task_cutoffs.get(task_id)
    expected_user = task_input.get("user_id")
    expected_scope = _scope_key(task_input.get("allowed_scope"))
    referenced_surfaces: set[str] = set()
    for field_name, identifiers in reference_sets.items():
        for event_id in identifiers:
            event = events_by_id.get(str(event_id))
            if event is None:
                _add_error(errors, "task_event_reference_unknown", f"task-labels#/{field_name}")
                continue
            if (
                event.get("user_id") != expected_user
                or _scope_key(event.get("scope")) != expected_scope
            ):
                _add_error(errors, "task_event_scope_mismatch", f"task-labels#/{field_name}")
            available_at = event_available.get(str(event_id))
            if cutoff is None or available_at is None or available_at >= cutoff:
                _add_error(errors, "task_event_not_available_before_cutoff", f"task-labels#/{field_name}")
            if event.get("asr_state") == "partial" or event.get("lifecycle_state") == "partial":
                _add_error(errors, "partial_event_reference_forbidden", f"task-labels#/{field_name}")
            invalidation_effective = _invalidation_effective_before_cutoff(
                event,
                cutoff,
                events_by_id,
                event_available,
            )
            if field_name == "relevant_source_event_ids" and not _event_eligible_at_cutoff(
                event,
                cutoff,
                events_by_id,
                event_available,
            ):
                _add_error(
                    errors,
                    "relevant_event_not_eligible",
                    "task-labels#/relevant_source_event_ids",
                )
            if field_name == "superseded_event_ids":
                if event.get("invalidation_reason") not in {
                    "superseded",
                    "corrected",
                }:
                    _add_error(errors, "superseded_event_reason_mismatch", "task-labels")
                if not invalidation_effective:
                    _add_error(errors, "supersession_not_effective_before_cutoff", "task-labels")
            if field_name == "deleted_event_ids":
                if event.get("invalidation_reason") != "deleted":
                    _add_error(errors, "deleted_event_reason_mismatch", "task-labels")
                if not invalidation_effective:
                    _add_error(errors, "deletion_not_effective_before_cutoff", "task-labels")
            surface = event.get("source_surface")
            if isinstance(surface, str):
                referenced_surfaces.add(surface)
    declared_source_surfaces = (
        set(source_surfaces) if isinstance(source_surfaces, list) else set()
    )
    if declared_source_surfaces != referenced_surfaces:
        _add_error(errors, "source_surface_reference_mismatch", "task-labels")
    execution_surface = task_input.get("execution_surface")
    expected_memberships = set(referenced_surfaces)
    if isinstance(execution_surface, str):
        expected_memberships.add(execution_surface)
    is_cross_channel = any(
        surface != execution_surface for surface in referenced_surfaces
    )
    if is_cross_channel:
        expected_memberships.add("cross_channel")
    declared_memberships = set(memberships) if isinstance(memberships, list) else set()
    if declared_memberships != expected_memberships:
        _add_error(errors, "surface_membership_not_derived", "task-labels")
    if (
        label.get("memory_requirement") == "absent"
        and (
            reference_sets["relevant_source_event_ids"]
            or declared_source_surfaces
        )
    ):
        _add_error(errors, "absent_memory_cannot_have_relevant_events", "task-labels")
    _validate_label_closure(
        label,
        reference_sets["relevant_source_event_ids"],
        _parse_utc(task_input.get("task_timestamp")),
        errors,
    )
    _validate_scenario_family(
        label,
        reference_sets,
        events_by_id,
        is_cross_channel,
        errors,
    )


def _validate_scenario_family(
    label: dict[str, Any],
    reference_sets: dict[str, set[Any]],
    events_by_id: dict[str, dict[str, Any]],
    is_cross_channel: bool,
    errors: list[str],
) -> None:
    family = label.get("scenario_family")
    checks = [
        item
        for item in label.get("deterministic_checks", [])
        if isinstance(item, dict)
    ]
    forbidden_items = label.get("forbidden_items")
    forbidden_by_id = {
        str(item.get("forbidden_id")): item
        for item in forbidden_items
        if isinstance(item, dict) and _valid_id(item.get("forbidden_id"))
    } if isinstance(forbidden_items, list) else {}

    def has_typed_forbidden_check(
        check_type: str | None,
        allowed_kinds: set[str],
    ) -> bool:
        for item in checks:
            forbidden_id = item.get("subject_ref")
            forbidden = forbidden_by_id.get(str(forbidden_id))
            if (
                forbidden is None
                or forbidden.get("kind") not in allowed_kinds
                or item.get("subject_kind") != "forbidden_item"
                or item.get("hard_prohibition") is not True
                or item.get("operator") not in {"absent", "not_equals"}
            ):
                continue
            expected_type = check_type or forbidden.get("kind")
            if item.get("type") != expected_type:
                continue
            expected_values = item.get("expected_values")
            expected_strings: set[str] = set()
            for value in expected_values if isinstance(expected_values, list) else []:
                if not isinstance(value, dict):
                    continue
                canonical = value.get("canonical")
                if isinstance(canonical, str):
                    expected_strings.add(canonical)
                alternatives = value.get("alternatives")
                if isinstance(alternatives, list):
                    expected_strings.update(
                        candidate
                        for candidate in alternatives
                        if isinstance(candidate, str)
                    )
            if forbidden.get("canonical") in expected_strings:
                return True
        return False

    if family == "no_evidence":
        if (
            label.get("memory_requirement") != "absent"
            or any(reference_sets.values())
            or not has_typed_forbidden_check(
                None,
                {"fact", "action", "citation"},
            )
        ):
            _add_error(errors, "no_evidence_family_unsubstantiated", "task-labels")
    elif family == "stale_or_superseded":
        if not reference_sets["superseded_event_ids"] or not has_typed_forbidden_check(
            "staleness",
            {"fact", "action", "citation"},
        ):
            _add_error(errors, "stale_family_unsubstantiated", "task-labels")
    elif family == "correction":
        corrected = any(
            events_by_id.get(str(event_id), {}).get("invalidation_reason")
            == "corrected"
            for event_id in reference_sets["superseded_event_ids"]
        )
        if not corrected or not has_typed_forbidden_check(
            "staleness",
            {"fact", "action", "citation"},
        ):
            _add_error(errors, "correction_family_unsubstantiated", "task-labels")
    elif family == "deletion":
        if not reference_sets["deleted_event_ids"] or not has_typed_forbidden_check(
            "deletion",
            {"fact", "action", "citation"},
        ):
            _add_error(errors, "deletion_family_unsubstantiated", "task-labels")
    elif family == "scope_isolation":
        if not has_typed_forbidden_check("scope", {"scope"}):
            _add_error(errors, "scope_family_unsubstantiated", "task-labels")
    elif family == "cross_channel":
        if not is_cross_channel:
            _add_error(errors, "cross_channel_family_unsubstantiated", "task-labels")

    if family in {
        "stable_preferences",
        "open_loops",
        "people_followup",
        "meeting_decisions",
        "cross_channel",
    } and not reference_sets["relevant_source_event_ids"]:
        _add_error(errors, "memory_family_missing_relevant_event", "task-labels")


def _validate_label_closure(
    label: dict[str, Any],
    relevant_event_ids: set[Any],
    evaluation_at: datetime | None,
    errors: list[str],
) -> None:
    facts = label.get("acceptable_facts")
    fact_ids = {
        str(item.get("fact_id"))
        for item in facts
        if isinstance(item, dict) and _valid_id(item.get("fact_id"))
    } if isinstance(facts, list) else set()
    if isinstance(facts, list) and len(fact_ids) != len(facts):
        _add_error(errors, "duplicate_fact_id", "task-labels#/acceptable_facts")
    fact_importance = {
        str(item.get("fact_id")): item.get("importance")
        for item in facts
        if isinstance(item, dict) and _valid_id(item.get("fact_id"))
    } if isinstance(facts, list) else {}
    facts_by_id = {
        str(item.get("fact_id")): item
        for item in facts
        if isinstance(item, dict) and _valid_id(item.get("fact_id"))
    } if isinstance(facts, list) else {}
    inference_id_values: list[str] = []
    for fact in facts if isinstance(facts, list) else []:
        if not isinstance(fact, dict):
            continue
        inference_id_values.extend(
            str(item)
            for item in fact.get("allowed_inference_ids", [])
            if _valid_id(item)
        )
        valid_interval = fact.get("valid_interval")
        if isinstance(valid_interval, dict):
            start = (
                _parse_utc(valid_interval.get("start_inclusive"))
                if valid_interval.get("start_inclusive") is not None
                else None
            )
            end = (
                _parse_utc(valid_interval.get("end_exclusive"))
                if valid_interval.get("end_exclusive") is not None
                else None
            )
            if start is not None and end is not None and start >= end:
                _add_error(
                    errors,
                    "fact_valid_interval_empty",
                    "task-labels#/acceptable_facts",
                )
            if evaluation_at is not None and (
                (start is not None and evaluation_at < start)
                or (end is not None and evaluation_at >= end)
            ):
                _add_error(
                    errors,
                    "fact_not_valid_at_task_time",
                    "task-labels#/acceptable_facts",
                )
        evidence_sets = fact.get("acceptable_evidence_sets", [])
        if fact.get("source_support_required") is True and not evidence_sets:
            _add_error(
                errors,
                "source_support_requires_evidence_set",
                "task-labels#/acceptable_facts",
            )
        for evidence_set in evidence_sets:
            if isinstance(evidence_set, list) and not evidence_set:
                _add_error(errors, "empty_evidence_set", "task-labels#/acceptable_facts")
            if isinstance(evidence_set, list) and any(
                event_id not in relevant_event_ids for event_id in evidence_set
            ):
                _add_error(
                    errors,
                    "fact_evidence_event_unknown",
                    "task-labels#/acceptable_facts",
                )
    inference_ids = set(inference_id_values)
    if len(inference_ids) != len(inference_id_values):
        _add_error(
            errors,
            "duplicate_inference_id",
            "task-labels#/acceptable_facts",
        )
    answer_sets = label.get("acceptable_answer_sets")
    answer_set_ids = {
        str(item.get("set_id"))
        for item in answer_sets
        if isinstance(item, dict) and _valid_id(item.get("set_id"))
    } if isinstance(answer_sets, list) else set()
    if isinstance(answer_sets, list) and len(answer_set_ids) != len(answer_sets):
        _add_error(errors, "duplicate_answer_set_id", "task-labels#/acceptable_answer_sets")
    globally_required_facts = {
        fact_id for fact_id, importance in fact_importance.items() if importance == "required"
    }
    for answer_set in answer_sets if isinstance(answer_sets, list) else []:
        if not isinstance(answer_set, dict):
            continue
        required_fact_ids = list(answer_set.get("required_fact_ids", []))
        optional_fact_ids = list(answer_set.get("optional_fact_ids", []))
        answer_fact_ids = required_fact_ids + optional_fact_ids
        if any(fact_id not in fact_ids for fact_id in answer_fact_ids):
            _add_error(errors, "answer_set_fact_unknown", "task-labels#/acceptable_answer_sets")
        if set(required_fact_ids) & set(optional_fact_ids):
            _add_error(errors, "answer_set_fact_overlap", "task-labels#/acceptable_answer_sets")
        if not answer_fact_ids and label.get("memory_requirement") != "absent":
            _add_error(errors, "empty_answer_set", "task-labels#/acceptable_answer_sets")
        if any(fact_importance.get(fact_id) != "required" for fact_id in required_fact_ids):
            _add_error(
                errors,
                "required_answer_fact_importance_mismatch",
                "task-labels#/acceptable_answer_sets",
            )
        if any(fact_importance.get(fact_id) != "optional" for fact_id in optional_fact_ids):
            _add_error(
                errors,
                "optional_answer_fact_importance_mismatch",
                "task-labels#/acceptable_answer_sets",
            )
        if not globally_required_facts.issubset(set(required_fact_ids)):
            _add_error(
                errors,
                "required_fact_omitted_from_answer_set",
                "task-labels#/acceptable_answer_sets",
            )
    checks = label.get("deterministic_checks")
    check_ids = {
        str(item.get("check_id"))
        for item in checks
        if isinstance(item, dict) and _valid_id(item.get("check_id"))
    } if isinstance(checks, list) else set()
    if isinstance(checks, list) and len(check_ids) != len(checks):
        _add_error(errors, "duplicate_check_id", "task-labels#/deterministic_checks")
    forbidden_items = label.get("forbidden_items")
    forbidden_ids = {
        str(item.get("forbidden_id"))
        for item in forbidden_items
        if isinstance(item, dict) and _valid_id(item.get("forbidden_id"))
    } if isinstance(forbidden_items, list) else set()
    if isinstance(forbidden_items, list) and len(forbidden_ids) != len(forbidden_items):
        _add_error(errors, "duplicate_forbidden_id", "task-labels#/forbidden_items")
    forbidden_by_id = {
        str(item.get("forbidden_id")): item
        for item in forbidden_items
        if isinstance(item, dict) and _valid_id(item.get("forbidden_id"))
    } if isinstance(forbidden_items, list) else {}
    checks_by_id = {
        str(item.get("check_id")): item
        for item in checks
        if isinstance(item, dict) and _valid_id(item.get("check_id"))
    } if isinstance(checks, list) else {}
    non_memory_id_values = [
        str(item)
        for item in label.get("allowed_non_memory_evidence_refs", [])
        if _valid_id(item)
    ]
    non_memory_ids = set(non_memory_id_values)
    if len(non_memory_ids) != len(non_memory_id_values):
        _add_error(
            errors,
            "duplicate_non_memory_evidence_id",
            "task-labels#/allowed_non_memory_evidence_refs",
        )
    subject_namespaces = (
        fact_ids,
        forbidden_ids,
        inference_ids,
        non_memory_ids,
    )
    if any(
        left & right
        for index, left in enumerate(subject_namespaces)
        for right in subject_namespaces[index + 1 :]
    ):
        _add_error(
            errors,
            "subject_namespace_collision",
            "task-labels",
        )
    allowed_subject_ids = (
        fact_ids
        | forbidden_ids
        | inference_ids
        | non_memory_ids
    )
    if any(
        item.get("subject_ref") not in allowed_subject_ids
        for item in checks_by_id.values()
    ):
        _add_error(errors, "check_subject_unknown", "task-labels#/deterministic_checks")
    for item in checks_by_id.values():
        subject_ref = str(item.get("subject_ref"))
        expected_subject_kind = None
        if subject_ref in fact_ids:
            expected_subject_kind = "acceptable_fact"
        elif subject_ref in forbidden_ids:
            expected_subject_kind = "forbidden_item"
        elif subject_ref in inference_ids:
            expected_subject_kind = "inference"
        elif subject_ref in non_memory_ids:
            expected_subject_kind = "non_memory_evidence"
        if (
            expected_subject_kind is not None
            and item.get("subject_kind") != expected_subject_kind
        ):
            _add_error(
                errors,
                "check_subject_kind_mismatch",
                "task-labels#/deterministic_checks",
            )
        if expected_subject_kind == "acceptable_fact" and item.get("type") not in {
            "fact",
            "citation",
        }:
            _add_error(
                errors,
                "fact_check_type_incompatible",
                "task-labels#/deterministic_checks",
            )
        if expected_subject_kind == "forbidden_item":
            forbidden_kind = forbidden_by_id.get(subject_ref, {}).get("kind")
            if item.get("type") not in FORBIDDEN_CHECK_TYPES.get(
                str(forbidden_kind),
                set(),
            ):
                _add_error(
                    errors,
                    "forbidden_check_type_incompatible",
                    "task-labels#/deterministic_checks",
                )

    def canonical_value_keys(values: Any) -> set[str]:
        if not isinstance(values, list):
            return set()
        return {
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for value in values
            if isinstance(value, dict)
        }

    def compatible_required_fact_check(item: dict[str, Any], fact_id: str) -> bool:
        expected_values = canonical_value_keys(item.get("expected_values"))
        acceptable_values = canonical_value_keys(
            facts_by_id.get(fact_id, {}).get("acceptable_values")
        )
        return (
            item.get("subject_kind") == "acceptable_fact"
            and item.get("subject_ref") == fact_id
            and item.get("type") == "fact"
            and item.get("operator") in {"present", "equals"}
            and bool(expected_values)
            and expected_values.issubset(acceptable_values)
        )

    def compatible_hard_forbidden_check(
        item: dict[str, Any],
        forbidden_id: str,
    ) -> bool:
        forbidden = forbidden_by_id.get(forbidden_id, {})
        expected_values = item.get("expected_values")
        expected_strings: set[str] = set()
        for value in expected_values if isinstance(expected_values, list) else []:
            if not isinstance(value, dict):
                continue
            canonical = value.get("canonical")
            if isinstance(canonical, str):
                expected_strings.add(canonical)
            alternatives = value.get("alternatives")
            if isinstance(alternatives, list):
                expected_strings.update(
                    candidate
                    for candidate in alternatives
                    if isinstance(candidate, str)
                )
        return (
            item.get("subject_kind") == "forbidden_item"
            and item.get("subject_ref") == forbidden_id
            and item.get("type")
            in FORBIDDEN_CHECK_TYPES.get(str(forbidden.get("kind")), set())
            and item.get("operator") in {"absent", "not_equals"}
            and forbidden.get("canonical") in expected_strings
        )

    success_rule = label.get("success_rule")
    if isinstance(success_rule, dict):
        for field_name in ("required_check_ids", "hard_prohibition_check_ids"):
            values = success_rule.get(field_name)
            if isinstance(values, list) and any(value not in check_ids for value in values):
                _add_error(errors, "success_rule_check_unknown", f"task-labels#/success_rule/{field_name}")
        required_check_ids = set(success_rule.get("required_check_ids", []))
        expected_required_check_ids = {
            check_id
            for check_id, item in checks_by_id.items()
            if item.get("must_pass") is True
        }
        if required_check_ids != expected_required_check_ids or not required_check_ids:
            _add_error(
                errors,
                "required_check_closure_mismatch",
                "task-labels#/success_rule/required_check_ids",
            )
        hard_check_ids = set(success_rule.get("hard_prohibition_check_ids", []))
        expected_hard_check_ids = {
            check_id
            for check_id, item in checks_by_id.items()
            if item.get("hard_prohibition") is True
        }
        if hard_check_ids != expected_hard_check_ids or not hard_check_ids:
            _add_error(
                errors,
                "hard_prohibition_check_closure_mismatch",
                "task-labels#/success_rule/hard_prohibition_check_ids",
            )
        hard_forbidden_ids = {
            str(item.get("forbidden_id"))
            for item in forbidden_items
            if isinstance(item, dict)
            and item.get("hard_prohibition") is True
            and _valid_id(item.get("forbidden_id"))
        } if isinstance(forbidden_items, list) else set()
        covered_hard_subjects = {
            forbidden_id
            for forbidden_id in hard_forbidden_ids
            if any(
                item.get("hard_prohibition") is True
                and compatible_hard_forbidden_check(item, forbidden_id)
                for item in checks_by_id.values()
            )
        }
        if not hard_forbidden_ids.issubset(covered_hard_subjects):
            _add_error(
                errors,
                "hard_forbidden_item_unchecked",
                "task-labels#/forbidden_items",
            )
        checked_required_facts = {
            fact_id
            for fact_id in globally_required_facts
            if any(
                item.get("must_pass") is True
                and compatible_required_fact_check(item, fact_id)
                for item in checks_by_id.values()
            )
        }
        if not globally_required_facts.issubset(checked_required_facts):
            _add_error(
                errors,
                "required_fact_unchecked",
                "task-labels#/acceptable_facts",
            )
        if success_rule.get("human_judgment_required") != label.get(
            "human_judgment_required"
        ):
            _add_error(
                errors,
                "human_judgment_rule_mismatch",
                "task-labels#/success_rule/human_judgment_required",
            )


def _invalidation_effective_before_cutoff(
    event: dict[str, Any],
    cutoff: datetime | None,
    events_by_id: dict[str, dict[str, Any]],
    event_available: dict[str, datetime],
) -> bool:
    if cutoff is None:
        return False
    invalidating_id = event.get("invalidated_by_event_id")
    invalidated_at = _parse_utc(event.get("invalidated_at"))
    if not isinstance(invalidating_id, str) or invalidating_id not in events_by_id:
        return False
    invalidating_available = event_available.get(invalidating_id)
    return (
        invalidated_at is not None
        and invalidating_available is not None
        and invalidated_at < cutoff
        and invalidating_available < cutoff
    )


def _event_eligible_at_cutoff(
    event: dict[str, Any],
    cutoff: datetime | None,
    events_by_id: dict[str, dict[str, Any]],
    event_available: dict[str, datetime],
) -> bool:
    event_id = event.get("event_id")
    available_at = event_available.get(str(event_id))
    if (
        cutoff is None
        or available_at is None
        or available_at >= cutoff
        or event.get("memory_bearing") is not True
        or event.get("asr_state") == "partial"
        or event.get("lifecycle_state") == "partial"
    ):
        return False
    if event.get("lifecycle_state") == "tombstoned" and not isinstance(
        event.get("invalidated_by_event_id"),
        str,
    ):
        return False
    return not _invalidation_effective_before_cutoff(
        event,
        cutoff,
        events_by_id,
        event_available,
    )


def _artifact_reference_content_fingerprint(
    reference: Any,
    content_fingerprints_by_path: dict[str, str],
) -> str | None:
    key = _artifact_ref_key(reference)
    if key is None:
        return None
    path, digest, _ = key
    return content_fingerprints_by_path.get(path, f"binary:{digest}")


def _event_payload_fingerprint(
    event: dict[str, Any],
    content_fingerprints_by_path: dict[str, str],
) -> tuple[str, ...]:
    references = event.get("payload_refs")
    if not isinstance(references, list):
        return ()
    return tuple(
        sorted(
            {
                fingerprint
                for reference in references
                if (
                    fingerprint := _artifact_reference_content_fingerprint(
                        reference,
                        content_fingerprints_by_path,
                    )
                )
                and fingerprint != EMPTY_TEXT_FINGERPRINT
            }
        )
    )


def _eligible_event_component_times_by_user(
    events_by_id: dict[str, dict[str, Any]],
    event_times: dict[str, datetime],
    event_available: dict[str, datetime],
    task_cutoffs_by_user: dict[str, list[datetime]],
    eligible_user_ids: set[str],
    content_fingerprints_by_path: dict[str, str],
) -> dict[str, list[datetime]]:
    candidates: dict[str, list[tuple[datetime, tuple[str, ...]]]] = defaultdict(list)
    for event_id, event in events_by_id.items():
        user_id = str(event.get("user_id"))
        occurred_at = event_times.get(event_id)
        if user_id not in eligible_user_ids or occurred_at is None:
            continue
        if not any(
            _event_eligible_at_cutoff(
                event,
                cutoff,
                events_by_id,
                event_available,
            )
            for cutoff in task_cutoffs_by_user.get(user_id, [])
        ):
            continue
        fingerprints = _event_payload_fingerprint(
            event,
            content_fingerprints_by_path,
        )
        if fingerprints:
            candidates[user_id].append((occurred_at, fingerprints))

    result: dict[str, list[datetime]] = defaultdict(list)
    for user_id, items in candidates.items():
        parents = list(range(len(items)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        owner_by_fingerprint: dict[str, int] = {}
        for index, (_, fingerprints) in enumerate(items):
            for fingerprint in fingerprints:
                prior = owner_by_fingerprint.setdefault(fingerprint, index)
                union(index, prior)

        earliest_by_component: dict[int, datetime] = {}
        for index, (occurred_at, _) in enumerate(items):
            component = find(index)
            prior = earliest_by_component.get(component)
            earliest_by_component[component] = (
                occurred_at if prior is None else min(prior, occurred_at)
            )
        result[user_id] = sorted(earliest_by_component.values())
    return result


def _task_counting_fingerprint(
    task: dict[str, Any],
    content_fingerprints_by_path: dict[str, str],
) -> tuple[str, ...] | None:
    scope = _scope_key(task.get("allowed_scope"))
    fields = (task.get("user_id"),)
    if scope is None or any(not isinstance(item, str) for item in fields):
        return None
    snapshot_fingerprint = _artifact_reference_content_fingerprint(
        task.get("snapshot_artifact"),
        content_fingerprints_by_path,
    )
    material_fingerprints = [
        _artifact_reference_content_fingerprint(
            task.get(field_name),
            content_fingerprints_by_path,
        )
        for field_name in (
            "input_artifact",
            "recent_context_artifact",
            "tool_fixture_artifact",
        )
    ]
    if snapshot_fingerprint is None or any(
        fingerprint is None for fingerprint in material_fingerprints
    ):
        return None
    scope_value = json.dumps(scope, ensure_ascii=True, separators=(",", ":"))
    identity = tuple(str(item) for item in fields) + (scope_value,)
    material_value = json.dumps(
        [*identity, *material_fingerprints],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    snapshot_value = json.dumps(
        [*identity, snapshot_fingerprint],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return (
        f"task-material:{_sha256(material_value.encode('utf-8'))}",
        f"task-snapshot:{_sha256(snapshot_value.encode('utf-8'))}",
    )


def _representative_task_ids(
    inputs_by_id: dict[str, dict[str, Any]],
    task_fingerprints: dict[str, tuple[str, ...]],
) -> list[str]:
    task_ids = [
        task_id
        for task_id in sorted(inputs_by_id)
        if task_id in task_fingerprints
    ]
    parents = list(range(len(task_ids)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    owner_by_fingerprint: dict[str, int] = {}
    for index, task_id in enumerate(task_ids):
        for fingerprint in task_fingerprints[task_id]:
            prior = owner_by_fingerprint.setdefault(fingerprint, index)
            union(index, prior)

    return [
        task_id
        for index, task_id in enumerate(task_ids)
        if find(index) == index
    ]


def _calculate_declared_counts(
    users_by_id: dict[str, dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    inputs_by_id: dict[str, dict[str, Any]],
    labels_by_id: dict[str, dict[str, Any]],
    event_times: dict[str, datetime],
    event_available: dict[str, datetime],
    task_fingerprints: dict[str, tuple[str, ...]],
    content_fingerprints_by_path: dict[str, str],
) -> dict[str, Any]:
    users_by_split = Counter(
        user.get("split") for user in users_by_id.values() if user.get("split") in SPLITS
    )
    representative_task_ids = set(
        _representative_task_ids(inputs_by_id, task_fingerprints)
    )
    task_splits = {
        task_id: task.get("split")
        for task_id, task in inputs_by_id.items()
        if task_id in representative_task_ids and task.get("split") in SPLITS
    }
    unique_tasks_by_split = Counter(task_splits.values())
    surfaces = {split: Counter() for split in SPLITS}
    families = {split: Counter() for split in SPLITS}
    requirements = {split: Counter() for split in SPLITS}
    harm = Counter()
    for task_id, label in labels_by_id.items():
        split = task_splits.get(task_id)
        if split not in SPLITS:
            continue
        for surface in set(label.get("surface_memberships", [])):
            if surface in SURFACES:
                surfaces[split][surface] += 1
        family = label.get("scenario_family")
        if family in SCENARIO_FAMILIES:
            families[split][family] += 1
        requirement = label.get("memory_requirement")
        if requirement in MEMORY_REQUIREMENTS:
            requirements[split][requirement] += 1
        if family in HARM_FAMILIES:
            harm[split] += 1
    occurred_times: dict[str, list[datetime]] = defaultdict(list)
    available_times: dict[str, list[datetime]] = defaultdict(list)
    task_times: dict[str, list[datetime]] = defaultdict(list)
    task_cutoffs_by_user: dict[str, list[datetime]] = defaultdict(list)
    task_counts_by_user = Counter()
    for task_id, task in inputs_by_id.items():
        if task_id not in representative_task_ids:
            continue
        split = task.get("split")
        task_time = _parse_utc(task.get("task_timestamp"))
        cutoff = _parse_utc(task.get("history_cutoff"))
        user_id = str(task.get("user_id"))
        if split in SPLITS and task_time is not None:
            task_times[str(split)].append(task_time)
        if split == "hidden_test":
            task_counts_by_user[user_id] += 1
            if cutoff is not None:
                task_cutoffs_by_user[user_id].append(cutoff)
    for event_id, event in events_by_id.items():
        user = users_by_id.get(str(event.get("user_id")))
        occurred_at = event_times.get(event_id)
        available_at = event_available.get(event_id)
        if user is not None:
            split = user.get("split")
            if split in SPLITS and occurred_at is not None:
                occurred_times[str(split)].append(occurred_at)
            if split in SPLITS and available_at is not None:
                available_times[str(split)].append(available_at)
    hidden_users = sorted(
        user_id for user_id, user in users_by_id.items() if user.get("split") == "hidden_test"
    )
    eligible_component_times_by_user = _eligible_event_component_times_by_user(
        events_by_id,
        event_times,
        event_available,
        task_cutoffs_by_user,
        set(hidden_users),
        content_fingerprints_by_path,
    )
    hidden_spans = [
        (
            max(eligible_component_times_by_user[user_id])
            - min(eligible_component_times_by_user[user_id])
        ).total_seconds()
        / (24 * 60 * 60)
        if len(eligible_component_times_by_user[user_id]) >= 2
        else 0.0
        for user_id in hidden_users
    ]
    return {
        "users_by_split": {split: users_by_split[split] for split in SPLITS},
        "unique_tasks_by_split": {
            split: unique_tasks_by_split[split] for split in SPLITS
        },
        "surface_memberships_by_split": {
            split: {surface: surfaces[split][surface] for surface in SURFACES}
            for split in SPLITS
        },
        "scenario_families_by_split": {
            split: {
                family: families[split][family]
                for family in SCENARIO_FAMILIES
            }
            for split in SPLITS
        },
        "memory_requirements_by_split": {
            split: {
                requirement: requirements[split][requirement]
                for requirement in MEMORY_REQUIREMENTS
            }
            for split in SPLITS
        },
        "harm_unique_tasks_by_split": {
            split: harm[split] for split in SPLITS
        },
        "timeline_by_split": {
            split: {
                "event_occurred_at_min": (
                    _format_utc(min(occurred_times[split]))
                    if occurred_times[split]
                    else None
                ),
                "event_occurred_at_max": (
                    _format_utc(max(occurred_times[split]))
                    if occurred_times[split]
                    else None
                ),
                "event_available_at_min": (
                    _format_utc(min(available_times[split]))
                    if available_times[split]
                    else None
                ),
                "event_available_at_max": (
                    _format_utc(max(available_times[split]))
                    if available_times[split]
                    else None
                ),
                "task_timestamp_min": (
                    _format_utc(min(task_times[split]))
                    if task_times[split]
                    else None
                ),
                "task_timestamp_max": (
                    _format_utc(max(task_times[split]))
                    if task_times[split]
                    else None
                ),
            }
            for split in SPLITS
        },
        "hidden_test_per_user_minimums": {
            "history_span_days": min(hidden_spans, default=0.0),
            "memory_bearing_events": min(
                (
                    len(eligible_component_times_by_user[user_id])
                    for user_id in hidden_users
                ),
                default=0,
            ),
            "tasks": min(
                (task_counts_by_user[user_id] for user_id in hidden_users),
                default=0,
            ),
        },
    }


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _minimum_dataset_gates(
    users_by_id: dict[str, dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    inputs_by_id: dict[str, dict[str, Any]],
    labels_by_id: dict[str, dict[str, Any]],
    event_times: dict[str, datetime],
    event_available: dict[str, datetime],
    task_fingerprints: dict[str, tuple[str, ...]],
    content_fingerprints_by_path: dict[str, str],
) -> dict[str, Any]:
    hidden_users = {
        user_id for user_id, user in users_by_id.items() if user.get("split") == "hidden_test"
    }
    representative_task_ids = set(
        _representative_task_ids(inputs_by_id, task_fingerprints)
    )
    tasks_by_user = Counter(
        str(task.get("user_id"))
        for task_id, task in inputs_by_id.items()
        if task_id in representative_task_ids and task.get("split") == "hidden_test"
    )
    task_cutoffs_by_user: dict[str, list[datetime]] = defaultdict(list)
    for task_id, task in inputs_by_id.items():
        if task_id not in representative_task_ids or task.get("split") != "hidden_test":
            continue
        cutoff = _parse_utc(task.get("history_cutoff"))
        if cutoff is not None:
            task_cutoffs_by_user[str(task.get("user_id"))].append(cutoff)
    events_by_user = _eligible_event_component_times_by_user(
        events_by_id,
        event_times,
        event_available,
        task_cutoffs_by_user,
        hidden_users,
        content_fingerprints_by_path,
    )
    hidden_task_ids = {
        task_id
        for task_id, task in inputs_by_id.items()
        if task_id in representative_task_ids and task.get("split") == "hidden_test"
    }
    surface_counts = Counter()
    family_counts = Counter()
    harm_count = 0
    for task_id in hidden_task_ids:
        label = labels_by_id.get(task_id)
        if label is None:
            continue
        surface_counts.update(set(label.get("surface_memberships", [])))
        family = label.get("scenario_family")
        family_counts[family] += 1
        if family in HARM_FAMILIES:
            harm_count += 1
    checks = {
        "users": len(hidden_users) >= 30,
        "tasks": len(hidden_task_ids) >= 300,
        "events_per_user": bool(hidden_users)
        and all(len(events_by_user[user_id]) >= 50 for user_id in hidden_users),
        "tasks_per_user": bool(hidden_users)
        and all(tasks_by_user[user_id] >= 10 for user_id in hidden_users),
        "history_span_per_user": bool(hidden_users)
        and all(
            len(events_by_user[user_id]) >= 2
            and (
                max(events_by_user[user_id])
                - min(events_by_user[user_id])
            ).total_seconds()
            >= 14 * 24 * 60 * 60
            for user_id in hidden_users
        ),
        "surface_coverage": all(surface_counts[surface] >= 50 for surface in SURFACES),
        "scenario_family_coverage": all(
            family_counts[family] > 0 for family in SCENARIO_FAMILIES
        ),
        "harm_task_share": bool(hidden_task_ids)
        and harm_count / len(hidden_task_ids) >= 0.20,
    }
    return {
        "passed": all(checks.values()),
        "checks": {
            name: {"passed": passed}
            for name, passed in checks.items()
        },
    }


def validate_dataset_bundle(
    dataset_root: Path,
    *,
    enforce_minimum_dataset_gates: bool = False,
) -> dict[str, Any]:
    errors = validate_schema_bundle()
    reader = _BundleReader(dataset_root, "dataset-bundle", errors)
    manifest, manifest_raw = _load_manifest(
        reader,
        "dataset-manifest.json",
        "dataset-manifest",
        errors,
    )
    minimum_gates = _empty_minimum_gates()
    separation = {
        "generator_input_file_count": 0,
        "custodian_only_file_count": 0,
        "task_inputs_and_labels_physically_separate": False,
    }
    record_counts: dict[str, int] = {}
    file_count = 0
    release_status: str | None = None
    if manifest is not None:
        manifest_valid = _validate_instance(
            manifest,
            "dataset-manifest",
            "dataset-manifest",
            errors,
        )
        _validate_contract_versions(manifest, "dataset-manifest", errors)
        release_status = (
            str(manifest.get("release_status"))
            if isinstance(manifest.get("release_status"), str)
            else None
        )
        if manifest_valid:
            (
                entries_by_role,
                records_by_role,
                _,
                content_fingerprints_by_path,
            ) = _validate_inventory(
                reader,
                manifest,
                manifest_filename="dataset-manifest.json",
                bundle_role="dataset",
                errors=errors,
            )
            file_count = len(entries_by_role)
            record_counts = {
                role: len(records_by_role.get(role, []))
                for role in DATASET_CORE_ROLES
            }
            exposed_entries_by_role = {
                role: entry
                for role, entry in entries_by_role.items()
                if entry.get("access_class")
                in {"generator_input", "executor_input"}
            }
            _validate_private_content_separation(
                reader,
                manifest,
                reader,
                exposed_entries_by_role,
                errors,
                exposure_error_code="private_content_exposed_across_access_classes",
                exposure_location="dataset-manifest#/files",
                private_change_code="private_file_changed_during_separation_check",
                exposed_change_code="exposed_file_changed_during_separation_check",
            )
            minimum_gates, separation = _validate_dataset_semantics(
                manifest,
                entries_by_role,
                records_by_role,
                content_fingerprints_by_path,
                errors,
            )
    validation_passed = not errors
    status_usable_for_new_run = release_status == "sealed"
    passed = (
        validation_passed
        and status_usable_for_new_run
        and (minimum_gates["passed"] if enforce_minimum_dataset_gates else True)
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "artifact_scope": "private-structural-preflight",
        "private": True,
        "claimable": False,
        "runner_scope": "dataset and sealed-input contract validation only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "manifest_sha256": _sha256(manifest_raw) if manifest_raw is not None else None,
            "manifest_size_bytes": len(manifest_raw) if manifest_raw is not None else None,
            "release_status": release_status,
            "status_usable_for_new_run": status_usable_for_new_run,
            "inventory_file_count": file_count,
            "record_counts": record_counts,
        },
        "separation": separation,
        "minimum_dataset_gates": minimum_gates,
        "minimum_dataset_gates_enforced": enforce_minimum_dataset_gates,
        "validation": {
            "passed": validation_passed,
            "error_count": len(errors),
            "errors": sorted(set(errors))[:MAX_ERRORS],
        },
        "passed": passed,
        "qualification_status": "not_evaluated",
        "caveats": [
            "A valid contract proves structure and exact artifact identity, not consent truth, de-identification quality, independent custody, execution, product effect, or Track B qualification.",
            "Task and event minimums use conservative normalized structural counting; they do not prove semantic sample diversity.",
            "The task-input and hidden-label files are structurally separated; this validator does not provide or verify the controlled executor mount boundary.",
        ],
    }


def validate_run_bundle(
    dataset_root: Path,
    run_root: Path,
    *,
    enforce_minimum_dataset_gates: bool = False,
) -> dict[str, Any]:
    dataset_report = validate_dataset_bundle(
        dataset_root,
        enforce_minimum_dataset_gates=enforce_minimum_dataset_gates,
    )
    errors: list[str] = []
    if not dataset_report["validation"]["passed"]:
        _add_error(errors, "dataset_contract_invalid", "sealed-run")

    dataset_reader = _BundleReader(dataset_root, "dataset-bundle", errors)
    dataset_manifest, dataset_manifest_raw = _load_manifest(
        dataset_reader,
        "dataset-manifest.json",
        "dataset-manifest",
        errors,
    )
    validated_dataset_sha256 = dataset_report["dataset"]["manifest_sha256"]
    if (
        dataset_manifest_raw is not None
        and _sha256(dataset_manifest_raw) != validated_dataset_sha256
    ):
        _add_error(
            errors,
            "dataset_changed_during_run_validation",
            "sealed-run-manifest#/dataset",
        )
    run_reader = _BundleReader(run_root, "run-bundle", errors)
    run_manifest, run_manifest_raw = _load_manifest(
        run_reader,
        "sealed-run-manifest.json",
        "sealed-run-manifest",
        errors,
    )
    file_count = 0
    if run_manifest is not None:
        run_manifest_valid = _validate_instance(
            run_manifest,
            "sealed-run-manifest",
            "sealed-run-manifest",
            errors,
        )
        _validate_contract_versions(run_manifest, "sealed-run-manifest", errors)
        if not run_manifest_valid:
            run_manifest = None
    if run_manifest is not None:
        entries_by_role, _, _, _ = _validate_inventory(
            run_reader,
            run_manifest,
            manifest_filename="sealed-run-manifest.json",
            bundle_role="run",
            errors=errors,
        )
        file_count = len(entries_by_role)
        for role, entry in entries_by_role.items():
            if entry.get("access_class") != "run_config":
                _add_error(
                    errors,
                    "run_file_access_class_mismatch",
                    _report_role(role, "run"),
                )
        dataset_files = (
            dataset_manifest.get("files")
            if isinstance(dataset_manifest, dict)
            else None
        )
        private_dataset_fingerprints = {
            (entry.get("sha256"), entry.get("size_bytes"))
            for entry in (dataset_files if isinstance(dataset_files, list) else [])
            if isinstance(entry, dict)
            and entry.get("access_class") in {"custodian_only", "identity_vault"}
            and _valid_sha256(entry.get("sha256"))
            and isinstance(entry.get("size_bytes"), int)
            and not isinstance(entry.get("size_bytes"), bool)
        }
        if any(
            (entry.get("sha256"), entry.get("size_bytes"))
            in private_dataset_fingerprints
            for entry in entries_by_role.values()
        ):
            _add_error(
                errors,
                "private_dataset_content_exposed_in_run_config",
                "sealed-run-manifest#/files",
            )
        if (
            dataset_report["validation"]["passed"]
            and isinstance(dataset_manifest, dict)
        ):
            _validate_private_content_separation(
                dataset_reader,
                dataset_manifest,
                run_reader,
                entries_by_role,
                errors,
                exposure_error_code="private_dataset_content_exposed_in_run_config",
                exposure_location="sealed-run-manifest#/files",
                private_change_code="dataset_changed_during_run_validation",
                exposed_change_code="run_changed_during_private_separation_check",
            )
        entries_by_path = {
            str(entry.get("path")): entry
            for entry in entries_by_role.values()
            if _safe_relative_path(entry.get("path"))
        }
        _validate_inventory_references(
            run_manifest,
            entries_by_role,
            entries_by_path,
            errors,
            location="sealed-run-manifest#",
        )
        _validate_run_semantics(
            run_manifest,
            (
                dataset_manifest
                if dataset_report["validation"]["passed"]
                else None
            ),
            (
                dataset_manifest_raw
                if dataset_report["validation"]["passed"]
                else None
            ),
            errors,
        )
    validation_passed = not errors
    passed = validation_passed and (
        dataset_report["minimum_dataset_gates"]["passed"]
        if enforce_minimum_dataset_gates
        else True
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "artifact_scope": "private-sealed-run-preflight",
        "private": True,
        "claimable": False,
        "runner_scope": "dataset and sealed-run contract validation only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "validation_passed": dataset_report["validation"]["passed"],
            "minimum_dataset_gates_passed": dataset_report["minimum_dataset_gates"]["passed"],
            "manifest_sha256": dataset_report["dataset"]["manifest_sha256"],
        },
        "run": {
            "manifest_sha256": (
                _sha256(run_manifest_raw) if run_manifest_raw is not None else None
            ),
            "manifest_size_bytes": (
                len(run_manifest_raw) if run_manifest_raw is not None else None
            ),
            "inventory_file_count": file_count,
        },
        "minimum_dataset_gates_enforced": enforce_minimum_dataset_gates,
        "validation": {
            "passed": validation_passed,
            "error_count": len(errors),
            "errors": sorted(set(errors))[:MAX_ERRORS],
        },
        "passed": passed,
        "qualification_status": "not_evaluated",
        "caveats": [
            "A sealed-run manifest binds frozen inputs but does not prove that execution occurred or matched them.",
            "Private-content guards detect whole, substring, normalized-text, and nested-JSON copies, not every paraphrase or semantic leakage channel.",
            "Arm execution, adjudication, scoring, latency, audit completeness, and public projection are outside this structural validator.",
        ],
    }


def _validate_contract_versions(
    value: dict[str, Any],
    location: str,
    errors: list[str],
) -> None:
    expected = {
        "contract_version": CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
    }
    for field_name, expected_value in expected.items():
        if value.get(field_name) != expected_value:
            _add_error(errors, "frozen_version_mismatch", f"{location}#/{field_name}")


def _validate_run_semantics(
    run_manifest: dict[str, Any],
    dataset_manifest: dict[str, Any] | None,
    dataset_manifest_raw: bytes | None,
    errors: list[str],
) -> None:
    sealed_at = _parse_utc(run_manifest.get("sealed_at"))
    if sealed_at is None:
        _add_error(errors, "invalid_timestamp", "sealed-run-manifest#/sealed_at")
    dataset_reference = run_manifest.get("dataset")
    if not isinstance(dataset_reference, dict):
        return
    if dataset_reference.get("manifest_path") != "dataset-manifest.json":
        _add_error(
            errors,
            "dataset_manifest_path_mismatch",
            "sealed-run-manifest#/dataset/manifest_path",
        )
    if dataset_manifest is None or dataset_manifest_raw is None:
        _add_error(errors, "dataset_manifest_unavailable", "sealed-run-manifest#/dataset")
    else:
        if dataset_reference.get("dataset_release_id") != dataset_manifest.get(
            "dataset_release_id"
        ):
            _add_error(errors, "dataset_release_id_mismatch", "sealed-run-manifest#/dataset")
        if dataset_reference.get("manifest_sha256") != _sha256(dataset_manifest_raw):
            _add_error(errors, "dataset_manifest_sha256_mismatch", "sealed-run-manifest#/dataset")
        if dataset_manifest.get("release_status") != "sealed":
            _add_error(errors, "dataset_not_available_for_new_run", "sealed-run-manifest#/dataset")
        frozen_at = _parse_utc(dataset_manifest.get("frozen_at"))
        if frozen_at is not None and sealed_at is not None and sealed_at < frozen_at:
            _add_error(errors, "run_sealed_before_dataset_freeze", "sealed-run-manifest#/sealed_at")

    models = run_manifest.get("models")
    provider_policy = run_manifest.get("provider_policy")
    required_provider = (
        provider_policy.get("upstream_provider")
        if isinstance(provider_policy, dict)
        else None
    )
    if isinstance(models, dict):
        if set(models) != set(MODEL_ROLES):
            _add_error(errors, "model_role_set_mismatch", "sealed-run-manifest#/models")
        for role in MODEL_ROLES:
            config = models.get(role)
            if not isinstance(config, dict):
                continue
            enabled = config.get("enabled")
            if role == "reader" and enabled is not True:
                _add_error(errors, "reader_model_must_be_enabled", f"sealed-run-manifest#/models/{role}")
            if enabled is True:
                for field_name in (
                    "model_id",
                    "immutable_model_version",
                    "actual_upstream_provider",
                    "immutable_route",
                ):
                    field_value = config.get(field_name)
                    if not _safe_model_id(field_value):
                        _add_error(
                            errors,
                            "enabled_model_identity_invalid",
                            f"sealed-run-manifest#/models/{role}/{field_name}",
                        )
                if _looks_rolling_alias(config.get("immutable_model_version")):
                    _add_error(
                        errors,
                        "rolling_model_alias_forbidden",
                        f"sealed-run-manifest#/models/{role}/immutable_model_version",
                    )
                if _looks_rolling_alias(config.get("immutable_route")):
                    _add_error(
                        errors,
                        "rolling_provider_route_forbidden",
                        f"sealed-run-manifest#/models/{role}/immutable_route",
                    )
                if config.get("actual_upstream_provider") != required_provider:
                    _add_error(
                        errors,
                        "model_provider_policy_mismatch",
                        f"sealed-run-manifest#/models/{role}/actual_upstream_provider",
                    )
                for field_name in ("model_id", "immutable_route"):
                    field_value = config.get(field_name)
                    if (
                        isinstance(required_provider, str)
                        and isinstance(field_value, str)
                        and not field_value.startswith(f"{required_provider}/")
                    ):
                        _add_error(
                            errors,
                            "model_provider_namespace_mismatch",
                            f"sealed-run-manifest#/models/{role}/{field_name}",
                        )
            elif enabled is False:
                if any(
                    config.get(field_name) is not None
                    for field_name in (
                        "model_id",
                        "immutable_model_version",
                        "actual_upstream_provider",
                        "immutable_route",
                    )
                ):
                    _add_error(
                        errors,
                        "disabled_model_identity_must_be_null",
                        f"sealed-run-manifest#/models/{role}",
                    )

    memory = run_manifest.get("memory")
    if isinstance(memory, dict):
        _validate_named_config(
            memory.get("retrieval_config"),
            "sealed-run-manifest#/memory/retrieval_config",
            errors,
        )
        _validate_named_config(
            memory.get("feature_flags"),
            "sealed-run-manifest#/memory/feature_flags",
            errors,
        )

    generation = run_manifest.get("generation")
    if isinstance(generation, dict):
        _validate_named_config(
            generation.get("request_parameters"),
            "sealed-run-manifest#/generation/request_parameters",
            errors,
        )
        seed_supported = generation.get("seed_supported")
        seed = generation.get("seed")
        if (seed_supported is True and isinstance(seed, bool)) or (
            seed_supported is True and not isinstance(seed, int)
        ):
            _add_error(errors, "supported_seed_missing", "sealed-run-manifest#/generation/seed")
        if seed_supported is False and seed is not None:
            _add_error(errors, "unsupported_seed_must_be_null", "sealed-run-manifest#/generation/seed")
        retry_count = generation.get("retry_count")
        backoff_seconds = generation.get("backoff_seconds")
        if (
            isinstance(retry_count, int)
            and not isinstance(retry_count, bool)
            and isinstance(backoff_seconds, list)
            and len(backoff_seconds) != retry_count
        ):
            _add_error(errors, "retry_backoff_length_mismatch", "sealed-run-manifest#/generation")
        fallback_policy = generation.get("fallback_policy")
        fallback_routes = generation.get("fallback_routes")
        if fallback_policy == "none" and fallback_routes != []:
            _add_error(errors, "fallback_routes_forbidden", "sealed-run-manifest#/generation")
        if fallback_policy != "none":
            _add_error(errors, "fallback_policy_forbidden", "sealed-run-manifest#/generation")
        if isinstance(fallback_routes, list) and any(
            not _safe_model_id(route) or _looks_rolling_alias(route)
            for route in fallback_routes
        ):
            _add_error(errors, "fallback_route_not_immutable", "sealed-run-manifest#/generation")


def _validate_named_config(
    value: Any,
    location: str,
    errors: list[str],
) -> None:
    if not isinstance(value, list):
        return
    names = [
        item.get("name")
        for item in value
        if isinstance(item, dict) and _valid_id(item.get("name"))
    ]
    if len(names) != len(value) or len(set(names)) != len(names):
        _add_error(errors, "named_config_duplicate", location)
    if names != sorted(names):
        _add_error(errors, "named_config_not_sorted", location)


def _safe_model_id(value: Any) -> bool:
    if not isinstance(value, str) or MODEL_ID_PATTERN.fullmatch(value) is None:
        return False
    if value.startswith(("/", "\\")) or "\\" in value:
        return False
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return False
    lowered = value.lower()
    return not lowered.startswith(("sk-", "api-key-", "token-", "bearer-"))


def _looks_rolling_alias(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    parts = re.split(r"[/:._+-]+", lowered)
    return any(part in {"latest", "default", "auto", "current", "stable"} for part in parts)


def _empty_minimum_gates() -> dict[str, Any]:
    names = (
        "users",
        "tasks",
        "events_per_user",
        "tasks_per_user",
        "history_span_per_user",
        "surface_coverage",
        "scenario_family_coverage",
        "harm_task_share",
    )
    return {
        "passed": False,
        "checks": {name: {"passed": False} for name in names},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate private OfficeLifeMemoryBench Track B artifact contracts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    dataset_parser = subparsers.add_parser("validate-dataset")
    dataset_parser.add_argument("dataset_root", type=Path)
    dataset_parser.add_argument(
        "--enforce-minimum-dataset-gates",
        action="store_true",
        help="enforce frozen cohort minimums; does not establish Track B qualification",
    )
    dataset_parser.add_argument("--output-json", type=Path)
    run_parser = subparsers.add_parser("validate-run")
    run_parser.add_argument("dataset_root", type=Path)
    run_parser.add_argument("run_root", type=Path)
    run_parser.add_argument(
        "--enforce-minimum-dataset-gates",
        action="store_true",
        help="enforce frozen cohort minimums; does not establish Track B qualification",
    )
    run_parser.add_argument("--output-json", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    roots = [args.dataset_root]
    if args.command == "validate-run":
        roots.append(args.run_root)
        report = validate_run_bundle(
            args.dataset_root,
            args.run_root,
            enforce_minimum_dataset_gates=args.enforce_minimum_dataset_gates,
        )
    else:
        report = validate_dataset_bundle(
            args.dataset_root,
            enforce_minimum_dataset_gates=args.enforce_minimum_dataset_gates,
        )
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output_json is not None:
        if _output_overlaps_inputs(args.output_json, roots):
            raise SystemExit("output path must be outside every private input root")
        args.output_json.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    if report["passed"]:
        return 0
    if report["validation"]["passed"]:
        return 3
    return 4


def _output_overlaps_inputs(output_path: Path, roots: list[Path]) -> bool:
    output = output_path.expanduser().resolve(strict=False)
    for root in roots:
        resolved_root = root.expanduser().resolve(strict=False)
        if output == resolved_root or resolved_root in output.parents:
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
