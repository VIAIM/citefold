# Changelog

All notable changes to Citefold are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Frozen OfficeLifeMemoryBench Track B execution profile, versioned private
  dataset/sealed-run artifact schemas, and a fail-closed validator for exact
  inventories, hashes, lifecycle rules, hidden-label separation, frozen system
  identity, and minimum dataset gates.
- Offline Track B post-run diagnostic and aggregate-calculation harness. Its
  synthetic tests validate evaluator contracts and redaction/non-claimability
  guards only; they are not a Track B product result.
- Versioned `agent-turn-v1` prepare/complete hooks with a stable JSON-compatible
  context envelope, trusted-host turn IDs, and an installed-wheel consumer test.
- Auditable Python API and CLI operations to pin active records against decay
  and unpin them without changing trust, evidence, or deletion semantics.
- Root-level schema 2 manifests and read-only storage status inspection.
- Explicit v0.1 → v0.2 additive-metadata migration with semantic preflight, a
  mandatory verified backup, v0.1 scope/ledger locking, concurrent-change
  detection, and interruption recovery that never rolls canonical data back.
- Verified ZIP backup and restore APIs/commands with archive path and hash
  validation; a sibling restore-intent journal closes directory-swap recovery,
  and replacing a non-empty root retains and reports the displaced root.
- Shared root coordination for normal operations and exclusive coordination for
  migration, backup, and restore on supported local POSIX filesystems.

### Changed

- Agent turns now use turn-aware observation, Episode, and Markdown identities,
  preventing same-session turns completed in the same second from overwriting
  each other's projection.
- Storage roots must be dedicated to Citefold. Unrecognized non-empty roots,
  legacy roots awaiting migration, corrupt manifests, and newer schemas now fail
  closed instead of being initialized or used by normal memory operations.

## 0.1.0 — 2026-08-03

### Added

- Evidence-backed text, image, audio, and video ingestion.
- Scoped memory records with revisions, corrections, archival, and deletion.
- Bounded recall packs with source citations and an evidence gate.
- Local-first storage, hybrid retrieval, consolidation, and an optional
  OpenRouter adapter.
- Initial alpha Python API, `citefold` command-line interface, reproducible
  benchmark runners, and the first public documentation set.
