# OfficeLifeMemoryBench Report

Generated at: `2026-07-16T09:45:30.066356+00:00`

## Scope

This benchmark compares `no_memory` against `memory_pack` on synthetic office/life tasks: preferences, open loops, people follow-up, meeting/voice follow-up, ASR-noise guardrails, no-evidence prompts, and scope isolation.

## Parameters

```json
{
  "probes": 24,
  "scenarios": 4,
  "tenants": 2,
  "users_per_tenant": 2
}
```

## Environment

```json
{
  "platform": "macOS-15.7.4-arm64-arm-64bit-Mach-O",
  "processor": "arm",
  "python": "3.13.2"
}
```

## Baseline Comparison

| Baseline | task_success_rate | expected_marker_hit_rate | forbidden_hit_count | citation_rate | avg_selected_nodes |
|----------|------------------:|-------------------------:|--------------------:|--------------:|-------------------:|
| no_memory | 0.3333 | 0.3333 | 0 | 0.0000 | 0.00 |
| memory_pack | 1.0000 | 1.0000 | 0 | 1.0000 | 2.17 |

## Memory Lift

- Task success lift: 0.6667
- Expected marker hit-rate lift: 0.6667

## By Category

| Category | n | no_memory | memory_pack | lift |
|----------|--:|----------:|------------:|-----:|
| asr_noise_guard | 4 | 1.0000 | 1.0000 | 0.0000 |
| meeting_followup | 4 | 0.0000 | 1.0000 | 1.0000 |
| no_evidence | 4 | 1.0000 | 1.0000 | 0.0000 |
| people_followup | 4 | 0.0000 | 1.0000 | 1.0000 |
| preference_recall | 4 | 0.0000 | 1.0000 | 1.0000 |
| task_tracking | 4 | 0.0000 | 1.0000 | 1.0000 |

## Quality Checks

- Missing expected markers: 0
- Unsupported context hits: 0
- Scope leakage count: 0

## Caveats

- This is a deterministic office/life scenario benchmark for Citefold, not a public leaderboard.
- It measures MemoryPack usefulness against a no-memory context on synthetic probes.
- No-memory is expected to fail memory-required probes; the important signal is lift without leakage or unsupported-context regressions.
