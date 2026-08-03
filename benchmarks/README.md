# Citefold benchmarks

The benchmark suite keeps retrieval, end-to-end QA, synthetic task utility, and deterministic lifecycle checks separate. Do not average them into one “memory accuracy” score.

## Runners

| Runner | Network/model calls | Purpose |
|---|---:|---|
| `officelife_memory_benchmark.py` | 0 / 0 | MemoryPack vs no-memory on scoped synthetic office/life probes |
| `officelife_track_b_benchmark.py` | 0 / 0 | Private-artifact preflight and aggregate calculations for custodian-supplied Track B inputs |
| `multimodal_memory_benchmark.py` | 0 / 0 | Multimodal evidence, conflict, correction, injection, and deletion contracts |
| `longmemeval_citefold_benchmark.py` | 0 / 0 | Citefold MemoryPack/session retrieval used for the published diagnostic |
| `longmemeval_retrieval_benchmark.py` | 0 / 0 | Separate lexical retrieval baseline; not the published Citefold score |
| `longmemeval_qa_benchmark.py` | external calls | Generate, judge, and summarize LongMemEval-S answers |
| `citefold_benchmark.py` | 0 / 0 | Local phase-one scale, isolation, and latency diagnostics |

The LongMemEval dataset is not redistributed in this repository. Obtain it from the upstream project under its terms and record its SHA-256.

## Offline checks

Run from the repository root after installing Citefold:

```bash
python -m benchmarks.officelife_memory_benchmark \
  --output-json /tmp/officelife.json \
  --output-md /tmp/officelife.md

python -m benchmarks.multimodal_memory_benchmark \
  --output-json /tmp/multimodal.json \
  --output-md /tmp/multimodal.md
```

LongMemEval retrieval:

```bash
python -m benchmarks.longmemeval_citefold_benchmark \
  /path/to/longmemeval_s_cleaned.json \
  --manifest benchmarks/longmemeval_manifest.json \
  --output-json /tmp/retrieval.json \
  --output-md /tmp/retrieval.md
```

Use `--limit` for a smoke test. Do not report a limited run as the full benchmark.

## Track B operator harness — no public score yet

The frozen
[`OFFICELIFE_TRACK_B_EXECUTION_PROFILE_V1.md`](OFFICELIFE_TRACK_B_EXECUTION_PROFILE_V1.md)
and offline runner validate and summarize artifacts supplied by the hidden-test
custodian. The runner does not collect histories, execute the paired agent arms,
blind outputs, obtain human ratings, or verify the complete private audit bundle.
Its unit tests generate temporary synthetic fixtures to exercise schemas,
arithmetic, failure semantics, gate boundaries, and redaction only. They do not
measure Citefold's real-world effect.

Keep the dataset, manifest, evaluation, preflight output, raw answers, and audit
records in an access-controlled directory outside the repository. A private
structural preflight can be run with:

```bash
python -m benchmarks.officelife_track_b_benchmark preflight \
  /private/path/dataset.json \
  /private/path/manifest.json \
  --strict \
  --output-json /private/path/preflight.json
```

After the custodian has supplied a complete paired evaluation artifact, run the
calculation with the frozen bootstrap defaults:

```bash
python -m benchmarks.officelife_track_b_benchmark summarize \
  /private/path/dataset.json \
  /private/path/manifest.json \
  /private/path/evaluation.json \
  --output-json /private/path/summary-candidate.json \
  --output-md /private/path/summary-candidate.md \
  --enforce-computed-gates
```

`--enforce-computed-gates` only changes the process exit code for the offline
calculation. It never marks the run qualified or claimable.

The preflight JSON contains per-user pseudonymous identifiers and is not a
publication artifact. The aggregate output is a non-claimable diagnostic
summary, not a sanitized publication artifact. A strict allowlist-based public
projector is still pending, as are the sealed run manifest, controlled
execution, latency artifact, private audit bundle, consent, privacy review, and
re-identification review. A qualifying calculation must retain the frozen
defaults of 100,000 user-cluster bootstrap repetitions and seed `20260804`.

## End-to-end QA

QA generation and judging use external models and can incur cost. Keep keys in environment variables and inspect all options before running:

```bash
python -m benchmarks.longmemeval_qa_benchmark generate --help
python -m benchmarks.longmemeval_qa_benchmark judge --help
python -m benchmarks.longmemeval_qa_benchmark summarize --help
```

A reproducible run should save:

- the dataset hash;
- exact Citefold commit/release;
- manifest and system version;
- context mode and token budget;
- reader and judge model slugs;
- actual provider if the API reports it;
- output, evaluation, summary, and sanitized trace files;
- start/end time, cost, and error count.

Never commit API keys or traces containing private user data.

## Current results

Checked-in reports generated on 2026-07-16 live in [`results/current/`](results/current/README.md). Retrieval is a complete Citefold `0.1.0` source-snapshot diagnostic, but it is not bound to a commit, tag, or PyPI artifact. The complete QA snapshot remains a pre-rename, pre-release, unofficial run because it used a non-official judge. Historical QA and OfficeLife filenames/fields that contain `pmos` or `personal-memory-os` are retained unchanged so the artifacts remain auditable.

The current result set establishes:

- strong retrieval on one public dataset;
- moderate and uneven end-to-end QA with a non-official judge;
- deterministic synthetic lift without observed fixture leakage;
- multimodal memory lifecycle behavior independent of media-model quality.

It does not establish production readiness, official leaderboard status, or real-world OCR/ASR/vision quality.
There is no qualifying Track B result yet.

## Adding a result

1. Run into a temporary directory, not `results/current/`.
2. Inspect the JSON for secrets, paths, and raw private content.
3. Verify sample counts, hashes, model/provider fields, and caveats.
4. Re-run the relevant benchmark tests.
5. Add both machine-readable JSON and a concise Markdown report.
6. Update `results/current/README.md` and `docs/benchmarks.md` in the same change.

Performance or quality claims in pull requests should link to the exact artifact that supports them.
