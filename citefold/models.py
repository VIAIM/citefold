from __future__ import annotations

import re
import math
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCOPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_SCOPE_ID_LENGTH = 128
AGENT_TURN_CONTRACT = "agent-turn-v1"
AGENT_TURN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_AGENT_TURN_ID_LENGTH = 128


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


def _validate_agent_turn_request(user_message: Any, turn_id: Any, mode: Any) -> None:
    if not isinstance(user_message, str) or not user_message.strip():
        raise ValueError("user_message must be a non-empty string")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("turn_id must be a non-empty string")
    if len(turn_id) > MAX_AGENT_TURN_ID_LENGTH:
        raise ValueError(f"turn_id must be at most {MAX_AGENT_TURN_ID_LENGTH} characters")
    if not AGENT_TURN_ID_PATTERN.fullmatch(turn_id):
        raise ValueError("turn_id must match [A-Za-z0-9_.-]+")
    if mode not in {"text", "voice"}:
        raise ValueError("mode must be 'text' or 'voice'")


@dataclass(frozen=True)
class AgentTurnContext:
    turn_id: str
    scope: MemoryScope
    user_message: str
    mode: str
    memory_pack: MemoryPack
    contract_version: str = AGENT_TURN_CONTRACT

    def __post_init__(self) -> None:
        _validate_agent_turn_request(self.user_message, self.turn_id, self.mode)
        if self.contract_version != AGENT_TURN_CONTRACT:
            raise ValueError(f"unsupported agent turn contract: {self.contract_version}")
        if not isinstance(self.scope, MemoryScope):
            raise TypeError("scope must be a MemoryScope")
        if not isinstance(self.memory_pack, MemoryPack):
            raise TypeError("memory_pack must be a MemoryPack")
        if self.memory_pack.identity_scope != self.scope.as_record():
            raise ScopeError("memory_pack identity scope does not match the agent turn scope")

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "turn_id": self.turn_id,
            "scope": self.scope.as_record(),
            "user_message": self.user_message,
            "mode": self.mode,
            "memory_pack": {
                "identity_scope": dict(self.memory_pack.identity_scope),
                "coverage": self.memory_pack.coverage,
                "context_markdown": self.memory_pack.markdown,
                "selected_nodes": [
                    {
                        "node_id": node.node_id,
                        "path": node.path,
                        "reason": node.reason,
                    }
                    for node in self.memory_pack.selected_nodes
                ],
                "citations": deepcopy(self.memory_pack.citations),
                "conflicts": deepcopy(self.memory_pack.conflicts),
                "unknowns": list(self.memory_pack.unknowns),
            },
        }
