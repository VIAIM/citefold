# Agent integrations

Citefold is framework-neutral. Integrate it at two lifecycle points: recall before a model turn and ingest after the completed turn.

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
    pack = memory.recall(scope, user_message, token_budget=1_200)
    assistant_message = model(user_message, pack.markdown)
    memory.ingest_chat(
        scope,
        [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ],
        source="agent_loop",
    )
    return assistant_message
```

[`examples/agent_loop.py`](https://github.com/jappre/citefold/blob/main/examples/agent_loop.py) is runnable without an LLM; it uses a small stand-in responder to make the hooks visible.

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
pack = memory.recall(scope, query)

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
pack = memory.recall(current, "What is the launch codename?")
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

You may use any model for the main agent turn. Citefold's only built-in model adapter is the optional [OpenRouter provider](providers/openrouter.md) for observation, consolidation, and embeddings. The `MemoryPack` is plain structured data plus Markdown, so it can be passed to other SDKs without a Citefold-specific dependency.

## Framework adapters

Official OpenAI Agents SDK, LangGraph, LlamaIndex, and MCP adapters are not shipped in the first alpha. Until stable semantics are proven, prefer the two explicit hooks above over a deep framework abstraction. Adapter contributions should preserve:

- full `MemoryScope` on every operation;
- recall before, ingest after;
- source/role assigned by trusted host code;
- coverage and conflicts visible to the agent;
- no automatic approval of model-generated candidates;
- citations kept with the claims they support.
