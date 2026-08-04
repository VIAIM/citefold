# Benchmarks

Citefold checks retrieval, end-to-end question answering, synthetic agent usefulness, multimodal lifecycle behavior, and scope/safety invariants. Each measurement answers a different question.

The numbers below are **checked-in measurements generated on 2026-07-16**. Retrieval is a complete rerun on the Citefold `0.1.0` source-publication snapshot, with `system=citefold` and `system_version=0.1.0`; it is not commit-bound and does not imply a tag or PyPI release. The QA run is a complete historical pre-release snapshot with a non-official judge.

## Scorecard

| Evaluation | Dataset / cases | Current result | Interpretation |
|---|---:|---:|---|
| LongMemEval-S retrieval diagnostic (`0.1.0` source snapshot) | 470 answerable questions | Recall-any@5 `0.9723`; Recall-all@5 `0.8447`; MRR `0.9139` | Whether at least one/all gold sessions were returned |
| LongMemEval-S end-to-end QA (historical pre-release) | 500 questions | Overall `0.6180`; answerable `0.6043`; abstention `0.8333` | Reader answer judged by another model |
| OfficeLifeMemoryBench Track A | 24 synthetic probes | MemoryPack `1.0000`; no-memory `0.3333`; lift `+0.6667` | Whether scoped memory exposes expected markers without forbidden context |
| OfficeLifeMemoryBench Track B | Qualifying dataset/run pending | No result | Frozen profile, strict private artifacts, and a test-only controlled-execution draft |
| Multimodal lifecycle | 10 deterministic fixtures | MemoryPack `1.0000`; no-memory `0.3000`; lift `+0.7000` | Whether evidence, conflicts, deletion, and safety contracts survive multiple modalities |

## LongMemEval-S retrieval

The retrieval diagnostic uses the public cleaned dataset, excludes 30 abstention questions in the official retrieval style, and scores session IDs represented by returned MemoryPack nodes. The `0.1.0` value identifies the tested source snapshot; the artifact does not contain a commit SHA and is not a tagged-release result.

- Dataset SHA-256: `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`
- [Markdown report](https://github.com/VIAIM/citefold/blob/main/benchmarks/results/current/longmemeval-s-cleaned-citefold-retrieval-v0.1.0.md)
- [Machine-readable result](https://github.com/VIAIM/citefold/blob/main/benchmarks/results/current/longmemeval-s-cleaned-citefold-retrieval-v0.1.0.json)

It is a retrieval diagnostic, not an end-to-end answer score.

## LongMemEval-S QA

The complete run generated 500 hypotheses and judged them with:

- reader: `deepseek/deepseek-chat-v3.1`;
- judge: `deepseek/deepseek-chat-v3.1`;
- official-judge compatible: **no**.

The actual upstream provider was not pinned, and the judge was not the official `gpt-4o-2024-08-06`. The result must not be presented as an official LongMemEval leaderboard score.

- [Markdown report](https://github.com/VIAIM/citefold/blob/main/benchmarks/results/current/longmemeval-s-cleaned-pmos-evidence-multimodal-v5-openrouter-deepseek-v3.1-2026-07-16.md)
- [Machine-readable result](https://github.com/VIAIM/citefold/blob/main/benchmarks/results/current/longmemeval-s-cleaned-pmos-evidence-multimodal-v5-openrouter-deepseek-v3.1-2026-07-16.json)

Question-type accuracy varied substantially: single-session-user `0.9000`, knowledge-update `0.8205`, temporal-reasoning `0.6617`, and multi-session `0.3835`. The aggregate should not hide those weaknesses.

## OfficeLifeMemoryBench Track A

This deterministic benchmark compares the same task contract with and without a MemoryPack across preferences, tasks, people follow-up, meeting/voice follow-up, ASR-noise guards, no-evidence prompts, and scope isolation.

- 2 tenants × 2 users = 4 scenarios; 6 probes per scenario = 24 probes.
- Missing expected markers: 0.
- Unsupported-context hits: 0.
- Scope leakage: 0.
- [Markdown report](https://github.com/VIAIM/citefold/blob/main/benchmarks/results/current/officelife-memory-bench-evidence-multimodal-v5-2026-07-16.md)
- [Machine-readable result](https://github.com/VIAIM/citefold/blob/main/benchmarks/results/current/officelife-memory-bench-evidence-multimodal-v5-2026-07-16.json)

This establishes deterministic behavior on synthetic probes, not field productivity.

## OfficeLifeMemoryBench Track B

Track B now has a frozen execution profile, a versioned private dataset and
sealed-run artifact contract, a fail-closed structural validator, a label-free
paired-arm executor draft, a sealed qualification plan, and an auditable
scoring/adjudication/latency/public-projection validation chain. The strict contract physically separates
generator-visible task inputs from custodian-only labels and binds an exhaustive
inventory, hashes, record schemas, event lifecycle, minimum dataset gates, and
the frozen system/provider configuration. Exact clones and common Unicode,
case, whitespace, prompt-attachment, or event-attachment variants cannot
inflate its conservative structural task/event counts. Passing it proves only
the supplied artifact structure and identity at validation time; it does not
prove semantic dataset diversity.

The executor draft prepares an exhaustive label-free handoff, runs randomized
paired arms, enforces frozen retry and provider identity, and binds private
outputs, traces, blinding maps, and audit events. The qualification pipeline
then validates deterministic scoring, blinded ratings and adjudication,
release-bound latency, and a receipt-bound public projection. Its implemented
handler is an in-process callable and its snapshot adapter treats state as
opaque bytes. It therefore does not enforce OS file denial, establish eligible
Citefold snapshot state, conduct real blinded judging, execute a real controlled
latency assay, or produce a qualifying/public artifact. Generated synthetic
fixtures test contracts, failure semantics, recovery, and redaction guards only.

No qualifying consented hidden dataset has been run, so Citefold has no
trustworthy Track B product-effect score. A result remains non-claimable until
independent custody, consented collection and annotation, a sealed paired run,
human adjudication, privacy review, and the full audit chain are complete. See
the [frozen execution profile](https://github.com/VIAIM/citefold/blob/main/benchmarks/OFFICELIFE_TRACK_B_EXECUTION_PROFILE_V1.md),
[artifact contract](https://github.com/VIAIM/citefold/blob/main/benchmarks/OFFICELIFE_TRACK_B_ARTIFACT_CONTRACT_V1.md),
[executor contract](https://github.com/VIAIM/citefold/blob/main/benchmarks/OFFICELIFE_TRACK_B_EXECUTOR_V1.md),
[qualification pipeline](https://github.com/VIAIM/citefold/blob/main/benchmarks/OFFICELIFE_TRACK_B_QUALIFICATION_V1.md),
and [benchmark operator guide](https://github.com/VIAIM/citefold/blob/main/benchmarks/README.md).

## Multimodal lifecycle

The fixture covers text, image text, audio commitments, aligned video observations, low-confidence ASR, correction, unresolved conflict, no-evidence behavior, media prompt injection, and deletion cascades.

- Fixture SHA-256: `d494d1aac58cb9b09fc4eb0aaf9b64666bd7a223655b5b68b595099af174cf51`
- Network calls: 0; model calls: 0.
- Unsupported / forbidden / scope leakage: 0 / 0 / 0.
- [Markdown report](https://github.com/VIAIM/citefold/blob/main/benchmarks/results/current/multimodal-memory-pack-v5.md)
- [Machine-readable result](https://github.com/VIAIM/citefold/blob/main/benchmarks/results/current/multimodal-memory-pack-v5.json)

Supplied observations deliberately isolate memory behavior from OCR, ASR, vision-model, codec, and reader-LLM quality.

## Reproduce

Offline benchmarks can be run without a model key:

```bash
python -m benchmarks.officelife_memory_benchmark \
  --output-json /tmp/officelife.json \
  --output-md /tmp/officelife.md

python -m benchmarks.multimodal_memory_benchmark \
  --output-json /tmp/multimodal.json \
  --output-md /tmp/multimodal.md
```

LongMemEval requires obtaining the dataset under its own terms. Retrieval:

```bash
python -m benchmarks.longmemeval_citefold_benchmark \
  /path/to/longmemeval_s_cleaned.json \
  --manifest benchmarks/longmemeval_manifest.json \
  --output-json /tmp/retrieval.json \
  --output-md /tmp/retrieval.md
```

QA generation and judging make paid external model calls. Inspect `--help`, pin model/provider where possible, record the manifest, and never commit API keys or raw private traces.

See [`benchmarks/README.md`](https://github.com/VIAIM/citefold/blob/main/benchmarks/README.md) for runner details and result-handling rules.

## Reporting rules

When sharing a score, include:

1. benchmark and dataset hash;
2. Citefold commit or release;
3. metric name and sample count;
4. reader/judge model and provider if applicable;
5. network/model-call count;
6. environment and relevant options;
7. the caveat that limits the claim.

Do not combine retrieval recall, QA accuracy, and deterministic contract success into one “memory accuracy” number.
Do not report Track B calculation-harness output as user benefit or a qualifying
product-effect result.
