from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCOPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_SCOPE_ID_LENGTH = 128


class ScopeError(ValueError):
    """Raised when a memory identity scope is incomplete or invalid."""


class EvidenceValidationError(ValueError):
    """Raised when a derived memory cannot be traced to live evidence."""


class PrivacyPolicyError(RuntimeError):
    """Raised when a model request cannot satisfy the required privacy policy."""


def finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def unit_interval(value: Any, field_name: str) -> float:
    return max(0.0, min(1.0, finite_number(value, field_name)))


@dataclass(frozen=True)
class MemoryScope:
    tenant_id: str
    user_id: str
    namespace: str
    agent_id: str
    session_id: str

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "user_id", "namespace", "agent_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ScopeError(f"{field_name} must be a non-empty string")
            if value != value.strip():
                raise ScopeError(f"{field_name} must not have leading or trailing whitespace")
            if value in {".", ".."}:
                raise ScopeError(f"{field_name} must not be a path traversal segment")
            if len(value) > MAX_SCOPE_ID_LENGTH:
                raise ScopeError(f"{field_name} must be at most {MAX_SCOPE_ID_LENGTH} characters")
            if not SCOPE_ID_PATTERN.fullmatch(value):
                raise ScopeError(f"{field_name} must match [A-Za-z0-9_.-]+")

    def as_record(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "namespace": self.namespace,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class EvidenceResult:
    evidence_id: str
    evidence_ref: str
    path: Path
    evidence_anchor: str | None = None


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: str
    memory_type: str
    status: str


@dataclass(frozen=True)
class IngestResult:
    evidence_refs: list[str] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    memory_paths: list[str] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)
    observation_ids: list[str] = field(default_factory=list)
    episode_ids: list[str] = field(default_factory=list)
    record_ids: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SelectedNode:
    node_id: str
    path: str
    reason: str


@dataclass(frozen=True)
class Asset:
    asset_id: str
    mime_type: str
    sha256: str
    storage_path: str
    scope: dict[str, str]
    source: str
    captured_at: str
    occurred_at: str | None = None
    privacy_policy: dict[str, Any] = field(default_factory=dict)
    original_name: str | None = None
    byte_size: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Observation:
    observation_id: str
    asset_id: str
    modality: str
    locator: dict[str, Any]
    content: str
    scope: dict[str, str]
    producer_type: str
    producer_model: str | None
    confidence: float
    source_origin: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Episode:
    episode_id: str
    start_at: str
    end_at: str
    participants: list[str]
    summary: str
    observation_ids: list[str]
    scope: dict[str, str]
    scene: str | None
    topics: list[str]
    source_origin: str
    created_at: str
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class MemoryCandidate:
    candidate_id: str
    memory_type: str
    content: str
    evidence_refs: list[str]
    scope: dict[str, str]
    source_origin: str
    confidence: float
    risk: str
    sensitivity: str
    salience: float
    proposed_operation: str
    created_at: str
    status: str = "pending"
    target_record_id: str | None = None
    claim_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    memory_type: str
    content: str
    status: str
    valid_from: str
    valid_to: str | None
    observed_at: str
    supersedes_id: str | None
    version: int
    source_origin: str
    evidence_refs: list[str]
    scope: dict[str, str]
    confidence: float
    claim_key: str
    access_strength: float = 1.0
    pinned: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Revision:
    revision_id: str
    operation: str
    record: dict[str, Any]
    created_at: str
    actor: str
    reason: str
    idempotency_key: str
    scope: dict[str, str]
    previous_record: dict[str, Any] | None = None

    def as_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class MemoryPack:
    markdown: str
    selected_nodes: list[SelectedNode]
    identity_scope: dict[str, str]
    confirmed: list[dict[str, Any]] = field(default_factory=list)
    user_reported: list[dict[str, Any]] = field(default_factory=list)
    preferences: list[dict[str, Any]] = field(default_factory=list)
    open_tasks: list[dict[str, Any]] = field(default_factory=list)
    procedures: list[dict[str, Any]] = field(default_factory=list)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    pending_inferences: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    coverage: str = "none"
