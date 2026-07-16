# OpenRouter provider

OpenRouter is an optional adapter for model-backed observations, consolidation, and embeddings. Citefold's local storage, supplied observations, lexical retrieval, and SQLite FTS do not require it.

## Configure the client

The key is read only from the current process environment:

```bash
export OPENROUTER_API_KEY=your-key
```

```python
from citefold import Citefold
from citefold.openrouter import OpenRouterClient

memory = Citefold(".citefold", openrouter=OpenRouterClient())
```

No key parameter is accepted by the constructor. This reduces accidental serialization into config, logs, or ledgers; the host still owns safe secret management.

## Default model roles

The current source defaults are:

| Role | Model slug | Used for |
|---|---|---|
| Observation | `google/gemini-2.5-flash-lite` | image, frame, and short-video observation |
| ASR | `qwen/qwen3-asr-flash-2026-02-10` | audio transcription |
| Consolidation | `qwen/qwen3.7-plus` | candidate extraction from episodes |
| Embedding | `qwen/qwen3-embedding-8b` | optional rebuildable index signal |

Model presence does not guarantee availability under a particular provider, region, account, or privacy route. Check OpenRouter at deployment time.

Override roles explicitly:

```python
from citefold.openrouter import OpenRouterClient, OpenRouterModels

client = OpenRouterClient(
    models=OpenRouterModels(
        observation="provider/vision-model",
        asr="provider/asr-model",
        consolidation="provider/text-model",
        embedding="provider/embedding-model",
    )
)
```

The selected models must support the exact endpoint and structured-output features used by Citefold.

## Privacy routing

Every request sends these provider constraints:

```json
{
  "zdr": true,
  "data_collection": "deny",
  "require_parameters": true
}
```

Citefold does not retry by removing or weakening them. A route that cannot satisfy the constraints fails. Depending on the operation, the caller receives an explicit error or the asset/consolidation batch remains pending.

This is a routing policy, not a legal compliance certification. Review the selected provider's actual retention terms and your own data classification before transmitting private data.

## Structured output and untrusted media

Image and video calls request strict JSON schemas. Their system message treats media and extracted text as untrusted evidence and forbids following instructions found inside it. Returned values are validated before they become Observations; they still do not become trusted long-term records automatically.

## Consolidation

```python
pending = memory.consolidate(scope, episode_ids=[episode_id])
for candidate in pending:
    print(candidate.candidate_id, candidate.status)
```

Model candidates remain pending. Inspect and approve them with `memory.approve_candidate(...)`. A constrained model route that is unavailable leaves a sanitized pending consolidation entry for later retry.

## Embeddings

```python
memory.rebuild(scope, embeddings=True)
```

This sends index text to the configured embedding route and stores vectors only in the rebuildable SQLite index. An embedding request failure is explicit; run `memory.rebuild(scope)` to rebuild the local lexical/FTS index without external calls.

## CLI

`--openrouter` is a global option and appears before the command:

```bash
citefold --openrouter ingest-image whiteboard.png
citefold --openrouter consolidate
citefold --openrouter rebuild --embeddings
```

The adapter writes model-call audit metadata—including role, requested/actual model when reported, timing, outcome, and scope—to the scoped ledger. Do not treat audit metadata as proof that a provider complied with an external policy.
