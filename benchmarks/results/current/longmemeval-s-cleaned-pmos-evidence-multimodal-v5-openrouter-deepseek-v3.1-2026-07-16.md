# LongMemEval-S Cleaned QA Benchmark Report

Generated at: `2026-07-16T05:03:16.281122+00:00`

## Scope

This report is an end-to-end QA evaluation using generated hypotheses and an LLM judge.

## Overall

- Accuracy: 0.6180
- Answerable accuracy: 0.6043 (470)
- Abstention accuracy: 0.8333 (30)
- Hypotheses: 500
- Coverage: 500/500 (complete)
- Context mode: `personal-memory-os`
- Reader model: `deepseek/deepseek-chat-v3.1`
- Judge model: `deepseek/deepseek-chat-v3.1`

- Official judge compatible: no
- Dataset SHA-256: `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`

## By Question Type

| question_type | n | accuracy |
|---------------|--:|---------:|
| knowledge-update | 78 | 0.8205 |
| multi-session | 133 | 0.3835 |
| single-session-assistant | 56 | 0.5714 |
| single-session-preference | 30 | 0.3667 |
| single-session-user | 70 | 0.9000 |
| temporal-reasoning | 133 | 0.6617 |
