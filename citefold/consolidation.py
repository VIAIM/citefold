from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Callable

from .models import MemoryCandidate, MemoryRecord, MemoryScope, Revision, unit_interval
from .policy import PolicyDecision, PolicyGate
from .store import LedgerStore


ProjectionCallback = Callable[[MemoryScope], None]
VALID_OPERATIONS = {"ADD", "REINFORCE", "SUPERSEDE", "CONFLICT", "IGNORE"}


class ConsolidationService:
    def __init__(
        self,
        store: LedgerStore,
        policy: PolicyGate,
        project: ProjectionCallback | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.project = project

    def submit(
        self,
        scope: MemoryScope,
        memory_type: str,
        content: str,
        evidence_refs: list[str],
        source_origin: str,
        confidence: float,
        risk: str = "low",
        sensitivity: str = "private",
        salience: float = 0.5,
        proposed_operation: str = "ADD",
        target_record_id: str | None = None,
        claim_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        user_authorized: bool = False,
    ) -> tuple[MemoryCandidate, PolicyDecision, MemoryRecord | None]:
        normalized_confidence = unit_interval(confidence, "confidence")
        normalized_salience = unit_interval(salience, "salience")
        operation = proposed_operation.upper()
        if operation not in VALID_OPERATIONS:
            raise ValueError(f"Unsupported consolidation operation: {proposed_operation}")
        decision = self.policy.evaluate(
            memory_type=memory_type,
            content=content,
            source_origin=source_origin,
            confidence=normalized_confidence,
            metadata=metadata,
            user_authorized=user_authorized,
        )
        if not decision.accepted:
            raise ValueError(decision.reason)
        normalized_key = claim_key or self.claim_key(decision.normalized_type, content, metadata)
        candidate_id = self.store.stable_id(
            "cand",
            *scope.as_record().values(),
            decision.normalized_type,
            content.strip(),
            "\n".join(sorted(evidence_refs)),
            source_origin,
            operation,
            target_record_id or "",
        )
        candidate_metadata = dict(metadata or {})
        candidate_metadata["policy_reason"] = decision.reason
        candidate_metadata.update(decision.record_metadata)
        candidate = MemoryCandidate(
            candidate_id=candidate_id,
            memory_type=decision.normalized_type,
            content=content.strip(),
            evidence_refs=list(dict.fromkeys(evidence_refs)),
            scope=scope.as_record(),
            source_origin=source_origin,
            confidence=normalized_confidence,
            risk=risk,
            sensitivity=sensitivity,
            salience=normalized_salience,
            proposed_operation=operation,
            created_at=self.store.now_iso(),
            target_record_id=target_record_id,
            claim_key=normalized_key,
            metadata=candidate_metadata,
        )
        appended = self.store.append_candidate(scope, candidate)
        if not appended:
            existing = self.store.current_candidates(scope).get(candidate_id)
            if existing and existing.get("status") == "approved":
                record_id = existing.get("metadata", {}).get("record_id")
                record = self._record(scope, record_id) if record_id else None
                return candidate, decision, record
        record: MemoryRecord | None = None
        if decision.auto_activate:
            record = self.activate(scope, candidate_id, actor=scope.agent_id)
        return candidate, decision, record

    def activate(self, scope: MemoryScope, candidate_id: str, actor: str) -> MemoryRecord | None:
        candidates = self.store.current_candidates(scope)
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown candidate_id: {candidate_id}")
        if candidate.get("status") == "approved":
            record_id = candidate.get("metadata", {}).get("record_id")
            return self._record(scope, record_id) if record_id else None

        evidence_refs = list(candidate.get("evidence_refs", []))
        self.store.assert_evidence(scope, evidence_refs)
        operation = str(candidate.get("proposed_operation", "ADD")).upper()
        if operation == "IGNORE":
            self.store.append_candidate_state(scope, candidate, "ignored")
            return None

        active = self.store.effective_records(scope, include_inactive=False)
        target = self._select_target(active, candidate)
        if operation == "ADD" and target is not None:
            operation = "REINFORCE" if target["content"] == candidate["content"] else "CONFLICT"

        if operation == "REINFORCE":
            if target is None:
                operation = "ADD"
            else:
                record = self._reinforce(scope, candidate, target, actor)
                self._approve(scope, candidate, record)
                self._project(scope)
                return record

        if operation == "SUPERSEDE" and target is None:
            raise KeyError("SUPERSEDE requires an active target record")

        if operation == "SUPERSEDE":
            record = self._supersede(scope, candidate, target, actor)
        elif operation == "CONFLICT":
            record = self._add(scope, candidate, actor, conflict_target=target)
        else:
            record = self._add(scope, candidate, actor)
        self._approve(scope, candidate, record)
        self._project(scope)
        return record

    def correct(
        self,
        scope: MemoryScope,
        record_id: str,
        content: str,
        evidence_refs: list[str],
        actor: str,
        reason: str = "explicit user correction",
    ) -> MemoryRecord:
        target = self._record(scope, record_id)
        if target is None or target.status != "active":
            raise KeyError(f"Unknown active record_id: {record_id}")
        candidate, _decision, record = self.submit(
            scope=scope,
            memory_type=target.memory_type,
            content=content,
            evidence_refs=evidence_refs,
            source_origin="user_correction",
            confidence=1.0,
            proposed_operation="SUPERSEDE",
            target_record_id=record_id,
            claim_key=target.claim_key,
            metadata={**target.metadata, "correction_reason": reason},
            user_authorized=True,
        )
        if record is None:
            record = self.activate(scope, candidate.candidate_id, actor=actor)
        if record is None:  # pragma: no cover - guarded by SUPERSEDE semantics.
            raise RuntimeError("Correction did not produce a record")
        return record

    def change_status(
        self,
        scope: MemoryScope,
        record_id: str,
        status: str,
        actor: str,
        reason: str,
    ) -> MemoryRecord:
        if status not in {"active", "archived", "deleted"}:
            raise ValueError(f"Unsupported record status: {status}")
        current = self._record(scope, record_id)
        if current is None:
            raise KeyError(f"Unknown record_id: {record_id}")
        changed = replace(current, status=status, valid_to=self.store.now_iso() if status == "deleted" else current.valid_to)
        self._revision(scope, status.upper(), changed, actor, reason, f"status:{record_id}:{status}")
        self._project(scope)
        return changed

    def change_pinned(
        self,
        scope: MemoryScope,
        record_id: str,
        pinned: bool,
        actor: str,
        reason: str,
    ) -> tuple[MemoryRecord, bool]:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason must not be empty")
        current = self._record(scope, record_id)
        if current is None or current.status != "active":
            raise KeyError(f"Unknown active record_id: {record_id}")
        if current.pinned == pinned:
            return current, False

        operation = "PIN" if pinned else "UNPIN"
        transition = 1 + sum(
            1
            for revision in self.store.read_ledger(scope, "revisions")
            if revision.get("operation") in {"PIN", "UNPIN"}
            and isinstance(revision.get("record"), dict)
            and revision["record"].get("record_id") == record_id
        )
        changed = replace(current, pinned=pinned)
        self._revision(
            scope,
            operation,
            changed,
            actor,
            normalized_reason,
            f"pinned:{record_id}:{transition}:{operation}",
            previous_record=current,
        )
        self._project(scope)
        return changed, True

    def _add(
        self,
        scope: MemoryScope,
        candidate: dict[str, Any],
        actor: str,
        conflict_target: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        now = self.store.now_iso()
        version = 1
        same_claim = [
            item
            for item in self.store.current_records(scope)
            if item.get("claim_key") == candidate.get("claim_key")
        ]
        if same_claim:
            version = max(int(item.get("version", 1)) for item in same_claim) + 1
        metadata = dict(candidate.get("metadata", {}))
        if conflict_target is not None:
            metadata["conflicts_with"] = [conflict_target["record_id"]]
            metadata["conflict_group"] = self.store.stable_id(
                "conflict", candidate.get("claim_key", ""), conflict_target["record_id"]
            )
        record_id = self.store.stable_id("mem", candidate["candidate_id"], str(version))
        record = MemoryRecord(
            record_id=record_id,
            memory_type=candidate["memory_type"],
            content=candidate["content"],
            status="active",
            valid_from=now,
            valid_to=None,
            observed_at=candidate.get("created_at", now),
            supersedes_id=None,
            version=version,
            source_origin=candidate["source_origin"],
            evidence_refs=list(candidate["evidence_refs"]),
            scope=scope.as_record(),
            confidence=float(candidate["confidence"]),
            claim_key=candidate.get("claim_key") or self.claim_key(candidate["memory_type"], candidate["content"]),
            metadata=metadata,
        )
        operation = "CONFLICT" if conflict_target is not None else "ADD"
        self._revision(scope, operation, record, actor, "candidate accepted", f"{candidate['candidate_id']}:{operation}")
        return record

    def _reinforce(
        self,
        scope: MemoryScope,
        candidate: dict[str, Any],
        target: dict[str, Any],
        actor: str,
    ) -> MemoryRecord:
        metadata = dict(target.get("metadata", {}))
        metadata["reinforcement_count"] = int(metadata.get("reinforcement_count", 0)) + 1
        reinforced = self._from_dict(
            {
                **target,
                "evidence_refs": list(dict.fromkeys(target.get("evidence_refs", []) + candidate["evidence_refs"])),
                "metadata": metadata,
            }
        )
        self._revision(
            scope,
            "REINFORCE",
            reinforced,
            actor,
            "duplicate claim with additional evidence",
            f"{candidate['candidate_id']}:REINFORCE:{target['record_id']}",
        )
        return reinforced

    def _supersede(
        self,
        scope: MemoryScope,
        candidate: dict[str, Any],
        target: dict[str, Any],
        actor: str,
    ) -> MemoryRecord:
        now = self.store.now_iso()
        old = self._from_dict({**target, "status": "superseded", "valid_to": now})
        metadata = dict(candidate.get("metadata", {}))
        record_id = self.store.stable_id("mem", candidate["candidate_id"], str(int(target.get("version", 1)) + 1))
        new = MemoryRecord(
            record_id=record_id,
            memory_type=candidate["memory_type"],
            content=candidate["content"],
            status="active",
            valid_from=now,
            valid_to=None,
            observed_at=candidate.get("created_at", now),
            supersedes_id=target["record_id"],
            version=int(target.get("version", 1)) + 1,
            source_origin=candidate["source_origin"],
            evidence_refs=list(candidate["evidence_refs"]),
            scope=scope.as_record(),
            confidence=float(candidate["confidence"]),
            claim_key=target["claim_key"],
            access_strength=float(target.get("access_strength", 1.0)),
            pinned=bool(target.get("pinned", False)),
            metadata=metadata,
        )
        self._revision(
            scope,
            "SUPERSEDE",
            new,
            actor,
            "new version accepted",
            f"{candidate['candidate_id']}:new:{record_id}",
            previous_record=old,
        )
        return new

    def _approve(self, scope: MemoryScope, candidate: dict[str, Any], record: MemoryRecord) -> None:
        metadata = dict(candidate.get("metadata", {}))
        metadata["record_id"] = record.record_id
        self.store.append_candidate_state(scope, {**candidate, "metadata": metadata}, "approved")

    def _revision(
        self,
        scope: MemoryScope,
        operation: str,
        record: MemoryRecord,
        actor: str,
        reason: str,
        idempotency_key: str,
        previous_record: MemoryRecord | None = None,
    ) -> None:
        revision = Revision(
            revision_id=self.store.stable_id("rev", *scope.as_record().values(), idempotency_key),
            operation=operation,
            record=record.as_record(),
            created_at=self.store.now_iso(),
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            scope=scope.as_record(),
            previous_record=previous_record.as_record() if previous_record is not None else None,
        )
        self.store.append_revision(scope, revision)

    def _record(self, scope: MemoryScope, record_id: str | None) -> MemoryRecord | None:
        if record_id is None:
            return None
        for record in self.store.effective_records(scope):
            if record.get("record_id") == record_id:
                return self._from_dict(record)
        return None

    @staticmethod
    def _select_target(active: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any] | None:
        target_id = candidate.get("target_record_id")
        if target_id:
            return next((item for item in active if item.get("record_id") == target_id), None)
        claim_key = candidate.get("claim_key")
        return next((item for item in reversed(active) if item.get("claim_key") == claim_key), None)

    def _project(self, scope: MemoryScope) -> None:
        if self.project is not None:
            self.project(scope)

    @staticmethod
    def claim_key(memory_type: str, content: str, metadata: dict[str, Any] | None = None) -> str:
        metadata = metadata or {}
        category = str(metadata.get("category", "general"))
        subject = str(metadata.get("subject", ""))
        if memory_type == "semantic" and category in {"preference", "identity", "boundary"}:
            return f"{memory_type}:{category}:{subject or 'user'}"
        normalized = re.sub(r"\s+", " ", content.strip().lower())
        return f"{memory_type}:{category}:{LedgerStore.stable_id('claim', normalized)}"

    @staticmethod
    def _from_dict(value: dict[str, Any]) -> MemoryRecord:
        fields = {
            "record_id",
            "memory_type",
            "content",
            "status",
            "valid_from",
            "valid_to",
            "observed_at",
            "supersedes_id",
            "version",
            "source_origin",
            "evidence_refs",
            "scope",
            "confidence",
            "claim_key",
            "access_strength",
            "pinned",
            "metadata",
        }
        return MemoryRecord(**{key: value[key] for key in fields if key in value})
