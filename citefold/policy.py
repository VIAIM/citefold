from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


MEMORY_TYPE_ALIASES = {
    "episode": "episodic",
    "episodic": "episodic",
    "fact": "semantic",
    "people": "semantic",
    "person": "semantic",
    "preference": "semantic",
    "profile": "semantic",
    "semantic": "semantic",
    "task": "prospective",
    "prospective": "prospective",
    "procedure": "procedural",
    "procedural": "procedural",
}
MODEL_ORIGINS = {"model", "model_generated", "openrouter"}
THIRD_PARTY_ORIGINS = {"third_party_agent", "external_agent", "tool_output"}
SECRET_PATTERN = re.compile(
    r"(?:"
    r"api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret|"
    r"bearer\s+[A-Za-z0-9._-]+|"
    r"\bsk-(?:or-)?[A-Za-z0-9_-]{8,}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/:]+:[^\s/@]+@|"
    r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    r")",
    re.IGNORECASE,
)
PERMISSION_PATTERN = re.compile(
    r"(?:"
    r"(?:grant|授予|绕过|disable|关闭).{0,24}(?:permission|权限|安全|approval|审批)|"
    r"(?:无需|不经|跳过|免于).{0,12}(?:approval|审批|授权|确认).{0,16}(?:执行|部署|运行|操作)?|"
    r"(?:权限|角色).{0,12}(?:设为|设置为|改为|提升为).{0,12}(?:admin|administrator|管理员|root)"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PolicyDecision:
    accepted: bool
    auto_activate: bool
    reason: str
    normalized_type: str
    record_metadata: dict[str, Any]


class PolicyGate:
    """Pure policy rules; no model output can bypass this gate."""

    stable_profile_confidence = 0.75

    def normalize_memory_type(self, memory_type: str) -> str:
        normalized = MEMORY_TYPE_ALIASES.get(memory_type.lower())
        if normalized is None:
            raise ValueError(f"Unsupported memory_type: {memory_type}")
        return normalized

    def evaluate(
        self,
        memory_type: str,
        content: str,
        source_origin: str,
        confidence: float,
        metadata: dict[str, Any] | None = None,
        user_authorized: bool = False,
    ) -> PolicyDecision:
        normalized_type = self.normalize_memory_type(memory_type)
        metadata = dict(metadata or {})
        record_metadata: dict[str, Any] = {}

        if normalized_type == "procedural":
            record_metadata.update({"executable": False, "grants_permissions": False})
            if SECRET_PATTERN.search(content):
                return PolicyDecision(False, False, "procedural memory contains a credential-like secret", normalized_type, record_metadata)
            if PERMISSION_PATTERN.search(content):
                return PolicyDecision(False, False, "procedural memory cannot grant or bypass permissions", normalized_type, record_metadata)

        origin = source_origin.lower()
        if origin in MODEL_ORIGINS:
            return PolicyDecision(True, False, "model output remains a pending candidate", normalized_type, record_metadata)
        if origin in THIRD_PARTY_ORIGINS:
            return PolicyDecision(True, False, "third-party output requires approval", normalized_type, record_metadata)

        if metadata.get("modality") in {"audio", "video"} and confidence < self.stable_profile_confidence:
            if normalized_type in {"semantic", "procedural"}:
                return PolicyDecision(True, False, "low-confidence media observation cannot update stable memory", normalized_type, record_metadata)

        if origin == "agent_output":
            return PolicyDecision(True, False, "agent output is evidence, not a user fact", normalized_type, record_metadata)

        if origin in {"user_reported", "user_correction"} and user_authorized:
            return PolicyDecision(True, True, "explicit user statement may be synchronously accepted", normalized_type, record_metadata)

        return PolicyDecision(True, False, "candidate requires explicit policy approval", normalized_type, record_metadata)

    @staticmethod
    def source_origin(role: str, source: str) -> str:
        normalized_source = source.lower()
        if "tool" in normalized_source:
            return "tool_output"
        if any(marker in normalized_source for marker in ("external", "third_party", "crm_agent")):
            return "third_party_agent"
        if any(marker in normalized_source for marker in ("openrouter", "model_generated")):
            return "model_generated"
        normalized_role = role.lower()
        if normalized_role == "user":
            return "user_reported"
        if normalized_role in {"assistant", "agent"}:
            return "agent_output"
        if normalized_role in {"tool", "function"}:
            return "tool_output"
        if "agent" in normalized_source and normalized_source not in {"text_agent", "voice_agent"}:
            return "third_party_agent"
        return "external_content"
