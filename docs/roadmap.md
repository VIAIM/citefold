# Roadmap

The roadmap follows evidence and release gates. It is directional and may change; entries are not delivery commitments.

## 0.1 — trustworthy alpha

- [x] Evidence → observation → episode → candidate → record model.
- [x] Scoped local storage and rebuildable SQLite FTS.
- [x] Bounded, cited MemoryPack with evidence validation.
- [x] Text, image, audio, video, and realtime-voice ingest paths.
- [x] Correction, archival, decay, deletion, and index rebuild.
- [x] Candidate list/approve/reject CLI workflow plus local init/doctor/demo commands.
- [x] Optional privacy-constrained OpenRouter adapter.
- [x] Retrieval, QA, synthetic A/B, and multimodal lifecycle reports.
- [x] Freeze package metadata and publish tagged `0.1.0` artifacts with PyPI attestations.
- [x] Verify CI on every declared Python/OS combination.
- [x] Publish PyPI distribution from the same release artifacts.

## 0.2 — review and compatibility

- Bulk candidate-review UX and richer filtering/explanation metadata.
- [x] Freeze the Track B execution profile; add a versioned, fail-closed private
  dataset/sealed-run artifact contract and non-claimable aggregate diagnostics.
- [ ] Complete an independently custodied qualifying Track B run on consented,
  de-identified longitudinal data.
- [x] Public pin/unpin operations.
- [x] Explicit root storage schema and fail-closed status inspection.
- [x] Preflighted v0.1 → v0.2 migration with verified backup and restore commands.
- [x] Versioned `agent-turn-v1` prepare/complete hooks and wheel-consumer smoke test.
- Windows and network-filesystem locking tests.
- Pluggable extraction interfaces with at least one non-OpenRouter example.
- Better entity, time, topic, novelty, and contradiction grouping.
- [x] Stable machine-readable MemoryPack contract through `AgentTurnContext.as_dict()`.

## 0.3 — measured integrations

- Real image, audio, and video quality suites.
- Scale, latency, storage-growth, and model-cost measurements.
- Exhaustive crash/power-loss and concurrent-writer stress tests beyond the covered additive-migration and journaled-restore breakpoints.
- Narrow OpenAI Agents SDK, LangGraph, LlamaIndex, and MCP adapters.
- User-facing candidate review reference application.
- Benchmark result manifests tied to release commits and artifacts.

## 1.0 release gates

1. Stable API and on-disk schema with documented migrations.
2. Independent security and threat-model review.
3. Real-user longitudinal evaluation with consent and deletion testing.
4. Independently reproduced public benchmark results.
5. Declared performance/scale envelope and supported platforms.
6. Maintainer and vulnerability-response policy proven in practice.

## Non-goals

- Claiming full human-memory equivalence.
- Becoming a general vector database.
- Hiding provider or evaluation uncertainty behind one score.
- Automatically granting tool permissions from remembered procedures.
- Shipping a distributed service before the embedded contracts are stable.

Open a discussion before implementing a major roadmap item. A small, evidenced capability is preferable to an adapter or abstraction with no verified consumer.
