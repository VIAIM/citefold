# Quickstart

This path stays local and makes no network or model calls.

## Install from PyPI

Create an isolated environment and install the alpha release:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install citefold
```

Verify the installed CLI:

```bash
citefold --help
citefold init
citefold doctor
citefold demo
```

## Install from source

Clone the repository to run the checked-in examples or contribute:

```bash
git clone https://github.com/VIAIM/citefold.git
cd citefold
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the deterministic Python example:

```bash
python examples/quickstart.py
```

## Ingest and recall

```python
from citefold import Citefold, MemoryScope

memory = Citefold(".citefold")
scope = MemoryScope(
    tenant_id="acme",
    user_id="alex",
    namespace="work",
    agent_id="copilot",
    session_id="launch-planning",
)

result = memory.ingest_text(
    scope,
    "The launch codename is ORCHID-77. Send the launch brief on Friday at 10:00.",
    source="chat",
)
print(result.observation_ids)

pack = memory.recall(
    scope,
    "What is the launch codename and when should I send the brief?",
    token_budget=800,
)
print(pack.coverage)
print(pack.markdown)
```

This retrieves the recorded episode and includes an `observation:` citation. It does not require the sentence to become a long-term semantic record first.

## Promote a generic long-term claim

The built-in deterministic direct-write parser is intentionally narrow. For arbitrary languages and domains, submit a supported candidate (or use optional model consolidation), inspect it, and approve it explicitly:

```python
event = memory.ingest_text(
    scope,
    "Alex prefers project updates on Friday afternoon.",
    source="chat",
)
evidence_ref = f"observation:{event.observation_ids[0]}"

proposal = memory.consolidate(
    scope,
    candidates=[
        {
            "memory_type": "semantic",
            "content": "Alex prefers project updates on Friday afternoon.",
            "evidence_refs": [evidence_ref],
            "confidence": 1.0,
            "proposed_operation": "ADD",
        }
    ],
)[0]
assert proposal.status == "pending"

memory.approve_candidate(scope, proposal.candidate_id)
```

The same review is available from the CLI:

```bash
citefold candidates list --status pending
citefold candidates approve candidate_example_id
citefold candidates reject candidate_example_id --reason 'not a durable user fact'
```

## Use local defaults or explicit scope

For a local trial, the CLI defaults to `~/.citefold`, tenant `local`, user `me`, namespace `personal`, agent `citefold-cli`, and session `default`:

```bash
citefold init
citefold demo
citefold recall 'What is the launch codeword?' --markdown
```

For an application or multi-user environment, set every value explicitly. Environment variables keep commands readable:

```bash
export CITEFOLD_ROOT="$PWD/.citefold"
export CITEFOLD_TENANT_ID=acme
export CITEFOLD_USER_ID=alex
export CITEFOLD_NAMESPACE=work
export CITEFOLD_AGENT_ID=copilot
export CITEFOLD_SESSION_ID=launch-planning

citefold ingest-text 'The launch codename is ORCHID-77.' --source chat
citefold recall 'What is the launch codename?' --markdown
```

Command-line identity options are global and must appear before the subcommand:

```bash
citefold \
  --root .citefold \
  --tenant-id acme \
  --user-id alex \
  --namespace work \
  --agent-id copilot \
  --session-id launch-planning \
  recall 'What is the launch codename?'
```

## Next steps

- Add memory to a turn loop with [Integrations](integrations.md).
- Register image, audio, and video evidence with [Multimodal](multimodal.md).
- Enable optional model operations with [OpenRouter](providers/openrouter.md).
- Read [Security](security.md) before storing sensitive information.
