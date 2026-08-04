# OfficeLifeMemoryBench Track B Artifact Contract v1

Status: frozen private-artifact contract; controlled execution, adjudication,
scoring, latency qualification, and public projection are not implemented by
this validator.

## Purpose and version binding

This contract gives the Track B custodian a fail-closed, machine-verifiable
format for a sealed dataset and the inputs of a sealed run. It implements the
artifact requirements of the Track B protocol and
`OFFICELIFE_TRACK_B_EXECUTION_PROFILE_V1.md` without changing either document's
qualification gates.

Every record and manifest is bound to all three frozen identifiers:

- `contract_version = officelife-track-b-artifact-contract-v1`
- `protocol_version = 1.0`
- `execution_profile_version = officelife-track-b-execution-profile-v1`

The schemas are JSON Schema Draft 2020-12 files under
`schemas/officelife_track_b/v1/`. Their stable `urn:viaim:` references are
resolved only from the checked-in local registry; validation never retrieves a
schema from the network.

## Two physically separate bundles

The dataset root and run root MUST be distinct, access-controlled directories
outside the repository. Each manifest inventories every other regular file in
its own root. An undeclared file, a missing file, a duplicate role or path, a
case-folded path collision, a hard-linked file collision, a symlink, or a
non-regular file makes validation fail.

A dataset bundle has this fixed core:

```text
dataset-root/
├── dataset-manifest.json
├── users.jsonl
├── events.jsonl
├── task-inputs.jsonl
├── task-labels.jsonl
├── payloads/...
├── snapshots/...
└── governance/...
```

The four fixed file roles are `users`, `events`, `task-inputs`, and
`task-labels`. The first three have access class `generator_input`.
`task-labels` has access class `custodian_only`. The manifest also binds the
consent, de-identification, annotation, prohibited-identifier scan, access,
retention, withdrawal, and identity-mapping commitments required by the
execution profile.

Each task binds one pre-task snapshot through a complete artifact reference.
Its `snapshot_id` and inventory role are both `snapshot-<sha256>`, its bytes are
inventoried as a package, and its access class is `executor_input`, not
`generator_input`. This proves which snapshot bytes an executor must clone; it
does not prove that a later executor used them.

A run bundle has this fixed shape:

```text
run-root/
├── sealed-run-manifest.json
└── artifacts/...
```

The run manifest binds the exact dataset-manifest SHA-256, Citefold release and
distribution, code and dependency identities, storage/migration state, all
model roles and actual providers, prompts, tool definitions and schemas,
recent-context builder, pre-execution qualification plan, MemoryPack
configuration, generation/provider policy, randomization, and reference
environment. Every referenced run file has access
class `run_config` and is exhaustively inventoried.

The manifest binds bytes; it is not a signature. Custodian access controls and
the retained audit chain establish who was authorized to seal those bytes.

## Hidden-label boundary

`task-inputs.jsonl` is the only task record intended for the generator. Its
schema deliberately has no ground truth, expected answer, acceptable or
forbidden fact, relevant event, memory-requirement, scenario-family, scoring,
rating, treatment, or unblinding field. Unknown properties fail validation.

`task-labels.jsonl` is a separate custodian-only file. Input and label records
MUST form an exact one-to-one relation by `task_id`; their user and allowed
scope MUST agree, every task split MUST match its user split, and task/source
surface memberships are derived and checked exactly. A valid bundle proves
physical and structural separation. It does not prove that a future executor
mounted only generator-visible files; that requires the controlled executor
and file-access audit. The validator rejects whole-file, literal substring,
normalized-text, and nested-JSON copies of custodian-only or identity-vault
content in generator/executor inputs or run configuration. It also rejects
unreferenced exposed files and constrains event, task, recent-context, tool, and
snapshot inventory roles. Partial extraction, paraphrase, or other semantic
re-encoding still requires an independent leakage scan and human review, so
this is content- and structure-level separation rather than proof of semantic
non-leakage.

## Deterministic encoding and inventory

JSON manifests, JSON documents, and JSONL records MUST be UTF-8 without a BOM,
carriage return, duplicate key, non-finite number, or invalid JSON. JSONL
additionally requires:

- one object per physical line;
- canonical key ordering and compact separators;
- no blank lines; and
- one final LF.

Every inventory entry records a unique role and relative path, exact SHA-256,
exact byte size, media type, access class, artifact kind, schema version, and
record count. Non-JSONL entries use `record_count = 0`. Paths are ASCII,
root-relative POSIX paths without `..`, empty components, backslashes, URI or
drive prefixes, or NUL bytes.

The validator opens each path relative to an already opened real bundle root,
prohibits symlink traversal and every file with a link count other than one,
and uses the same file descriptor for size, SHA-256, decoding, and parsing. It
detects a file that changes during the read. Limits on file size, line size,
nesting depth, integer range, and reported errors keep hostile artifacts
bounded.

## Cross-file and lifecycle checks

Schema validation is followed by semantic validation. The validator recomputes
and checks, among other invariants:

- unique user, conversation, source-record, event, task, and artifact
  identities;
- user split, conversation, and scope ownership for events and tasks;
- exact input/label pairing and closure of facts, checks, and event references;
- strict `available_at < history_cutoff` eligibility;
- correction, supersession, and deletion targets within the same scope and
  without invalidation cycles, impossible timestamps, or cutoff drift;
- partial ASR as non-memory-bearing and non-recallable, never label evidence or
  a minimum-event contribution;
- source surfaces and product-surface memberships derived exactly from task and
  event records, plus evidence/check requirements for harm scenario families;
- required facts valid at task time, retained as required in every acceptable
  answer set, covered by typed and value-compatible must-pass checks, and all
  hard prohibitions covered by typed absence checks and closed into the success
  rule; fact, forbidden-item, inference, and non-memory-evidence subject IDs are
  pairwise disjoint within each label;
- content-addressed pre-task snapshots and exposed payload role/access binding;
- immutable provider-qualified model and route identities, provider-policy
  agreement, named-config uniqueness, deterministic retry/seed relationships,
  and no fallback route;
- declared users, unique tasks, slice memberships, families, requirements,
  harm tasks, unique memory-bearing payloads, and timeline ranges from the
  actual records; and
- the hidden-test minimum dataset gates frozen in the execution profile.

For structural counting, tasks under the same user and canonical allowed scope
share one counting unit when they bind the same pre-task snapshot content.
Changing task IDs, paths, timestamps, conversations, surface labels, prompts,
or prompt attachments cannot turn one frozen pre-task state into multiple
counted tasks. Eligible events are grouped per user into connected components:
events sharing any normalized payload fingerprint belong to one component, so
adding an attachment cannot increase the count. Text fingerprints use Unicode
NFKC, case-folding, and whitespace collapse; JSON additionally uses canonical
structure and normalized string values; non-text bytes use their exact digest.
Empty normalized text does not form a counting unit. Per-user event counts and
the 14-day span use those components, with the earliest event time representing
each component.

These are conservative normalized structural lower bounds. They prevent exact
clones and common mechanical variants from satisfying cohort minimums, but do
not establish semantic sample diversity or annotation quality; those require
custodian review and the later audit chain.

An invalidated dataset remains structurally inspectable but cannot pass for new
use. `--enforce-minimum-dataset-gates` additionally makes the frozen cohort
minimums mandatory. Neither status evaluates arm outputs or any Track B effect
or safety gate.

## Validation commands

The validator is shipped in the source distribution, not the runtime wheel.
Use a source checkout or extracted sdist with the development dependencies:

```bash
python -m pip install -e ".[dev]"

python -m benchmarks.officelife_track_b_contract validate-dataset \
  /private/dataset-root \
  --enforce-minimum-dataset-gates \
  --output-json /private/reports/dataset-contract.json

python -m benchmarks.officelife_track_b_contract validate-run \
  /private/dataset-root \
  /private/run-root \
  --enforce-minimum-dataset-gates \
  --output-json /private/reports/run-contract.json
```

The output path MUST be outside both private input roots. Exit codes are:

- `0`: the requested validation passed;
- `3`: structure is valid, but dataset status or an enforced minimum gate did
  not pass;
- `4`: schema, identity, inventory, lifecycle, or run binding is invalid.

Reports contain roles, line numbers, JSON Pointers, and stable error codes, not
record values or local input paths. Reports are nevertheless marked `private`
and `claimable = false`; they are not public-result artifacts.

## What passing does not prove

A passing contract report proves that the supplied bytes match the declared
schemas, inventory, references, frozen versions, and implemented semantic
invariants at validation time. It does not prove:

- that consent, de-identification, independent custody, or provider policy is
  truthful or complete;
- that normalized task/event counting establishes semantic sample diversity;
- that generator processes could not read hidden labels;
- that the paired arms were executed from isolated identical snapshots;
- that outputs were blinded, adjudicated, or scored correctly;
- that the recall-latency profile passed;
- that an audit bundle is complete; or
- that Citefold achieved any Track B product-effect or release gate.

Those claims require the later controlled executor, adjudication/scoring,
latency runner, independent review, and allowlist-based public projector. Until
that full chain exists and a qualifying consented hidden run completes, Citefold
has no trustworthy Track B result.
