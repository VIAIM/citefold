# OfficeLifeMemoryBench Track B Executor v1

Status: private controlled-execution draft. The callable adapter and opaque
snapshot adapter implemented in this version are test-only and never produce a
qualifying or claimable Track B result.

## Scope

This executor implements the byte-level handoff between the frozen Track B
dataset/run contract and later adjudication. It performs no model or network
call itself. A supplied handler may stand in for a real Agent, but the only
handler protocol implemented here is an in-process Python callable for offline
contract tests.

The executor does not score answers, read hidden labels during generation,
adjudicate ratings, measure the independent latency gate, project a public
result, or mark a run qualified. Raw inputs, answers, MemoryPacks, traces,
unblinding maps, and reports remain private.

## Required two-process boundary

Execution has two explicit phases:

1. `prepare_worker_bundle(dataset_root, run_root, worker_root)` runs in the
   custodian environment. It validates the complete dataset and sealed run,
   including hidden labels, and then writes a new exhaustive worker bundle.
2. `execute_worker_bundle(worker_root, execution_root, handler)` runs only
   against that worker bundle. A formal operator MUST invoke it in a new process
   whose operating-system identity or sandbox cannot open the original dataset
   or custodian roots.

The worker bundle contains only hidden-test `task-inputs`, their referenced
task/recent-context/tool/snapshot artifacts, an allowlisted projection of the
sealed run, and the exact run artifacts needed by the Agent. It excludes
`task-labels`, global event/user files, evaluator-prompt bytes, governance files,
identity mappings, and every unreferenced dataset artifact. Excluding the global
event file also prevents a handler from reading events after a task cutoff.

The full source sealed-run manifest is retained in the worker bundle as a
hash-bound configuration commitment. The worker validator reconstructs the
allowlisted run projection from that manifest and the inventoried executor
config and requires exact structural equality. Rewriting generation, provider,
model, randomization, MemoryPack, prompt/tool references, Agent identity, or
handler settings therefore fails even if an attacker also recalculates the
worker inventory.

The Python callable does not create an OS security boundary. Every callable run
records all of these nonqualification reasons where applicable:

- `callable_handler_test_only`
- `opaque_snapshot_adapter`
- `filesystem_isolation_not_enforced`
- `controlled_executor_draft`

`external-process-v1` is reserved in the config schema, but is not implemented.
The callable entrypoint rejects it rather than silently treating it as isolated.

## Frozen executor config

The sealed run MUST inventory exactly one `track-b-executor-config` file with
schema `officelife-track-b-executor-config-v1`. It freezes:

- `handler_id` and `handler_protocol`;
- `snapshot_adapter_id`;
- trusted `agent_id` and `session_id_policy`;
- required worker-isolation level; and
- `executor_contract_version = officelife-track-b-executor-v1`.

The current implementation accepts only
`snapshot_adapter_id = opaque-pass-through-v1`. It copies snapshot bytes into
each arm workspace and never guesses, extracts, or imports an opaque archive.
This closes artifact identity and clone independence, but cannot prove the
snapshot's internal Citefold root, full `MemoryScope`, or eligible state. It is
therefore always nonqualifying. A later qualifying adapter needs a frozen safe
package format, extraction rules, internal manifest, and scope mapping.

`agent_id` and `session_id_policy = sha256-task-v1` are frozen now so a later
adapter cannot silently invent the two `MemoryScope` fields absent from the task
input. The opaque adapter does not interpret them.

## Paired execution

Tasks run in their canonical `task-inputs.jsonl` order. Only
`task_order_policy = fixed-dataset-order` is accepted.

For task `task_id`, arm order is:

1. key: the base-10 ASCII encoding of the sealed integer randomization seed;
2. message: UTF-8 fields joined by a single NUL byte in this exact order:
   `officelife-track-b-arm-order-v1`, `run_id`, `iteration_id`, `task_id`;
3. digest: HMAC-SHA-256;
4. if the low bit of digest byte zero is `0`, order is
   `no_memory, memory_pack`; otherwise it is reversed.

The order is computed before outputs and never changes in response to content
or failure.

Both arms bind the same canonical paired-input digest over the task record,
AgentTurn contract, Citefold identity, reader model, prompt/tool references,
MemoryPack settings, generation policy, and provider policy. Each attempt gets
fresh regular-file copies of the task input, recent context, tool fixture, and
the exact same content-addressed snapshot. Workspaces have different paths and
inodes and are removed after finalization. Source worker artifacts are hashed
again before each copy.

The only treatment marker is:

- `no_memory`: the handler MUST return no MemoryPack;
- `memory_pack`: the handler MUST return one pre-answer MemoryPack artifact.

Unexpected or missing treatment artifacts become infrastructure errors. The
handler is frozen by `handler_id`; it receives one task/arm workspace and no
dataset, run-root, label, evaluator, or unblinding path.

The executor never calls `Citefold.complete_agent_turn()`. Benchmark answers and
their derived data are not written back into a task snapshot or shared history.

## Provider, retry, and outcome semantics

The sealed run MUST use:

- one enabled reader with immutable model and route;
- `fallback_policy = none` and an empty fallback-route list;
- `hmac-sha256-v1` arm order;
- `uuid-v4-v1` blinded output IDs; and
- retry count and backoff list already closed by the artifact validator.

A successful handler result reports actual model ID, immutable model version,
upstream provider, and immutable route. All four MUST exactly equal the frozen
reader configuration. Missing or different identity, fallback use, invalid
treatment, or invalid output is an infrastructure error and follows the same
frozen retry schedule in both arms. A returned value that has the wrong Python
type, fails its schema or JSON encoding, or would exceed the frozen artifact
envelope becomes retryable `handler_output_invalid`; the invalid payload itself
is never persisted. A Python exception raised by the handler remains a distinct
terminal failure.

The callable invokes one attempt at a time; handler-internal retries are outside
this contract and prohibited in a formal adapter. A handler may raise
`HandlerInfrastructureError` with a safe preregistered category. Unexpected
Python exceptions are redacted to `unclassified-handler-exception`, are not
retried, and make the iteration incomplete.

Outcomes are:

- `answer` and `refusal`: present output with non-empty content;
- `product_timeout`: present output with no content, permitted only when
  `product-timeout` is preregistered;
- `product_failure`: present output with no content and a preregistered category;
- `infrastructure_error`: no scoreable output after frozen retries or an
  unclassified failure.

Any infrastructure-error arm makes the execution `incomplete`. It is retained,
not converted to task failure, deleted, replaced, or used to produce a gate
conclusion.

## Artifact and scale envelope

The label-free worker preserves the source contract's task-input limits: 256
MiB for `task-inputs.jsonl` and 1 MiB per source record. Other individual JSON
documents, including an attempt receipt, arm result, and private trace, are
limited to 16 MiB each.

Each execution aggregate is limited to 128 MiB. Before any handler call, the
executor divides that capacity by the frozen number of arms and requires at
least 4 KiB per arm. Both the unblinded arm row and its blinded row must fit that
per-arm budget before a successful handler receipt is written. For the frozen
minimum Track B dataset of 300 tasks and 600 arms, the budget is 223,696 bytes
per row. The current hard capacity is 32,768 arms; larger worker bundles fail
before an execution directory or external action is created.

All four aggregate byte strings are constructed and checked before the first
aggregate file is replaced. Oversized handler data therefore follows the frozen
invalid-output retries and finishes as a small, valid `incomplete` bundle rather
than creating an artifact that its own validator cannot read. These are safety
limits for this draft, not throughput or production-scale measurements.

## Recovery and idempotency

Execution IDs are deterministic over executor version, run, iteration, task,
and arm. Attempt IDs and idempotency keys are deterministic over execution ID
and attempt index. A randomized blinded output ID is written to immutable
`request.json` before the first handler call.

Before the first arm, the executor writes `execution-binding.json`, which binds
the exact worker-manifest hash and frozen handler configuration. Every resume
must match it. The executor then preflights every existing arm—not only the next
arm in task order—before making any new handler call. This prevents a resumed
run from mixing outputs from different worker bundles or discovering a later
conflict only after an earlier arm has already caused a new external action.

Each attempt writes one immutable receipt. On restart:

- a valid completed arm result is reused without calling the handler;
- a completed handler receipt without a final arm result is finalized without
  another handler call;
- an infrastructure receipt advances only according to the frozen retry list;
- any conflicting request, attempt, trace, or result fails closed.

This provides local crash recovery and stable request idempotency. It does not
promise exactly-once behavior from a remote provider. A formal external handler
must pass the idempotency key to any provider mechanism that supports it and
retain every provider attempt.

Only one executor may own an execution root at a time. A persistent sibling
regular file carries a non-blocking POSIX advisory lock; process exit releases
the lock even after an unclean crash, so a stale unlocked file does not block
recovery. The file is mode `0600`, is not part of the final bundle, and is never
unlinked during normal lock release because replacing its inode could admit two
owners. This behavior is supported only on the project's declared local macOS
and Linux boundary, not Windows or network filesystems. A complete execution
manifest is immutable; rerunning the entrypoint validates and returns it without
invoking the handler.

## Private artifacts

```text
execution-root/
├── execution-manifest.json
├── execution-binding.json
├── arm-outputs.jsonl
├── blinded-outputs.jsonl
├── unblinding-map.jsonl
├── execution-audit.jsonl
├── arms/<execution-id>/request.json
├── arms/<execution-id>/attempts/<attempt-index>.json
├── arms/<execution-id>/result.json
└── traces/<execution-id>.json
```

The execution manifest exhaustively inventories every other regular file by
path, role, byte size, and SHA-256. Symlinks, hard links, non-regular files,
undeclared files, missing files, case-folded path collisions, and changed bytes
fail validation.

The execution binding is the first immutable file in a new execution root. A
partial directory containing other files without that binding fails closed.

`arm-outputs.jsonl` is custodian-only and unblinded. It records task/arm/order,
paired-input and request hashes, randomized output ID, outcome, attempt count,
actual provider identity, usage, error category, and the exact private trace
reference.

`blinded-outputs.jsonl` contains only schema version, randomized output ID,
outcome, and rater-visible content. It contains no task ID, treatment, order,
model/route, MemoryPack coverage, trace, or MemoryPack length.

`unblinding-map.jsonl` is the unique bijection from randomized output ID to
task, arm, order, and execution ID. It is never a rater or public artifact.

`execution-audit.jsonl` contains one `arm_finalized` event per arm. Each event
binds the canonical stored arm result, previous event hash, and its own hash.
The manifest binds the final chain head. Deleting or reordering events, changing
their task/arm identity, or detaching an event from its stored result fails
semantic validation even if file inventory hashes are recalculated.

## Claim boundary

`validate_worker_bundle()` and `validate_execution_bundle()` establish local
schema, inventory, projection, pairing, blinding-map, trace-reference, and audit
chain closure at validation time. They do not establish:

- OS denial of the source custodian root;
- correct interpretation of an opaque snapshot;
- lack of hidden retries, network access, caches, or external side effects in a
  callable handler;
- correctness or usefulness of answers;
- deterministic checks or human ratings;
- Track B latency or statistical gates; or
- consent, independent custody, qualification, or a publishable result.

The local hashes and audit chain are consistency evidence, not signatures. A
party able to rewrite every execution artifact and reseal the manifest can also
replace otherwise schema-valid answer content. A qualifying run therefore needs
independent custody and external artifact authentication in addition to this
semantic validator.

Every artifact produced by this draft is `private`, `claimable = false`, and
`qualification_eligible = false`.
