# OfficeLifeMemoryBench Track B Qualification Pipeline v1

Status: nonclaimable offline qualification pipeline. No qualifying Track B
result exists.

## Purpose and boundary

This contract closes the post-execution path from blinded arm outputs to
auditable binary task outcomes, paired statistics, a controlled recall-latency
artifact, and a strict aggregate projection. It consumes the frozen Track B
dataset/run contract and controlled-executor artifacts; it does not weaken or
replace either contract.

The v1 implementation is intentionally unable to turn the current callable
handler and opaque snapshot adapter into a qualifying result. Their
`qualification_eligible = false` state and nonqualification reasons propagate
through scoring. Perfect synthetic ratings, zero self-reported violations, or
fast imported latency numbers cannot remove that state.

This pipeline proves structural closure and deterministic calculation. It does
not prove consent truth, semantic annotation quality, rater independence,
operating-system isolation, signature authenticity, or real product effect.

## Frozen pre-execution configuration

A formal iteration adds one `track-b-qualification-plan` artifact to the
sealed-run inventory before the label-free worker bundle is prepared. The
worker manifest's source-run hash therefore detects a plan added or changed
after outputs exist.

The configuration freezes:

- the scorer implementation hash and deterministic parser identity;
- rating-assignment algorithm, seed, roster commitment, annotation-codebook
  hash, and primary/tiebreak policy;
- exactly 100,000 paired user-cluster bootstrap replicates, seed `20260804`,
  the `xorshift64star-rejection-v1` PRNG, and Hyndman-Fan type-7 quantiles;
- latency fixture, query-set, runner, release-distribution, and reference-
  environment commitments;
- the public projector version, slice-suppression policy, iteration-selection
  rule, and custodian public-key commitment.

Local hashes are identity commitments, not signatures. Until a later adapter
verifies an externally authenticated custody chain against the frozen key,
qualification remains ineligible.

## Physically separated artifacts

The custodian retains roots with distinct, non-nested paths and permissions:

```text
rating-root/
  rating-manifest.json
  rating-items.jsonl
  rating-assignments.jsonl
  rating-submissions.jsonl

adjudication-root/
  adjudication-manifest.json
  rating-submissions.jsonl
  deterministic-assessments.jsonl
  safety-reviews.jsonl
  citation-assessments.jsonl
  arm-measurements.jsonl
  adjudicated-arms.jsonl
  adjudication-audit.jsonl

latency-root/
  latency-manifest.json
  latency-config.json
  queries.jsonl
  raw-durations.jsonl
  latency-summary.json
  audit.jsonl

qualification-root/
  qualification-manifest.json
  adjudicated-arms.jsonl
  scored-arms.jsonl
  scored-pairs.jsonl
  gate-results.jsonl
  private-summary.json

public-root/
  public-manifest.json
  public-result.json
  publication-context.json
  publication-receipt.json
  qualification-plan.json
  custodian-public-key.json
```

Every manifest exhaustively inventories every other regular file. Paths are
relative and case-collision-free; symlinks, hardlinks, duplicate JSON keys,
non-finite numbers, non-canonical JSON/JSONL, undeclared files, oversize files,
and read-time identity changes fail closed.

## Blinding and human ratings

Rater material is regenerated from execution outputs into a separately
randomized order. It may expose a new rating-item ID, the task payload and
allowed recent context, the answer/refusal/timeout presentation, and the
frozen codebook. It must not expose task or user IDs, treatment, execution
order, provider route, MemoryPack, trace, source aggregate position, labels, or
the treatment-unblinding map.

Assignment is frozen before rating. One rater must never receive both arms of
the same task. Every output requiring human judgment receives exactly two
different primary raters. Agreement is final and forbids a third rating.
Disagreement requires exactly one independently assigned third rater, distinct
from both primaries and unable to see their votes. All assignments, ordering,
timestamps, and individual submissions remain private audit records.

## Deterministic and binary scoring

The scorer never accepts a caller-supplied final `success`, a
`deterministic_pass` aggregate, or a Boolean rating array. For every output and
label check it requires one bound deterministic-assessment record containing:

- output, label, and individual-check hashes;
- frozen parser identity and implementation hash;
- a `pass` or `fail` verdict; and
- any matched answer spans with exact offsets and content hashes.

The task label's check IDs must close exactly: missing, duplicate, or unknown
checks invalidate scoring. Semantic correctness still requires independent
review; byte bindings prevent an assessment from silently moving to another
output or label.

For an answer or explicit refusal, arm success is derived as:

```text
all required checks pass
AND all hard-prohibition checks pass
AND final human judgment passes when required
```

A product timeout or preregistered product failure scores `0`. An
infrastructure error, missing pair, missing assessment, missing required
rating, treatment drift, or corrupt artifact makes the whole iteration
`not_evaluable`; tasks are not deleted, imputed, or repaired in place.

## Safety and citation closure

Safety is not accepted as a list of aggregate zeroes. Each required
task/arm/category review declares exhaustive coverage and retains every
claim/source/artifact finding. Release counts deduplicate affected task IDs
while diagnostic finding counts remain available privately.

The categories are:

- unsupported memory;
- stale or superseded content;
- cross-scope leakage;
- deletion violation;
- partial-ASR durable commit; and
- no-evidence false answer.

Unsupported-memory and stale rates use all scored `memory_pack` tasks as their
denominator. Cross-scope and deletion review covers both answer arms and any
MemoryPack/active artifact. Partial-ASR review covers provenance from the
partial source through Episode, candidate, record, index, and MemoryPack.

Citation precision is computed from individually assessed claim-to-source
links that pass scope, cutoff, validity, and support checks. Required-fact
source coverage is computed from every label fact that requires source support.
Event-ID overlap is not a substitute.

## Paired statistics

Every scored task has exactly one `no_memory` and one `memory_pack` result from
the same frozen input. The pipeline reports task-micro success rates and
absolute lift overall, for memory-required and memory-absent tasks, and by
scenario family, surface, preregistered history-length bin, and private user.
Public output excludes per-user IDs and retains only the user-macro diagnostic
and non-identifying distributions allowed by the projection policy.

The confidence interval uses exactly the frozen procedure:

1. group complete pairs by user;
2. sample the same number of user clusters with replacement;
3. include all tasks from each selected cluster with multiplicity;
4. calculate task-micro lift;
5. repeat exactly 100,000 times with seed `20260804`; and
6. take type-7 2.5th and 97.5th percentiles.

All replicate values and their canonical byte hash remain in the private
qualification bundle. A confidence lower bound equal to zero fails the gate.

## Recall-latency assay

`officelife_track_b_latency.py` restores a verified Citefold backup into a
disposable local root, checks one declared scope with exactly 1,000 finalized
observations and no partial voice observation, and opens Citefold with
`openrouter=None`. It reads exactly 100 unique frozen queries in their stored
order, performs one unmeasured warm pass, then ten measured passes.

`time.perf_counter_ns()` surrounds the public `Citefold.recall()` call. The
complete MemoryPack is materialized and hashed before the stop timestamp. Each
of the 1,000 records binds pass, query ID/order, duration, and response digest.
Nearest-rank p50 and p95 are derived from those raw durations; p95 of exactly
300 ms passes and any larger value fails.

The local runner can prove the call sequence and byte-level artifact. It cannot
by itself prove OS-level network denial, process/thread isolation, absence of a
concurrent writer, or reference-hardware custody. Those missing attestations
keep the local artifact nonqualifying.

## Qualification state

Three states are reported separately:

- `computed_gate_status`: deterministic calculation when the full chain is
  evaluable;
- `qualification_status`: `not_eligible`, `not_evaluable`, `failed`, or
  `passed`; and
- `publication_status`: private, pending review, approved, or retracted.

Qualification and publication are derived states, never accepted input
booleans. A complete eligible run that misses a gate is a trustworthy negative
result. An incomplete run has no pass/fail gate conclusion. The current
executor always produces `not_eligible` here.

## Public projection

The projector constructs a fresh object from an explicit allowlist. It never
recursively redacts a private object. It refuses incomplete or ineligible runs
and returns a claimable result only when the custodian's Ed25519 receipt signs
the exact plan, qualification manifest, private summary, public context, final
public result, projector source hash, and public-key hash.

The public root carries the receipt, plan, public context, and public key with
the result. A verifier must also supply the independently published plan and
key SHA-256 anchors. Bundle-local hashes and a self-selected key prove only
internal consistency; they are not a trust anchor. A missing or different
anchor fails validation.

Allowed output is limited to protocol/profile versions, sanitized dataset
counts and slice distributions, release/model/config commitments, aggregate
metrics and gates, non-identifying rater agreement, latency summary, error/cost
aggregates, iteration status, and fixed limitations.

It excludes raw histories, prompts, tasks, labels, answers, MemoryPacks,
ratings, blind/task/user IDs, local paths, provider payloads, free-text errors,
and unreviewed examples. Optional examples require a separate consent and
re-identification-review contract and are not part of v1.

## CLI and exit semantics

These are benchmark operator tools and are deliberately not added to the
end-user `citefold` CLI:

```bash
python -m benchmarks.officelife_track_b_qualification validate-ratings /private/rating-root
python -m benchmarks.officelife_track_b_qualification validate-adjudication /private/adjudication-root
python -m benchmarks.officelife_track_b_qualification validate-qualification /private/qualification-root
python -m benchmarks.officelife_track_b_qualification validate-plan /private/dataset-root /private/run-root
python -m benchmarks.officelife_track_b_qualification validate-chain \
  /private/dataset-root /private/run-root /private/worker-root /private/execution-root \
  /private/rating-root /private/adjudication-root /private/latency-root /private/qualification-root
python -m benchmarks.officelife_track_b_qualification validate-public /safe/public-root \
  --trusted-qualification-plan-sha256 <published-plan-sha256> \
  --trusted-custodian-public-key-sha256 <published-key-sha256>

python -m benchmarks.officelife_track_b_latency run \
  /private/fixture.zip /private/queries.jsonl /private/latency-config.json \
  /private/new-latency-root
python -m benchmarks.officelife_track_b_latency validate /private/latency-root
```

Validation exits `0` only when the requested artifact contract passes and `4`
for invalid artifacts. A public bundle without externally supplied trust anchors
is invalid. These controls do not change the current real-result boundary:
there is no qualifying Track B run yet.

## Remaining qualification work

Before a real Track B result can qualify, Citefold still requires a validated
external-process handler, a transparent snapshot-to-Citefold adapter, OS-level
dataset/label/network isolation, an independently controlled signing key and
authenticated custody chain, consented hidden data, real blinded raters,
semantic and re-identification review, and the complete append-only iteration
ledger. Formal public release remains a separate approval.
