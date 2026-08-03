# OfficeLifeMemoryBench v1 Protocol

Status: protocol frozen; dataset collection pending.

## Purpose

OfficeLifeMemoryBench is the product-validity layer for a personal office/life agent. It answers a different question from LongMemEval:

> With the same agent, model, current task, visible conversation, and tools, does an evidence-backed `MemoryPack` improve task completion over `No Memory` without increasing false memories, privacy leakage, or stale actions?

LongMemEval remains the public capability baseline. OfficeLifeMemoryBench must not be presented as a public leaderboard or used to compare unrelated systems.

## Evaluation Tracks

### Track A: Deterministic Regression

The existing 24-probe synthetic suite remains a fast engineering check for:

- preference recall
- open task and waiting-on recall
- people follow-up
- meeting and final voice transcript recall
- ASR partial-transcript exclusion
- no-evidence behavior
- tenant/user/namespace isolation

Track A can block a release, but it cannot prove product usefulness.

### Track B: Controlled Product Benchmark

Track B is the v1 product benchmark. It uses consented, de-identified longitudinal histories collected from realistic office/life workflows. Histories may come from a controlled user study or an opt-in pilot, but the final test set must not be authored or tuned against by the memory implementation team.

Minimum dataset gate:

- at least 30 distinct users
- at least 14 days of history per user
- at least 50 memory-bearing events per user
- at least 10 evaluation tasks per user
- at least 300 scored tasks total
- no real passwords, government identifiers, financial account numbers, or raw private contact details

Users, not conversations, are the split unit. A user's events may appear in exactly one of `development`, `validation`, or `hidden_test`.

### Track C: Longitudinal User Study

Track C measures user trust and repeated use in a pilot. It is reported separately from Track B because users, interfaces, and behavior adaptation make it non-deterministic.

## Product Surfaces

Every Track B release must report results separately for:

| Surface | Required behavior |
|---|---|
| Text chat | Recall user facts, decisions, tasks, corrections, and preferences from prior chats. |
| Realtime voice | Use final transcripts; partial ASR hypotheses must not become durable personal memory. |
| Third-party agents | Unapproved candidates must not be recalled; approved memory must retain source provenance. |
| Cross-channel | A fact created in one surface can support a later task in another surface under the same user scope. |

No surface may be represented by fewer than 50 hidden-test tasks. Cross-channel tasks may count toward both their source surface and the cross-channel slice, but the aggregate score counts each task once.

## Scenario Families

The hidden test set must cover all of these families:

1. Stable preferences and working style
2. Open loops, deadlines, waiting-on items, and completion state
3. People, teams, and follow-up context
4. Meeting decisions and action items
5. Temporal updates and superseded facts
6. Corrections, contradictions, and uncertainty
7. No-evidence questions where abstention is correct
8. Scope isolation across users, tenants, namespaces, and connected agents
9. Deletion or forget requests
10. Cross-channel recall from text, voice, and third-party sources

At least 20% of hidden-test tasks must be no-evidence, stale-memory, correction, deletion, or isolation cases. These are product-harm checks, not optional adversarial extras.

## A/B Protocol

Each task produces two blinded runs:

- `no_memory`: the agent receives the current task and the same allowed recent context, but no long-term memory output.
- `memory_pack`: the agent receives the identical inputs plus the `MemoryPack` produced before answering.

An optional `full_history_oracle` may be reported as a diagnostic ceiling but is not part of the primary lift claim.

Controls:

- same model and fixed model version
- same system prompt, tools, tool outputs, and current conversation
- same generation settings and retry policy
- identical task ordering policy
- randomized A/B presentation order
- evaluator does not receive the treatment label
- provider, model, prompt hash, code commit, dataset hash, and configuration are recorded

The memory system may use only events available before the task timestamp. Ground-truth answers, answer-session labels, evaluator notes, and future events must never enter ingestion or recall.

## Labels and Judging

Each task includes:

- acceptable answer facts and explicitly forbidden facts
- whether memory is required, optional, or should be absent
- relevant source event ids and valid time interval
- superseded or deleted event ids
- allowed user and tenant scope
- category and product surface

Deterministic checks score exact entities, dates, state transitions, citations, and forbidden facts. Open-ended usefulness is judged by two blinded human raters. Disagreements go to a third rater. An LLM judge may be reported as a secondary reproducibility signal, never as the sole product-validity label.

## Metrics

Primary:

- task success rate for `memory_pack` and `no_memory`
- absolute task success lift
- clustered 95% bootstrap confidence interval, resampled by user

Safety and trust:

- unsupported-memory rate
- no-evidence false-answer rate
- stale or superseded fact rate
- deletion violation count
- cross-scope leakage count
- citation precision and source coverage
- ASR partial-transcript durable-commit count

Efficiency:

- ingest latency p50/p95
- recall latency p50/p95
- context characters and provider-token count
- reader cost and total cost per successful task

All metrics are reported overall and by scenario family, product surface, history length, and user. Retrieval metrics may be included as diagnostics but must remain separate from end-to-end task success.

## Provisional Release Gates

A memory version passes Track B only when all conditions hold on `hidden_test`:

- task success lift is at least 10 percentage points
- the user-clustered 95% confidence interval for lift is above zero
- memory-required task lift is at least 15 percentage points
- no-evidence task success is no worse than `no_memory` by more than 1 percentage point
- unsupported-memory rate is at most 2%
- stale or superseded fact rate is at most 2%
- cross-scope leakage and deletion violations are both zero
- ASR partial-transcript durable commits are zero
- recall latency p95 is at most 300 ms at 1,000 stored events for one user

These gates are provisional until the first pilot establishes variance and annotation reliability. Gate changes require a protocol version bump and cannot be made after inspecting hidden-test results.

## Contamination Controls

- Freeze every dataset release with a manifest and SHA-256 hashes.
- Keep `hidden_test` inaccessible to implementation and prompt tuning.
- Tune only on `development`; use `validation` for release candidates.
- After a hidden-test run, changes create a new system version and consume a new test release or an explicitly recorded evaluation iteration.
- Store hypotheses, MemoryPack traces, judge outputs, and failure labels for audit.
- Report failed or reverted strategies, not only the selected result.

## Current Status

Track A is implemented in `officelife_memory_benchmark.py`. Track B now has a
frozen execution profile, the versioned
`officelife-track-b-artifact-contract-v1` dataset/sealed-run schemas, a
fail-closed private structural validator, and a legacy post-run aggregate
calculator. The tools do not collect data, enforce the controlled executor's
file-access boundary, execute paired agent arms, obtain blinded human ratings,
run the controlled latency assay, verify a complete audit bundle, or establish
product lift. No qualifying consented hidden dataset or trustworthy Track B
product-effect score exists yet. The next step is independent custody,
consented collection and annotation, followed by controlled execution,
adjudication, latency measurement, public projection, and complete audit.
