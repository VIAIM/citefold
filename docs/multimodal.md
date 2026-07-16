# Multimodal ingestion

Citefold accepts text, images, audio, and video. It keeps raw media separate from extracted observations so a model description never replaces the source bytes.

## Trust contract

All media-derived text is treated as untrusted evidence:

- it is stored with its producer and confidence;
- it keeps a locator such as a character range, time range, or frame timestamp;
- it cannot grant permissions or directly execute a procedure;
- model-derived long-term claims remain pending by default;
- it is rendered inside `UNTRUSTED EVIDENCE DATA` when recalled.

The host must supply trustworthy `source`, user identity, and capture metadata. Do not let arbitrary external content choose those fields.

## Images

Register an image with an already-recorded OCR or vision observation:

```python
result = memory.ingest_image(
    scope,
    "whiteboard.png",
    source="camera_upload",
    observations=[
        {
            "content": "The whiteboard says launch codename ORCHID-7.",
            "confidence": 0.96,
            "locator": {},
        }
    ],
)
```

The original image becomes an `Asset`; each supplied item becomes an `Observation` tied back to that asset. Without `observations` or a configured model client, Citefold still stores the image and returns it as pending.

Run the offline text + image example:

```bash
python examples/multimodal.py
```

## Audio

Supply timestamped transcript segments when ASR is performed elsewhere:

```python
result = memory.ingest_audio(
    scope,
    "meeting.wav",
    source="meeting_recorder",
    duration_ms=5_000,
    transcript_segments=[
        {
            "start_ms": 800,
            "end_ms": 2_400,
            "text": "Send the launch brief on Friday.",
            "confidence": 0.94,
        }
    ],
)
```

When available, `ffmpeg` normalizes audio to 16 kHz mono WAV. Long model-backed audio is split into chunks of at most five minutes while preserving absolute timestamps. If media tools fail, the error is reported in `IngestResult.errors`; Citefold does not invent a transcript.

A provider that does not return confidence is represented as `0.0`, which prevents the transcript from silently updating stable profile memory.

## Video

Video can accept both transcript segments and frame observations on one timeline:

```python
result = memory.ingest_video(
    scope,
    "meeting.mp4",
    source="meeting_upload",
    duration_ms=5_000,
    transcript_segments=[
        {"start_ms": 1_000, "end_ms": 2_400, "text": "Friday deadline.", "confidence": 0.92}
    ],
    frame_observations=[
        {"timestamp_ms": 1_800, "content": "The slide names Maya as owner.", "confidence": 0.97}
    ],
)
```

With the optional media/model path, Citefold can:

1. extract an audio track;
2. read embedded subtitles;
3. select scene-change keyframes;
4. request time-localized observations;
5. use a short clip only when keyframes yielded no useful visual observation.

The short-clip behavior is a conservative fallback, not dynamic video understanding. A whole-video summary is never the only evidence.

## Realtime voice

Use the text API with `mode="voice"`:

```python
memory.ingest_text(
    scope,
    "interim transcript",
    source="voice_transcript",
    mode="voice",
    final=False,
)

memory.ingest_text(
    scope,
    "final transcript",
    source="voice_transcript",
    mode="voice",
    final=True,
)
```

Only the latest five unfinished fragments are materialized as a working buffer for the same agent/session. They may appear as quoted working evidence, but do not become supported durable claims. A final fragment creates an Episode and tombstones matching partials.

## Model-backed extraction

Pass an explicit `OpenRouterClient` to enable image observation, ASR, video observation, consolidation, or embeddings. Provider routing is opt-in and fails closed on the configured privacy constraints. See [OpenRouter](providers/openrouter.md).

## What the current benchmark proves

The checked-in multimodal benchmark uses pre-supplied observations and zero network/model calls. It validates evidence linkage, time alignment, scope, conflict, prompt-injection handling, correction, and deletion. It does **not** validate OCR, ASR, visual model, codec, or reader-LLM quality. See [Benchmarks](benchmarks.md).
