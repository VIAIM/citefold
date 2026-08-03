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
citefold status
citefold init
citefold doctor
citefold demo
```

The default `~/.citefold` path must be dedicated to Citefold state. Use a different `--root` for another application or test; do not put source media, uploads, logs, or unrelated files inside the memory root.

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

Pin an active record when it should be exempt from normal decay, then unpin it to resume decay:

```python
record_id = memory.list_records(scope)[0]["record_id"]
memory.pin(scope, record_id, reason="keep this preference stable")
memory.unpin(scope, record_id, reason="resume normal decay")
```

Pinning freezes the current access strength. After unpinning, decay resumes from that point rather than catching up over the pinned interval. Pinning does not make a record more trustworthy or prevent correction, archival, or deletion.

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

## Upgrade an existing v0.1 root

Normal memory operations fail closed when they detect the implicit schema 1 layout written by v0.1. Stop all v0.1 processes first, upgrade every writer together, and do not restart old code against the migrated root.

Inspect and preflight without writing:

```bash
citefold --root .citefold status
citefold --root .citefold migrate --dry-run
```

When the plan reports `ready: true`, run the explicit migration:

```bash
citefold --root .citefold migrate \
  --backup-to "$PWD/backups/citefold-before-v0.2.zip"
```

Citefold holds the legacy scope/ledger locks, verifies the ZIP backup, confirms that v0.1 files did not change, and then adds schema 2 root metadata. A concurrent change aborts without restoring older backup data over it. Interrupted migration recovery only cleans up or completes its own additive metadata. Rehearse this on a copy of your own data before production use. The checked-in v0.1 fixture proves the deterministic compatibility path locally, not every deployment or filesystem.

Read [Storage, migration, backup, and restore](storage.md) before operating on persistent data. It also documents standalone backups, restore intent-journal recovery with a retained `displaced_root`, Python APIs, and the local-POSIX locking boundary.

## Next steps

- Add memory to a turn loop with [Integrations](integrations.md).
- Register image, audio, and video evidence with [Multimodal](multimodal.md).
- Enable optional model operations with [OpenRouter](providers/openrouter.md).
- Rehearse upgrades and restore with [Storage](storage.md).
- Read [Security](security.md) before storing sensitive information.
