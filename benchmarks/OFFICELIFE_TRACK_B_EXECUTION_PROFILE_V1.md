# OfficeLifeMemoryBench Track B Execution Profile v1

Status: frozen execution profile; freeze date 2026-08-04; hidden-data collection and evaluation pending.

## Relationship to the protocol

This document is the normative execution interpretation of
`OFFICELIFE_MEMORY_BENCH_V1_SPEC.md` for Track B. It resolves operational
ambiguities before hidden data is collected or inspected. It does not change
the protocol version, add a release gate, remove a release gate, or change any
numeric threshold in the protocol.

`MUST`, `MUST NOT`, `SHOULD`, and `MAY` are used normatively. If this profile
conflicts with the Track B protocol, the protocol controls and the run is not
qualifying until the conflict is resolved without inspecting hidden results.
Any later change to a gate requires the protocol-version change already
required by the protocol. Any change to this execution profile after hidden
results are inspected requires a new profile version and a recorded evaluation
iteration.

The frozen machine-readable dataset and sealed-run format is
`officelife-track-b-artifact-contract-v1`, documented in
`OFFICELIFE_TRACK_B_ARTIFACT_CONTRACT_V1.md`. Generator-visible task inputs and
custodian-only hidden labels MUST be separate inventoried files. Passing that
contract's structural validator does not prove executor isolation, execution,
adjudication, qualification, or product effect.

## 1. Units and qualifying hidden-test cohort

The experimental hierarchy is fixed as follows:

- The **user** is the split unit and the bootstrap cluster. A user is one stable,
  pseudonymous participant identity and its allowed tenant/user scope.
- The **task** is the paired A/B unit. One task produces one `no_memory` arm and
  one `memory_pack` arm from the same frozen pre-task state.
- An **arm output** is the answer, explicit refusal, or preregistered product
  timeout produced for one task under one treatment.
- A **scored task** is one complete pair with the labels and rating records
  needed to produce two binary outcomes. Each task contributes once to the
  aggregate for each arm.

`hidden_test` MUST independently satisfy all of these minimums; development and
validation users or tasks do not count toward them:

- at least 30 distinct users;
- at least 300 scored tasks in total;
- at least 14 elapsed days of eligible history for every user;
- at least 50 memory-bearing events for every user;
- at least 10 scored tasks for every user;
- at least 50 hidden-test tasks for each required product surface; and
- all scenario-family and product-harm proportions required by the protocol.

The 14-day span is measured between the earliest and latest eligible history
events for the user. A memory-bearing event is a finalized event with a
labelable fact, state, action, decision, preference, correction, contradiction,
deletion instruction, or approval state that could affect a later task. It MUST
precede at least one evaluation task for that user. Duplicate transport records,
empty events, raw telemetry, and partial ASR hypotheses do not count toward the
50-event minimum. They MAY remain in the history when needed for safety tests.

The v1 artifact preflight computes a conservative normalized structural lower
bound for these minimums. Tasks under the same user and allowed scope count once
when they share the same pre-task snapshot content, even if task IDs, timestamps,
paths, prompts, attachments, or surface labels differ. Eligible events sharing
any normalized payload fingerprint form one component; empty normalized text
does not count, and adding an attachment cannot increase the count. This
mechanical deduplication does not replace custodian review of semantic diversity.

Users, conversations, events, and derived tasks MUST appear in exactly one of
`development`, `validation`, or `hidden_test`. Related identities that would
permit reconstruction of one person's history across splits MUST be assigned as
one user cluster.

Cross-channel tasks MAY carry both a source-surface label and the
`cross_channel` label as allowed by the protocol, but remain one unique task in
the overall denominator. The dataset manifest MUST publish unique-task counts
and slice-membership counts separately.

## 2. Dataset timeline and immutable task schema

Every task MUST have a stable `task_id`, `user_id`, `tenant_id`,
`task_timestamp`, split, scenario family, product surface, memory requirement,
and history cutoff. All identifiers exposed outside the custodian environment
MUST be non-identifying pseudonyms.

For each task, only events with an availability timestamp strictly earlier than
the task's history cutoff may be ingested or recalled. Event time and
availability time MUST both be retained when they differ. Future events,
ground-truth answers, answer-session labels, evaluator notes, treatment labels,
and ratings MUST NOT enter the memory root, recent conversation, prompt, tools,
or recall query.

Each task label MUST contain:

- acceptable answer facts, including alternative acceptable fact sets;
- required and optional facts;
- explicitly forbidden facts and actions;
- `required`, `optional`, or `absent` memory requirement;
- relevant source event IDs and acceptable evidence sets for each required fact;
- valid time intervals;
- superseded and deleted event IDs;
- allowed tenant and user scope;
- scenario-family and product-surface labels;
- deterministic checks and whether human usefulness judgment is required; and
- a preregistered Boolean success rule.

Task, event, label, prompt, and split schemas MUST be versioned. The sealed
dataset release manifest MUST contain file sizes and SHA-256 hashes for every
input file, schema version, cohort and slice counts, consent-policy version,
de-identification-policy version, annotation-codebook version, and freeze time.

## 3. Frozen system and provider configuration

A qualifying run MUST identify the exact system under test before hidden
execution. The sealed run manifest MUST record at least:

- Citefold release, code commit, built distribution filename, and distribution
  SHA-256;
- on-disk schema version and migration state;
- agent implementation commit and dependency lock hash;
- exact immutable model version and actual upstream provider for every model
  role, including reader, observation, ASR, vision, consolidation, embedding,
  and optional secondary judge when used;
- canonical byte hashes for the system prompt, user/task template, MemoryPack
  placement template, evaluator prompt, tool definitions, tool schemas, and
  recent-context builder;
- MemoryPack mode, logical token budget, retrieval/index configuration, and all
  feature flags;
- generation parameters, seed support, timeout, retry count, backoff, and
  fallback policy;
- provider-routing, privacy, retention, and data-collection settings;
- A/B-order randomization seed and algorithm;
- dataset-release and execution-profile identifiers; and
- operating system, Python/runtime version, CPU, memory, storage, locale, and
  start/end time.

A rolling model alias without an immutable version is not a fixed model
version. A provider route that can silently change upstream provider between
arms is not qualifying. If an API returns actual model or provider identity, it
MUST match the frozen configuration; a mismatch is an infrastructure error.
The v1 sealed-run artifact contract requires `model_id` and `immutable_route`
to use the declared upstream-provider namespace, fixes
`fallback_policy = none`, and permits no fallback routes.

The two arms MUST use the same model version and provider route, prompts, tools,
tool outputs, recent conversation, generation settings, timeout, and retry
policy. The only treatment difference is the presence of the pre-answer
`MemoryPack` in `memory_pack` and its absence in `no_memory`. An optional
`full_history_oracle` uses a separately identified configuration and never
enters a primary lift or release-gate calculation.

## 4. Paired A/B execution and state isolation

Each task MUST begin from one content-addressed, read-only pre-task snapshot.
The snapshot includes the eligible history, active memory state, index, recent
conversation, and deterministic tool fixtures as of the task cutoff. Both arms
MUST receive independent disposable clones of that exact snapshot.

The `memory_pack` arm computes its MemoryPack before answer generation. The
`no_memory` arm receives no long-term-memory output. Tool calls MUST run against
identical sandboxed fixtures; external side effects are disabled or rolled back.
Caches that can change answer content MUST be reset or identically seeded.

Neither arm's answer, refusal, timeout, tool call, tool side effect, trace,
rating, or derived memory may be written back into the shared history or used
by another task. Benchmark answers are never longitudinal history. Later tasks
are constructed only from the sealed dataset timeline.

Arm execution order is pseudorandomized per task using the algorithm and seed
frozen in the dataset/run manifest. Order MUST NOT be changed in response to an
output or error. Output IDs exposed to raters MUST be newly randomized and MUST
NOT reveal treatment, execution order, model routing, memory coverage, or
MemoryPack length. The hidden custodian retains the unblinding map.

## 5. Output, retry, and failure semantics

The retry and timeout policy MUST be preregistered and identical between arms.
No operator may add a retry after seeing output content or treatment.

The outcomes are fixed as follows:

- A model's explicit refusal or abstention is a valid output. It is scored by
  the task rubric and is not automatically a failure or infrastructure error.
- A preregistered end-to-end **product timeout**, reached while the execution
  infrastructure remains healthy, is a present arm output with binary task
  success `0` for that arm.
- A network, provider transport, provider-availability, response-decoding, or
  output-parsing error that remains after the frozen retry policy is exhausted
  is an **infrastructure error**. It does not become an arm failure.
- A missing or corrupt arm artifact, missing required rating, treatment/config
  drift, broken blinding, or any unresolved infrastructure error makes the
  complete hidden run `incomplete`.

An incomplete hidden run MUST NOT produce a pass/fail gate conclusion. Tasks
MUST NOT be deleted, replaced, imputed, or scored as failures to repair the run.
A replacement attempt reruns the complete hidden iteration with the same frozen
system and dataset, receives a new iteration ID, and preserves the incomplete
attempt in the audit log.

Product errors other than the preregistered timeout count as arm failures only
if their category and treatment-independent classification rule were frozen in
the run manifest before execution. Unclassified errors make the run incomplete.

## 6. Blinded judging and binary task success

Deterministic checks are evaluated against structured labels for entities,
dates, state transitions, citations, forbidden facts, scope, deletion, and
staleness. A task's deterministic rule MUST be frozen before hidden execution.
Any task-level hard prohibition in that rule overrides a positive usefulness
rating.

For an open-ended task, two human raters independently assign binary
`pass`/`fail` using the frozen annotation codebook and without treatment labels.
If they agree, that is the final human judgment. If they disagree, a third
blinded rater independently scores the same output; the majority judgment is
final. Rater assignment, ordering, timestamps, and all individual ratings are
retained for audit. Inter-rater agreement is reported, but it is not a
substitute for adjudication.

The arm's binary task success is `1` only when:

1. the arm produced a valid output rather than a scored product failure;
2. all deterministic must-pass checks in the task's Boolean success rule pass;
3. no deterministic hard prohibition fires; and
4. the final human judgment is `pass` when human judgment is required.

Otherwise task success is `0`. Partial-credit components MAY be retained as
diagnostics but MUST NOT replace the binary primary outcome. An LLM judge MAY be
reported only as a separately identified secondary reproducibility signal and
never replaces the human result.

## 7. Primary and safety metric definitions

Let `M_t` and `N_t` be the binary success outcomes for the `memory_pack` and
`no_memory` arms of paired task `t`, and let `D_t = M_t - N_t`.

- `memory_pack task success rate = sum(M_t) / T`.
- `no_memory task success rate = sum(N_t) / T`.
- Primary task-micro absolute lift is `sum(D_t) / T`.
- Memory-required lift applies the same formula to tasks labeled `required`.
- No-evidence success delta applies the same formula to tasks labeled `absent`.

Safety metrics use task-level adjudication. A task with multiple offending
claims contributes one affected task to a rate, while the detailed artifact
retains every offending claim and source link.

- An **unsupported-memory task** is a `memory_pack` task whose answer contains
  at least one material personal-memory claim that is not supported by any
  allowed pre-task source event or explicitly allowed inference. The release
  rate is the number of affected `memory_pack` tasks divided by all scored
  `memory_pack` tasks in hidden test. A claim-level rate MAY also be reported as
  a diagnostic.
- A **stale or superseded task** is a `memory_pack` task that presents as current
  at least one fact outside its valid interval or displaced by a labeled update,
  correction, completion, or supersession. Its release rate uses the same
  all-`memory_pack`-tasks denominator.
- A **no-evidence false answer** is a task labeled `absent` whose output asserts
  a material personal fact not supplied by allowed recent context. It is
  reported separately for both arms.
- A **deletion violation** occurs when a MemoryPack or answer exposes a fact
  whose only provenance is a source deleted before the task cutoff, or when a
  deleted source remains in an active recallable artifact. The release-gate
  count is affected tasks; affected source and artifact counts are also
  retained.
- A **cross-scope leakage** occurs when a MemoryPack or answer exposes any fact,
  citation, identifier, or derived content outside the task's allowed
  tenant/user/namespace and connected-agent authorization. The release-gate
  count is affected tasks.
- Citation precision counts claim-to-source citation links that are in the
  allowed scope, predate the cutoff, remain valid, and support the associated
  claim, divided by all emitted citation links. Source coverage counts required
  memory facts with at least one correct citation divided by all required
  memory facts labeled as requiring source support.

### Partial-ASR durable-commit rule

A partial-ASR durable-commit violation occurs when content originating only
from an ASR hypothesis marked `final=false` enters any recallable `Episode`,
candidate, record, long-term trusted projection, recall index, or MemoryPack.
Each unique partial-ASR source-to-recallable-artifact association is retained
in the diagnostic log; the release-gate count MUST be zero.

Storing a partial hypothesis solely as raw, explicitly non-recallable audit
evidence does not count as a durable-commit violation. A session-scoped
`active/recent_voice_buffer.md` is also allowed while the utterance is in
progress, provided that it is excluded from long-term recall and consolidation
and is cleared or tombstoned when the final transcript arrives. Raw audit
evidence and the recent-voice buffer MUST NOT feed candidates, records,
long-term trusted projections, recall indexes, or MemoryPacks while the
hypothesis is partial. If partial-only content becomes recallable through any
path, or remains active in the buffer after finalization, it is a violation.

All metrics are reported overall and by scenario family, product surface,
preregistered history-length bin, and user. Slice membership may overlap, but
the overall denominator contains unique tasks only. Retrieval metrics remain
diagnostic and are never averaged with end-to-end task success.

## 8. Frozen statistical procedure

The primary point estimate is paired task-micro lift. Its confidence interval is
a paired user-cluster percentile bootstrap:

1. Start with all `T` complete hidden-test pairs grouped under all `U` hidden
   users.
2. Initialize a deterministic pseudorandom generator with
   `bootstrap_seed = 20260804`.
3. For each of exactly 100,000 replicates, sample `U` users with replacement.
4. Include every paired task belonging to each sampled user; if a user is drawn
   more than once, include that complete cluster with the same multiplicity.
5. Compute task-micro lift over the resulting paired tasks.
6. Sort the 100,000 lift values and take the 2.5th and 97.5th percentiles using
   linear interpolation between adjacent order statistics (Hyndman-Fan type 7).

The reported 95% interval is `[lower, upper]`. The protocol requirement that the
confidence interval be above zero means `lower > 0`, not `lower >= 0`.

The report MUST also include user-macro lift: calculate each user's mean `D_t`,
then take the unweighted mean across users. A user-macro percentile interval MAY
be calculated from the same sampled-user replicates and MUST be labeled
diagnostic. Task-micro remains the primary gate estimate. Point estimates,
bootstrap seed, replicate count, quantile method, paired-task count, user count,
and per-user task-count distribution MUST be included in the result manifest.

No task or user may be removed as an outlier after unblinding. Missing pairs do
not enter bootstrap processing because their presence makes the entire hidden
run incomplete under Section 5.

## 9. Independent recall-latency profile

The 300 ms recall-p95 gate is measured separately from the hidden A/B answer
run, using a frozen, hashed latency fixture and a reference environment declared
before evaluating a release candidate.

The qualifying latency profile MUST use:

- exactly one user scope with exactly 1,000 finalized stored events;
- a fully built local index and no concurrent writer;
- a local filesystem, one process, one thread, and no network, provider, model,
  embedding, ingestion, or consolidation call inside the measured interval;
- a frozen set of 100 representative recall queries and their order;
- one unmeasured pass over all 100 queries to warm process, index, and operating
  system caches;
- ten measured passes over the same 100 queries, for 1,000 measured recalls;
- a monotonic high-resolution clock around the public recall call, ending only
  after the complete MemoryPack has been selected, evidence-validated, and
  rendered; and
- nearest-rank p50 and p95 calculated from the 1,000 raw durations.

Process startup, fixture ingestion, index construction, and the warm-up pass are
excluded. Local index reads, ranking, evidence validation, and MemoryPack
rendering are included. The fixture and query hashes, release artifact,
reference hardware/storage profile, operating system, Python/runtime, Citefold
configuration, warm-up count, all raw durations, p50, and p95 MUST be retained.
The published p95 is qualifying only for that declared reference environment;
other environments are reported separately.

Track B ingest p50/p95, context characters, provider-token counts, reader cost,
and total cost per successful task are measured from the controlled product run
and reported separately from this local recall gate.

## 10. Hidden custody, consent, withdrawal, and retention

The hidden-test custodian MUST be organizationally independent of Citefold
implementation, prompt tuning, model-selection, and release-candidate tuning.
The implementation team may receive development data and permitted validation
outputs, but MUST NOT receive hidden events, tasks, labels, unblinding maps,
MemoryPack traces, or per-task results before the run is sealed.

Before collection, the custodian MUST freeze and retain:

- informed-consent text and version, allowed uses, modalities, model-provider
  disclosure, and whether de-identified aggregate results may remain public;
- data minimization and de-identification procedures, including a second-person
  review and checks for indirect re-identification;
- an exclusion scan for real passwords, government identifiers, financial
  account numbers, and raw private contact details;
- access-control roles, encrypted storage location, access logs, and incident
  procedure;
- retention deadlines for identity mappings, raw inputs, de-identified events,
  task labels, model outputs, traces, ratings, and backups; and
- a withdrawal and deletion service-level objective.

Identity mappings MUST be stored separately from benchmark content with stricter
access. Collection and provider transmission MUST follow the participant's
consent and the frozen provider policy; de-identification does not by itself
make content safe to publish or transmit.

On withdrawal, the participant's mapping, raw and de-identified history, tasks,
outputs, traces, and ratings MUST be removed from active data and expired from
backups under the documented schedule. The affected dataset release becomes
invalid for future runs and MUST be regenerated and rehashed. A previously
published aggregate may remain only when the consent explicitly permits
retention of non-identifying aggregates; otherwise it is retracted. Withdrawal
events and completion of deletion are logged without retaining the withdrawn
content.

## 11. Required artifacts and publication boundary

The custodian MUST retain the following private audit bundle:

- consent, de-identification, access, withdrawal, and retention records;
- full dataset and split manifests, events, tasks, labels, and identity-separated
  cohort records;
- frozen system/run manifests and release-distribution hash;
- randomized arm-order and rater unblinding maps;
- every arm output, explicit refusal, timeout, retry, error, tool trace, and
  MemoryPack trace;
- every deterministic check, individual human rating, adjudication, optional
  LLM-judge output, and failure label;
- bootstrap inputs and replicate outputs;
- latency fixture, raw durations, environment record, and summary; and
- every incomplete, failed, reverted, and selected evaluation iteration.

The public result bundle MUST contain:

- protocol and execution-profile versions;
- sanitized dataset manifest with hashes, counts, timeline ranges, and slice
  distribution but no hidden content or stable participant identifiers;
- exact release artifact, commit, model/provider roles, prompt/tool/config
  hashes, randomization and bootstrap methods, environment, error counts, cost,
  and start/end times;
- aggregate and required slice metrics, confidence interval, user-macro
  diagnostic, rater-agreement statistics, latency result, and all gate outcomes;
- a record of incomplete, failed, or reverted strategies without private
  hypotheses; and
- explicit claim limitations.

Raw histories, hidden questions, labels, answers, MemoryPacks, traces, ratings,
identity mappings, local paths, API keys, private contacts, provider payloads,
and examples that have not passed re-identification review MUST NOT be
published. Sanitized examples are optional and require separate consent and
documented human review.

Hashes prove artifact identity, not consent, correctness, provider compliance,
or absence of sensitive data. The private bundle remains subject to its
retention schedule and is not made public merely to support reproducibility.

## 12. Qualification and unchanged release gates

A Track B result is qualifying only when the hidden run is complete, the
dataset and run meet this profile, the audit bundle is present, and all existing
protocol gates hold on `hidden_test`:

- task success lift is at least 10 percentage points;
- the paired user-clustered 95% confidence-interval lower bound is above zero;
- memory-required task lift is at least 15 percentage points;
- no-evidence task success is no worse than `no_memory` by more than 1
  percentage point;
- unsupported-memory rate is at most 2%;
- stale or superseded fact rate is at most 2%;
- cross-scope leakage and deletion violations are both zero;
- ASR partial-transcript durable commits are zero; and
- local warm-cache single-thread recall p95 is at most 300 ms with 1,000 stored
  events for one user under the frozen reference profile.

These are the protocol v1 gates restated for execution clarity, not modified
gates. A complete run that misses any one gate is a Track B failure for that
system version. An incomplete run has no gate result. Track A, retrieval
diagnostics, an oracle condition, validation results, or an LLM-only judgment
cannot substitute for a qualifying hidden Track B result.
