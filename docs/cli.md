# CLI reference

The installed command is `citefold`. It emits UTF-8 JSON by default; `recall --markdown` emits only the rendered MemoryPack.

## Identity options

All identity options are global and must appear **before** the subcommand:

| Option | Environment variable | Meaning |
|---|---|---|
| `--root` | `CITEFOLD_ROOT` | Storage root; default `~/.citefold` |
| `--tenant-id` | `CITEFOLD_TENANT_ID` | Tenant/organization boundary; default `local` |
| `--user-id` | `CITEFOLD_USER_ID` | User boundary; default `me` |
| `--namespace` | `CITEFOLD_NAMESPACE` | Memory namespace; default `personal` |
| `--agent-id` | `CITEFOLD_AGENT_ID` | Agent provenance; default `citefold-cli` |
| `--session-id` | `CITEFOLD_SESSION_ID` | Session provenance; default `default` |
| `--openrouter` | — | Explicitly enable the optional OpenRouter client |

The defaults make local evaluation quick; production hosts should derive all scope fields from authenticated application state.

## Commands

### `init`, `doctor`, and `demo`

```bash
citefold init
citefold doctor
citefold demo
```

`init` creates the scoped storage/index, `doctor` reports storage, FFmpeg, and OpenRouter readiness, and `demo` runs a local ingest → cited recall. `doctor` reports configuration; it does not make a model request.

### `ingest-text`

```bash
citefold ingest-text 'The launch codename is ORCHID-77.' --source chat
citefold ingest-text --file meeting.txt --source meeting_note
printf '%s' 'final voice transcript' | citefold ingest-text --mode voice
citefold ingest-text 'interim transcript' --mode voice --not-final
```

Interim voice text remains a small working buffer; it is not promoted to a durable fact.

### `ingest-image`

Store an image with supplied observations:

```json
[
  {"content": "The whiteboard says ORCHID-7.", "confidence": 0.96, "locator": {}}
]
```

```bash
citefold ingest-image whiteboard.png \
  --source camera_upload \
  --observations-json image-observations.json
```

Without supplied observations or `--openrouter`, the asset is stored and returned as pending.

### `ingest-audio`

```json
[
  {"start_ms": 100, "end_ms": 1850, "text": "Send the brief Friday.", "confidence": 0.94}
]
```

```bash
citefold ingest-audio meeting.wav \
  --source recorder \
  --transcript-json transcript.json
```

Audio processing can use `ffmpeg`/`ffprobe`. Supplied transcripts preserve absolute time ranges.

### `ingest-video`

```bash
citefold ingest-video meeting.mp4 \
  --source meeting_upload \
  --duration-ms 5000 \
  --transcript-json transcript.json \
  --frames-json frames.json
```

Each frame item has `timestamp_ms`, `content`, and `confidence`. Transcript, subtitle, and frame observations share one timeline.

### `recall`

```bash
citefold recall 'What are my open tasks?'
citefold recall 'What are my open tasks?' --token-budget 1200 --markdown
citefold recall 'Show archived context for audit' --include-archived
```

The minimum logical token budget is 256. Archived records are excluded by default.

### `consolidate`

```bash
citefold --openrouter consolidate
citefold --openrouter consolidate --episode-id episode_example
```

Model-produced candidates remain pending until reviewed.

### `candidates`

```bash
citefold candidates list
citefold candidates list --status pending
citefold candidates approve candidate_example
citefold candidates reject candidate_example --reason 'not a durable user fact'
```

Approval activates a pending candidate through the same policy/evidence validation path as the Python API. Rejection appends a rejected state and preserves its audit history.

### `correct`

```bash
citefold correct mem_example \
  'Alex now prefers Monday follow-ups.' \
  --reason 'explicit user correction'
```

Correction appends a revision and creates a new version.

### `pin` and `unpin`

```bash
citefold pin mem_example --reason 'keep this preference stable'
citefold unpin mem_example --reason 'resume normal decay'
```

Each real state change appends an auditable revision; repeating the same command does not append a duplicate revision, while its audit event is marked `changed: false`. Only active records can be pinned or unpinned. Pinning freezes the current access strength against decay; after unpinning, decay resumes from the unpin time without retroactively charging the pinned interval. Pinning does not increase confidence, guarantee recall, or prevent correction, archival, evidence invalidation, or deletion.

### `archive`

```bash
citefold archive mem_example --reason 'project completed'
```

Archival changes visibility but retains evidence.

### `forget`

```bash
citefold forget observation:obs_example --reason 'user requested deletion'
citefold forget asset:asset_example --hard
```

`--hard` additionally deletes the referenced original and derived asset bytes. See [Security](security.md) for the deletion boundary.

### `rebuild`

```bash
citefold rebuild
citefold --openrouter rebuild --embeddings
```

The first form stays local. The embedding form sends indexed text to the configured model route.

### `list`

```bash
citefold list
citefold list --all
```

`--all` includes archived, superseded, and deleted record states.

## Exit behavior

Successful commands exit `0`. Handled filesystem, validation, lookup, and runtime failures print `citefold: error: ...` to stderr and exit `1`. Unexpected programming errors are not intentionally converted into success-shaped JSON.
