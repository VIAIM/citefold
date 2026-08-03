# Limitations

The current release is an evidence-first alpha, not a production memory service. Treat these as design constraints, not fine print.

## API and storage stability

- API and on-disk schemas may change before 1.0.
- There is no general migration tool yet.
- Candidate list/approve/reject exists in both Python and CLI paths; a bulk or graphical review workflow is not included.
- Pin/unpin behavior is represented internally but has no public operation.

## Language and extraction

- Local text capture and recall are language-agnostic at the evidence level.
- The built-in deterministic direct-write parser recognizes only a narrow set of explicit Chinese preference/reminder phrases.
- Arbitrary semantic extraction needs supplied candidates or optional model consolidation.
- Multi-Episode consolidation currently batches selected observations; entity/time/topic grouping and novelty/contradiction quality have not been independently evaluated.

## Retrieval and budgets

- The default path uses lexical matching and SQLite FTS5; optional embeddings are a secondary, rebuildable signal.
- `token_budget` is a logical character proxy, not an exact provider token count.
- LongMemEval retrieval is strong, but end-to-end QA is materially lower and uneven by question type.
- Results have not been independently reproduced by a third party.

## Multimodal quality

- The offline benchmark uses supplied observations and does not measure OCR, ASR, vision, codec, or reader-LLM quality.
- OpenRouter endpoint availability and ZDR routing can change.
- Audio normalization and video extraction depend on external `ffmpeg`/`ffprobe` executables.
- Video uses scene frames and a conservative short-clip fallback; it is not general video understanding.
- Missing provider confidence is stored as `0.0`, so such output will not silently become stable memory.

## Security and privacy

- Citefold has no authentication, remote authorization service, or application-level encryption at rest.
- It is not a sandbox for malicious code in the same Python process.
- JSONL ledgers are auditable but not cryptographically tamper-proof.
- ZDR routing is not a compliance certification.
- Hard deletion does not erase backups, exported logs, or external provider copies.

## Operations and scale

- Current locking is designed for local process/thread coordination; network-filesystem semantics remain unverified.
- Windows multi-process locking parity is not yet established.
- Distributed replication, sharding, high availability, and multi-node consistency are not implemented.
- There is no published scale, latency, storage-growth, or cost envelope yet.
- SQLite and filesystem projections are appropriate for embedded use, not a claim of high-throughput service readiness.

## Biological analogy

Citefold implements explicit episodes, consolidation, cues, reinforcement, decay, and forgetting. It does not reproduce emotional modulation, sleep-dependent consolidation, reconstructive recall, implicit memory, sensorimotor learning, or consciousness. “Human-memory-inspired” describes design prompts, not measured equivalence to the brain.

## Evaluation gaps before 1.0

- real users over weeks or months;
- real OCR/ASR/video quality and error propagation;
- adversarial media and prompt-injection evaluation beyond deterministic fixtures;
- large-scale concurrency and crash recovery;
- independent security/threat-model review;
- migration compatibility across releases;
- third-party benchmark reproduction.

If any of these are hard requirements, treat Citefold as a component to evaluate—not a finished solution.
