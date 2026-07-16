# Concepts

Citefold borrows useful ideas from human memory—episodes, consolidation, cues, reinforcement, and forgetting—but implements explicit software contracts rather than claiming biological fidelity.

## The evidence hierarchy

| Object | Meaning | Mutable? | Example |
|---|---|---:|---|
| `Asset` | Original or derived bytes, addressed by SHA-256 | No | a chat payload, PNG, WAV, or video |
| `Observation` | Something recorded from an asset, with a locator and producer | Append-only | text span, OCR line, ASR segment, video frame description |
| `Episode` | Related observations placed in time and context | Append-only status history | one chat turn, meeting, voice final, or image capture |
| `MemoryCandidate` | A proposed durable claim plus evidence, confidence, risk, and operation | Status changes are appended | “Alex prefers Friday follow-ups” |
| `MemoryRecord` | A versioned, active or inactive durable claim | New version, never silent overwrite | semantic preference or prospective task |
| `Revision` | The operation that produced a record state | Append-only | add, reinforce, supersede, conflict, archive, delete |
| `MemoryPack` | Query-specific, bounded context returned to an agent | Rebuilt per recall | selected facts, episodes, conflicts, unknowns, citations |

The hierarchy prevents a common collapse: model-generated text is not automatically promoted from “observation” to “known fact.”

## Four memory record types

- **Episodic:** what happened in a time-bounded situation.
- **Semantic:** relatively stable facts, preferences, and relationships.
- **Prospective:** open commitments, reminders, and intended future actions.
- **Procedural:** reference instructions for how something is normally done.

Procedural memory is data, not authority. It is stored with `executable=false` and `grants_permissions=false`; an agent still needs its own authorization checks before acting.

## Candidate operations

Candidates describe both a claim and an intended relationship to current memory:

- `ADD` creates a new record.
- `REINFORCE` adds supported evidence to an existing claim.
- `SUPERSEDE` creates a new version and closes the old one.
- `CONFLICT` keeps incompatible evidence visible until resolved.
- `IGNORE` records that the proposal should not change durable memory.

Explicit user statements can pass the trusted local policy for narrow supported cases. Media models, tools, and third-party agents remain pending by default and require approval.

## Coverage, not confidence theater

A `MemoryPack` reports one of three coverage states:

- `supported`: selected claims have a complete live citation closure.
- `partial`: relevant evidence exists, but unresolved conflicts or incomplete support remain.
- `none`: Citefold cannot provide a supported durable answer. It may still include clearly quoted working evidence such as an unfinished voice fragment.

Coverage does not mean the underlying source is objectively correct. It means the returned material is traceable to the recorded source and passes the current evidence contract.

## Identity scope

Every operation requires:

```python
MemoryScope(
    tenant_id="acme",
    user_id="alex",
    namespace="work",
    agent_id="copilot",
    session_id="session-42",
)
```

`tenant_id`, `user_id`, and `namespace` define the storage and retrieval boundary. `agent_id` and `session_id` preserve provenance inside that boundary. Scope IDs accept only letters, numbers, `_`, `-`, and `.`, are limited to 128 characters, and reject path traversal values.

Scope is a data-isolation primitive, not a complete authorization system. The host application must authenticate callers before constructing it.

## Source trust

Citefold records where content came from:

- direct user input;
- agent or tool output;
- external content;
- model-generated media observations;
- third-party candidate submissions.

Normal user-fact queries exclude agent/tool output unless the query explicitly asks what the assistant previously said or recommended. Media and external content are rendered as quoted untrusted data so instructions inside them do not silently become system instructions.

## Evidence versus truth

Citations answer **“why did the memory system return this?”**, not **“is this statement universally true?”** Use source quality, recency, conflict handling, and domain-specific validation on top of Citefold when the distinction matters.
