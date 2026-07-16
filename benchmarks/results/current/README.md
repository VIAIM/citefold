# Current benchmark snapshot

These files are checked-in Citefold measurements generated on 2026-07-16. Retrieval is a complete `0.1.0` source-publication snapshot diagnostic (`system=citefold`, `system_version=0.1.0`), but it is not bound to a commit, tag, or PyPI artifact. The complete QA snapshot is historical, pre-rename, pre-release, and unofficial because it did not use the official judge.

| Report | Sample | Result | Primary caveat |
|---|---:|---:|---|
| [LongMemEval-S retrieval](longmemeval-s-cleaned-citefold-retrieval-v0.1.0.md) | 470 answerable questions | Recall-any@5 `0.9723`; MRR `0.9139` | `0.1.0` source snapshot; not QA or a tagged release |
| [LongMemEval-S QA (historical pre-release)](longmemeval-s-cleaned-pmos-evidence-multimodal-v5-openrouter-deepseek-v3.1-2026-07-16.md) | 500 questions | Overall `0.6180` | Reader/judge were DeepSeek v3.1; not official-judge compatible |
| [OfficeLifeMemoryBench](officelife-memory-bench-evidence-multimodal-v5-2026-07-16.md) | 24 synthetic probes | `1.0000` vs `0.3333` | Deterministic synthetic utility, not field performance |
| [Multimodal lifecycle](multimodal-memory-pack-v5.md) | 10 fixtures | `1.0000` vs `0.3000` | Supplied observations; no OCR/ASR/vision/reader evaluation |

## Artifact notes

- JSON files are the machine-readable result source; Markdown files are summaries.
- Historical QA JSON fields that contain `/private/tmp` dataset or hypothesis paths are capture-time metadata only; those local files are not included in the repository and the paths are not expected to resolve.
- The retrieval JSON records `system=citefold`, `system_version=0.1.0`, 8 workers, 500 total questions, 470 evaluated questions, and 30 excluded abstention questions; it does not record a commit SHA.
- LongMemEval dataset SHA-256: `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.
- Multimodal fixture SHA-256: `d494d1aac58cb9b09fc4eb0aaf9b64666bd7a223655b5b68b595099af174cf51`.
- `pmos` and `personal-memory-os` in preserved historical QA and OfficeLife filenames/fields are pre-release names. Those artifacts were not rewritten during the Citefold rename.
- The QA provider was not pinned and the judge was not the official LongMemEval judge, so `0.6180` is not an official leaderboard score.
- Retrieval recall, QA accuracy, and deterministic contract success are different metrics.

For commands, result-handling rules, and claim hygiene, see the [benchmark suite README](../../README.md) and [benchmark documentation](../../../docs/benchmarks.md).
