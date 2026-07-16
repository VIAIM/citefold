# Multimodal MemoryPack vs No Memory Benchmark

Generated at: `2026-07-16T09:45:15.517714+00:00`

## Outcome

- MemoryPack task success: 1.0000
- No Memory task success: 0.3000
- Memory lift: 0.7000
- Unsupported / forbidden / scope leakage: 0 / 0 / 0

## Method

Each arm receives the same task contract and seeded history. `no_memory` receives no historical context; `memory_pack` receives the real `Citefold.recall()` result. Supplied observations keep the run offline and deterministic.

## Baseline Comparison

| Baseline | task success | expected hit | coverage match | unsupported | forbidden | scope leakage |
|---|---:|---:|---:|---:|---:|---:|
| no_memory | 0.3000 | 0.3000 | 0.3000 | 0 | 0 | 0 |
| memory_pack | 1.0000 | 1.0000 | 1.0000 | 0 | 0 | 0 |

## Cases

| Case | Modality | No Memory | MemoryPack | Coverage | Citations | Safety |
|---|---|---:|---:|---|---:|---|
| text_fact | text | 0 | 1 | supported | 1 | pass |
| image_text | image | 0 | 1 | supported | 1 | pass |
| audio_commitment | audio | 0 | 1 | supported | 1 | pass |
| video_audio_visual | video | 0 | 1 | supported | 2 | pass |
| low_confidence_asr | audio | 1 | 1 | none | 0 | pass |
| preference_correction | text | 0 | 1 | supported | 2 | pass |
| unresolved_conflict | text | 0 | 1 | partial | 2 | pass |
| no_evidence | none | 1 | 1 | none | 0 | pass |
| media_prompt_injection | image | 0 | 1 | supported | 1 | pass |
| deletion_cascade | image | 1 | 1 | none | 0 | pass |

## Fixture

- Path: `benchmarks/fixtures/multimodal_memory_bench_v1.json`
- SHA-256: `d494d1aac58cb9b09fc4eb0aaf9b64666bd7a223655b5b68b595099af174cf51`
- Cases: 10
- Network/model calls: 0/0

## Caveats

- This is a deterministic local regression benchmark, not a public leaderboard.
- It does not measure reader-LLM answer quality; both arms use the same deterministic contract scorer.
- Supplied observations isolate memory lifecycle behavior from OCR, ASR, vision-model, and codec quality.
- Task success measures whether MemoryPack exposes the expected evidence while respecting safety, coverage, deletion, and scope contracts.
- Raw media text may be present only as quoted untrusted evidence; forbidden checks separately inspect trusted active memory.
