# Agent integrations

Citefold is framework-neutral. The versioned `agent-turn-v1` contract recalls before a model call and records only a successfully completed turn.

## Minimal loop

```python
from collections.abc import Callable

from citefold import Citefold, MemoryScope


def run_turn(
    memory: Citefold,
    scope: MemoryScope,
    user_message: str,
    model: Callable[[str, str], str],
) -> str:
    turn = memory.prepare_agent_turn(scope, user_message, token_budget=1_200)
    assistant_message = model(user_message, turn.memory_pack.markdown)
    memory.complete_agent_turn(
        turn,
        assistant_message,
        source="agent_loop",
    )
    return assistant_message
```

[`examples/agent_loop.py`](https://github.com/VIAIM/citefold/blob/main/examples/agent_loop.py) is runnable without an LLM; it uses a small stand-in responder to make the hooks visible.

Use a persistent, dedicated root in a real host application. The example uses `TemporaryDirectory` only so its offline smoke data is deleted on exit.

## `agent-turn-v1` contract

`prepare_agent_turn()` returns an `AgentTurnContext` with a library-generated `turn_id`. A trusted host may instead pass its own `[A-Za-z0-9_.-]+` request ID so logs can be correlated and a retry can reuse the same identity.

```python
turn = memory.prepare_agent_turn(
    scope,
    user_message,
    turn_id=trusted_request_id,
    mode="text",
    token_budget=1_200,
)

payload = turn.as_dict()  # strict JSON-compatible agent-turn-v1 envelope
```

The envelope has stable top-level fields `contract_version`, `turn_id`, `scope`, `user_message`, `mode`, and `memory_pack`. The nested pack exposes `identity_scope`, `coverage`, `context_markdown`, `selected_nodes`, `citations`, `conflicts`, and `unknowns`. Use `turn.memory_pack` when the richer Python object is more convenient.

Contract behavior:

- prepare performs recall but does not call the host model or persist the current user message as evidence;
- `coverage="none"` is a valid result, not an exception;
- recall, storage, validation, and completion errors propagate to the host;
- call `complete_agent_turn()` only after the model has produced a final response;
- the user and assistant messages are stored as `user_reported` and `agent_output`; assistant text is not promoted to a trusted user fact;
- a `turn_id` is scoped by the complete `MemoryScope`; once completed, changing its messages, source, mode, or metadata fails before turn evidence is written;
- retrying an identical successful completion returns its stored receipt without re-ingesting evidence;
- a deleted or invalidated turn ID cannot be reused; v1 still does not promise transaction-level exactly-once recovery from a process or power failure during the first completion.

Scope, source, and host request IDs are authority-bearing values. Build them from authenticated application state, never from model output.

## Put MemoryPack below trusted instructions

Treat recalled context as data. A safe prompt structure is:

```text
SYSTEM: your trusted application and tool policy
DEVELOPER: how the agent should use cited memory and handle unknowns
CONTEXT: the complete MemoryPack, including Identity Scope and Coverage
USER: the current user message
```

Do not splice MemoryPack text into the system policy. Even supported memory can quote untrusted media or inaccurate user statements.

## Check coverage

```python
turn = memory.prepare_agent_turn(scope, query)
pack = turn.memory_pack

if pack.coverage == "none":
    # Ask the user, search an authoritative system, or answer with uncertainty.
    ...
elif pack.coverage == "partial":
    # Surface conflicts instead of silently choosing one record.
    ...
else:
    # Use the cited context, subject to domain policy.
    ...
```

Coverage is about evidence closure, not objective truth. High-stakes domains should verify against authoritative sources.

## Cross-session memory

Tenant, user, and namespace stay constant across sessions; agent and session identify provenance:

```python
prior = MemoryScope("acme", "alex", "work", "copilot", "session-1")
current = MemoryScope("acme", "alex", "work", "copilot", "session-2")

memory.ingest_text(prior, "The launch codename is ORCHID-77.", source="chat")
turn = memory.prepare_agent_turn(current, "What is the launch codename?")
```

The host must never reuse tenant/user values based only on model output.

## Other agents should submit candidates

Give third-party agents a narrow candidate path rather than direct authority to write trusted user memory:

```python
evidence = memory.append_event(
    scope,
    source="crm_agent",
    payload={"text": "Alex asked for Friday afternoon follow-up."},
)
candidate = memory.submit_candidate(
    scope,
    source_agent="crm_agent",
    memory_type="people",
    content="Alex prefers Friday afternoon follow-up.",
    evidence_refs=[evidence.evidence_ref],
    confidence=0.82,
)

# A trusted review flow can later call:
memory.approve_candidate(scope, candidate.candidate_id)
```

The current approval API is synchronous and local. A user-facing review queue is a host application concern.

## Model-provider integration

You may use any model for the main agent turn. Citefold's only built-in model adapter is the optional [OpenRouter provider](providers/openrouter.md) for observation, consolidation, and embeddings. The main model receives the contract's Markdown context; it does not need a Citefold-specific SDK.

## Framework adapters

Official OpenAI Agents SDK, LangGraph, LlamaIndex, and MCP adapters are not shipped in this release. Prefer the two explicit hooks above over a deep framework abstraction. Adapter contributions should preserve:

- full `MemoryScope` on every operation;
- recall before, ingest after;
- source/role assigned by trusted host code;
- coverage and conflicts visible to the agent;
- no automatic approval of model-generated candidates;
- citations kept with the claims they support.
