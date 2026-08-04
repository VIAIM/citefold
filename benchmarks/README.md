# Citefold benchmarks

The benchmark suite keeps retrieval, end-to-end QA, synthetic task utility, and deterministic lifecycle checks separate. Do not average them into one “memory accuracy” score.

## Runners

| Runner | Network/model calls | Purpose |
|---|---:|---|
| `officelife_memory_benchmark.py` | 0 / 0 | MemoryPack vs no-memory on scoped synthetic office/life probes |
| `officelife_track_b_contract.py` | 0 / 0 | Fail-closed validation of the strict Track B dataset and sealed-run artifact contract |
| `officelife_track_b_executor.py` | handler-defined | Label-free worker handoff and private paired-arm execution; current callable adapter is test-only |
| `officelife_track_b_latency.py` | 0 / 0 | Validates a private, release-bound recall-latency assay artifact |
| `officelife_track_b_qualification.py` | 0 / 0 | Validates the sealed scoring, adjudication, latency, qualification, and public-projection chain |
| `officelife_track_b_benchmark.py` | 0 / 0 | Legacy private-artifact diagnostic and aggregate calculator; not the controlled executor or public projector |
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

## Track B private artifact tools — no public score yet

The frozen
[`OFFICELIFE_TRACK_B_EXECUTION_PROFILE_V1.md`](OFFICELIFE_TRACK_B_EXECUTION_PROFILE_V1.md)
now has a versioned, physically separated
[`OFFICELIFE_TRACK_B_ARTIFACT_CONTRACT_V1.md`](OFFICELIFE_TRACK_B_ARTIFACT_CONTRACT_V1.md)
for custodian-supplied datasets and sealed-run inputs. The strict validator
checks local Draft 2020-12 schemas, exhaustive byte inventories, hashes,
cross-file references, event lifecycle, task/label separation, frozen system
identity, clone-resistant conservative structural counts, and minimum
hidden-test dataset gates. Those counts are lower bounds, not proof of semantic
sample diversity. The separate
[`OFFICELIFE_TRACK_B_EXECUTOR_V1.md`](OFFICELIFE_TRACK_B_EXECUTOR_V1.md)
contract prepares an exhaustive label-free worker bundle and executes paired
arms with frozen order, retries, provider identity checks, output blinding,
crash recovery, and result-bound private audit events. Its only implemented
handler is an in-process callable and its snapshot adapter is opaque, so every
output remains test-only, non-qualifying, and non-claimable. Neither tool
collects participant histories, obtains human ratings, or measures Citefold's
real-world effect.

The corresponding
[`OFFICELIFE_TRACK_B_QUALIFICATION_V1.md`](OFFICELIFE_TRACK_B_QUALIFICATION_V1.md)
adds the other half of the audit path: a qualification plan sealed before the
worker handoff, deterministic scoring, blinded rating/adjudication bindings, a
release-bound latency artifact, and a receipt-bound public projection validator.
It verifies hashes and structural invariants; it cannot convert a synthetic or
test-only execution into a qualifying product result.

Keep the dataset, run inputs, labels, validation reports, raw answers, and audit
records in access-controlled directories outside the repository. Install the
source development dependencies, then validate the strict contract:

```bash
python -m pip install -e ".[dev]"

python -m benchmarks.officelife_track_b_contract validate-dataset \
  /private/path/dataset-root \
  --enforce-minimum-dataset-gates \
  --output-json /private/path/reports/dataset-contract.json

python -m benchmarks.officelife_track_b_contract validate-run \
  /private/path/dataset-root \
  /private/path/run-root \
  --enforce-minimum-dataset-gates \
  --output-json /private/path/reports/run-contract.json
```

The output directory must not be inside either input root. A successful strict
validation report remains private and `claimable = false`: hashes prove byte
identity, not consent truth, independent custody, executor isolation, or
qualification. Exit `0` means the requested checks passed, `3` means the
contract is valid but dataset status or enforced minimum gates failed, and `4`
means the contract is invalid.

The executor is a Python contract rather than an operator CLI. Custodian code
calls `prepare_worker_bundle(...)`, then a separately launched label-free worker
calls `execute_worker_bundle(...)` with a handler implementing the documented
`ArmRequest`/`HandlerResult` interface. A formal run still requires a future
external-process adapter, OS-enforced denial of the custodian roots, and a
validated non-opaque snapshot adapter; the callable entrypoint fails closed if
configured as though those controls existed.

The older single-file diagnostic remains available for already prepared private
evaluation artifacts. It combines information that a controlled generator must
not receive, so it is post-run only. After the custodian has supplied a complete
paired evaluation artifact, calculate the frozen gates with:

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
summary, not a sanitized publication artifact. The qualification module has a
strict allowlist-based public projector, but a qualifying run still needs an
external-process executor and snapshot adapter, independent custody evidence,
consent, privacy review, re-identification review, real blinded raters, and a
separately controlled signing key. A qualifying calculation must retain the
frozen defaults of 100,000 user-cluster bootstrap repetitions and seed
`20260804`.

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
