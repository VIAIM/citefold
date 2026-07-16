# LongMemEval-S Citefold Public Retrieval Diagnostic

Generated at: `2026-07-16T10:20:52.874185+00:00`

This uses the public LongMemEval-S cleaned dataset. It is not the end-to-end QA score.

## Dataset

- SHA-256: `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`
- Evaluated questions: 470
- Excluded abstention questions: 30

## Overall

| metric | @1 | @3 | @5 | @10 |
|--------|------:|------:|------:|------:|
| recall_any | 0.8723 | 0.9511 | 0.9723 | 0.9723 |
| recall_all | 0.3064 | 0.7915 | 0.8447 | 0.8447 |
| ndcg_any | 0.8723 | 0.8838 | 0.8944 | 0.8938 |

- MRR: 0.9139

## Caveats

- This is a public-dataset retrieval diagnostic, not the end-to-end LongMemEval QA score.
- Questions ending in _abs are excluded, matching the official retrieval evaluator.
- Only session ids represented by nodes returned in the MemoryPack are scored as retrieved.
