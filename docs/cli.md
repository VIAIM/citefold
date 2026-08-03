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

`--root` must name a dedicated Citefold directory. Do not put source media, application uploads, logs, or unrelated files inside it. Storage commands operate on the whole root; the tenant/user/namespace options do not narrow a migration, backup, or restore.

## Commands

### `status`

```bash
citefold --root /path/to/citefold-data status
```

`status` inspects the root without writing. It reports the recognized state (`uninitialized`, `current`, `legacy`, `future`, `recovery_required`, or `invalid`), detected and current schema versions, scope count, store/generation IDs when available, and an issue when the root cannot be used safely.

### `migrate`

Always preflight a legacy v0.1 root first:

```bash
citefold --root /path/to/citefold-data migrate --dry-run
```

The dry run performs semantic and integrity checks and reports `ready`, `blockers`, `warnings`, the proposed backup path, counts, bytes, and a fingerprint without changing the storage root. Stop every v0.1 process before the preflight and migration; old processes do not participate in the v0.2 root lock. The actual migration also holds the v0.1 scope-writer and known ledger locks and aborts if canonical data changes despite them.

When `ready` is `true`, run:

```bash
citefold --root /path/to/citefold-data migrate

citefold --root /path/to/citefold-data migrate \
  --backup-to /secure/backups/citefold-before-v0.2.zip
```

Migration always creates and verifies a backup outside the root before adding schema 2 management metadata. It is additive: pre-existing v0.1 canonical files are not rewritten, and concurrent changes cause an abort without restoring older backup data over them. The backup remains available for explicit operator recovery only.

A current root returns `up_to_date`. Normal memory commands fail closed on legacy, future, recovery, and invalid states. Rerunning `migrate` after an interruption verifies the recorded backup, then either completes a committed manifest by removing stale owned state or removes only the uncommitted event/state and retries. It never automatically restores the whole root from the backup.

### `backup`

```bash
citefold --root /path/to/citefold-data backup

citefold --root /path/to/citefold-data backup \
  --output /secure/backups/citefold-2026-08-03.zip
```

The default archive is placed in a sibling `{root}.backups` directory. The command writes a ZIP manifest, hashes every included file, verifies the complete archive, and refuses an output inside the root or an existing output path. Backups include sensitive durable history; protect and expire them separately from the active store.

### `restore`

```bash
citefold --root /path/to/restored-citefold restore \
  /secure/backups/citefold-2026-08-03.zip

citefold --root /path/to/citefold-data restore \
  /secure/backups/citefold-2026-08-03.zip \
  --replace
```

The first form requires a missing or empty target. `--replace` is required for a non-empty target. Citefold verifies archive paths, declarations, hashes, and the store fingerprint before swapping directories. Replacement retains the previous root as a sibling path and returns it as `displaced_root`; it is not automatically deleted.

Before the directory swap, restore writes a sibling intent journal. If a crash leaves that journal, `status` and normal APIs return `recovery_required`. Rerun `restore` for the same archive transaction to roll the validated replacement forward. If the archive was lost after journaling but the journal and temporary replacement are intact, pass the same original archive path to complete recovery. A changed archive, a nonmatching digest, or inconsistent directory identity fails closed; a byte-identical archive at another path is accepted.

See [Storage, migration, backup, and restore](storage.md) for the full upgrade sequence, Python API, backup contents, recovery behavior, and local-POSIX boundary.

### `init`, `doctor`, and `demo`

```bash
citefold init
citefold doctor
citefold demo
```

`init` creates schema 2 root metadata plus the scoped storage/index when the root is missing or empty, `doctor` reports storage, FFmpeg, and OpenRouter readiness, and `demo` runs a local ingest → cited recall. `doctor` is read-only with respect to the root and does not make a model request. `init` does not silently adopt an unrelated non-empty directory or migrate legacy data.

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
