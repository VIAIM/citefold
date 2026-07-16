# Citefold

**Every memory remembers its source.**

Citefold is an evidence-backed multimodal memory library for agents. It stores raw inputs separately from observations and long-term claims, then returns bounded `MemoryPack` objects with identity scope, coverage, and citations.

## Start here

- [Quickstart](quickstart.md) — install from source and run local ingest → recall.
- [Concepts](concepts.md) — understand evidence, candidates, records, and coverage.
- [Architecture](architecture.md) — trace write, read, correction, and deletion paths.
- [CLI](cli.md) — use the current command surface.
- [Multimodal](multimodal.md) — register images, audio, and video safely.
- [OpenRouter](providers/openrouter.md) — enable optional extraction and embeddings.
- [Security](security.md) — know the trust boundaries before using private data.
- [Benchmarks](benchmarks.md) — read the measurements and their caveats.
- [Limitations](limitations.md) — decide whether Citefold fits your deployment.
- [Integrations](integrations.md) — add memory hooks to an agent loop.
- [Roadmap](roadmap.md) — see the path to a stable 1.0.

## What Citefold is

- An embeddable Python library with local, identity-scoped persistence.
- A provenance model that separates assets, observations, episodes, candidates, and records.
- Hybrid local retrieval followed by an evidence gate.
- A lifecycle for candidate approval, correction, archival, decay, and deletion.
- A provider-optional system: local text and supplied media observations work without a model call.

## What Citefold is not

- A hosted memory service or an authentication layer.
- A vector database or general-purpose document RAG system.
- An autonomous agent framework.
- A complete simulation of human memory.
- A claim of independently validated OCR, ASR, or video understanding.

## Build these docs

The site uses [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/):

```bash
python -m pip install -e ".[docs]"
mkdocs serve
```

The Markdown files remain readable directly on GitHub; the documentation site is presentation, not a separate source of truth.
