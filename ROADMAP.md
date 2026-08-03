# Roadmap

Citefold's roadmap prioritizes evidence quality, privacy, and reproducibility
over feature count. Items are directions, not delivery commitments.

## 0.1 — Public foundation

- Initial alpha Python API and `citefold` CLI for ingestion, recall, correction,
  archival, deletion, consolidation, and index rebuilds.
- Candidate review through CLI list, approve, and reject commands.
- Text-native local storage with scope enforcement and revision history.
- Reproducible retrieval, synthetic office/life, and deterministic multimodal
  benchmarks with explicit limitations.
- Linux and macOS CI across Python 3.9 through 3.13.

## Next

- Add higher-level human-review hooks and interfaces around the existing
  candidate CLI workflow.
- Add narrow runtime adapters around the versioned `agent-turn-v1` contract
  without coupling the core to one framework.
- Harden the explicit schema, additive v0.1 migration, verified backup/restore,
  intent-journal recovery, and displaced-root workflow against broader
  deployment histories and crash/power-loss points.
- Evaluate real OCR, speech-to-text, and video pipelines separately from memory
  lifecycle correctness.
- Publish latency, storage-growth, concurrency, and long-running retention
  measurements.
- Improve configuration and operator ergonomics around the existing storage
  status, migration, backup, and recovery commands.

## Later, after evidence

- Pluggable storage and retrieval backends where a second proven use case
  justifies the abstraction.
- Explicitly tested network-filesystem and Windows locking semantics.
- Real-user longitudinal evaluation with consent, privacy review, and auditable
  deletion tests.

Feature requests and design proposals belong in
[GitHub Issues](https://github.com/VIAIM/citefold/issues).
