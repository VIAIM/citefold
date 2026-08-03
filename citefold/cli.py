from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .core import Citefold
from .models import MemoryScope
from .openrouter import OpenRouterClient
from .storage import backup_store, inspect_store, migrate_store, restore_store


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    identity = parser.add_argument_group("memory location and identity scope")
    identity.add_argument(
        "--root",
        default=_env("CITEFOLD_ROOT") or str(Path.home() / ".citefold"),
        help="Memory root (default: ~/.citefold; or set CITEFOLD_ROOT).",
    )
    for option, environment, local_default, description in (
        ("tenant-id", "CITEFOLD_TENANT_ID", "local", "Tenant/organization boundary."),
        ("user-id", "CITEFOLD_USER_ID", "me", "User boundary."),
        ("namespace", "CITEFOLD_NAMESPACE", "personal", "Memory namespace, for example personal or work."),
        ("agent-id", "CITEFOLD_AGENT_ID", "citefold-cli", "Agent performing this operation."),
        ("session-id", "CITEFOLD_SESSION_ID", "default", "Current session identifier."),
    ):
        identity.add_argument(
            f"--{option}",
            default=_env(environment) or local_default,
            help=f"{description} (default: {local_default}; or set {environment}).",
        )
    identity.add_argument(
        "--openrouter",
        action="store_true",
        help="Use OpenRouter for model-backed operations; reads OPENROUTER_API_KEY from the environment.",
    )


def _add_media_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path, help="Local media file.")
    parser.add_argument("--source", default="cli_upload", help="Evidence source label.")
    parser.add_argument("--mime-type", help="Override the MIME type inferred from the filename.")
    parser.add_argument("--metadata-json", type=Path, help="JSON object containing capture metadata.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="citefold",
        description="Evidence-backed multimodal memory for agents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_identity_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="Initialize the local Citefold memory store.")
    initialize.set_defaults(handler=_handle_init)

    doctor = commands.add_parser("doctor", help="Check local storage and optional media/model tools.")
    doctor.set_defaults(handler=_handle_doctor)

    status = commands.add_parser("status", help="Inspect the storage schema without modifying it.")
    status.set_defaults(handler=_handle_status)

    migrate = commands.add_parser("migrate", help="Preflight or migrate the storage schema.")
    migrate.add_argument("--dry-run", action="store_true", help="Validate and print the plan without writing.")
    migrate.add_argument("--backup-to", type=Path, help="Write the required pre-migration backup here.")
    migrate.set_defaults(handler=_handle_migrate)

    backup = commands.add_parser("backup", help="Create and verify a storage backup.")
    backup.add_argument("--output", type=Path, help="Backup archive path; defaults beside the storage root.")
    backup.set_defaults(handler=_handle_backup)

    restore = commands.add_parser("restore", help="Restore a verified storage backup.")
    restore.add_argument("archive", type=Path, help="Backup archive to restore.")
    restore.add_argument("--replace", action="store_true", help="Replace a non-empty storage root.")
    restore.set_defaults(handler=_handle_restore)

    demo = commands.add_parser("demo", help="Run a local ingest-and-recall demonstration.")
    demo.set_defaults(handler=_handle_demo)

    ingest_text = commands.add_parser("ingest-text", help="Ingest a text observation.")
    ingest_text.add_argument("text", nargs="?", help="Text to ingest; stdin is used when omitted.")
    ingest_text.add_argument("--file", type=Path, help="Read text from a UTF-8 file instead.")
    ingest_text.add_argument("--source", default="cli_text", help="Evidence source label.")
    ingest_text.add_argument("--mode", choices=("text", "voice"), default="text")
    ingest_text.add_argument("--not-final", action="store_true", help="Mark a voice fragment as interim.")
    ingest_text.add_argument("--role", default="user", help="Observation role; defaults to user.")
    ingest_text.add_argument("--metadata-json", type=Path, help="JSON object with additional metadata.")
    ingest_text.set_defaults(handler=_handle_ingest_text)

    ingest_image = commands.add_parser("ingest-image", help="Register an image and observations.")
    _add_media_arguments(ingest_image)
    ingest_image.add_argument(
        "--observations-json",
        type=Path,
        help="JSON array of recorded observations; otherwise use OpenRouter or leave pending.",
    )
    ingest_image.set_defaults(handler=_handle_ingest_image)

    ingest_audio = commands.add_parser("ingest-audio", help="Register audio and transcript segments.")
    _add_media_arguments(ingest_audio)
    ingest_audio.add_argument(
        "--transcript-json",
        type=Path,
        help="JSON array of {start_ms,end_ms,text,confidence} segments.",
    )
    ingest_audio.add_argument("--duration-ms", type=int, help="Known media duration in milliseconds.")
    ingest_audio.set_defaults(handler=_handle_ingest_audio)

    ingest_video = commands.add_parser("ingest-video", help="Register video on one audio/visual timeline.")
    _add_media_arguments(ingest_video)
    ingest_video.add_argument("--transcript-json", type=Path, help="JSON array of transcript segments.")
    ingest_video.add_argument(
        "--frames-json",
        type=Path,
        help="JSON array of {timestamp_ms,content,confidence} frame observations.",
    )
    ingest_video.add_argument("--duration-ms", type=int, help="Known media duration in milliseconds.")
    ingest_video.set_defaults(handler=_handle_ingest_video)

    recall = commands.add_parser("recall", help="Build a bounded, cited MemoryPack.")
    recall.add_argument("query")
    recall.add_argument("--mode", choices=("text", "voice"), default="text")
    recall.add_argument("--token-budget", type=int, default=2200)
    recall.add_argument("--include-archived", action="store_true")
    recall.add_argument("--markdown", action="store_true", help="Print only MemoryPack Markdown.")
    recall.set_defaults(handler=_handle_recall)

    consolidate = commands.add_parser("consolidate", help="Extract pending memories from episodes.")
    consolidate.add_argument(
        "--episode-id",
        action="append",
        dest="episode_ids",
        help="Episode to consolidate; repeat to select several. Defaults to unprocessed episodes.",
    )
    consolidate.set_defaults(handler=_handle_consolidate)

    correct = commands.add_parser("correct", help="Create a new version of an active memory record.")
    correct.add_argument("record_id")
    correct.add_argument("content", nargs="?", help="Corrected content; stdin is used when omitted.")
    correct.add_argument("--file", type=Path, help="Read corrected content from a UTF-8 file.")
    correct.add_argument("--reason", default="explicit user correction")
    correct.set_defaults(handler=_handle_correct)

    pin = commands.add_parser("pin", help="Exempt an active memory record from decay.")
    pin.add_argument("record_id")
    pin.add_argument("--reason", default="explicit pin")
    pin.set_defaults(handler=_handle_pin)

    unpin = commands.add_parser("unpin", help="Allow an active memory record to decay normally.")
    unpin.add_argument("record_id")
    unpin.add_argument("--reason", default="explicit unpin")
    unpin.set_defaults(handler=_handle_unpin)

    archive = commands.add_parser("archive", help="Archive a memory record without deleting evidence.")
    archive.add_argument("record_id")
    archive.add_argument("--reason", default="explicit archive")
    archive.set_defaults(handler=_handle_archive)

    forget = commands.add_parser("forget", help="Tombstone evidence and invalidate dependent memory.")
    forget.add_argument("evidence_ref", help="Exact asset:, observation:, episode:, or legacy evidence anchor.")
    forget.add_argument("--hard", action="store_true", help="Also remove referenced asset bytes.")
    forget.add_argument("--reason", default="explicit user deletion")
    forget.set_defaults(handler=_handle_forget)

    rebuild = commands.add_parser("rebuild", help="Rebuild navigation and hybrid recall indexes.")
    rebuild.add_argument(
        "--embeddings",
        action="store_true",
        help="Build embeddings through OpenRouter; requires global --openrouter.",
    )
    rebuild.set_defaults(handler=_handle_rebuild)

    list_records = commands.add_parser("list", help="List current memory records.")
    list_records.add_argument("--all", action="store_true", help="Include archived, superseded, and deleted records.")
    list_records.set_defaults(handler=_handle_list)

    candidates = commands.add_parser("candidates", help="Review pending memory candidates.")
    candidate_commands = candidates.add_subparsers(dest="candidate_command", required=True)

    candidate_list = candidate_commands.add_parser("list", help="List memory candidates.")
    candidate_list.add_argument(
        "--status",
        choices=("pending", "approved", "rejected", "ignored"),
        help="Return only candidates in this state.",
    )
    candidate_list.set_defaults(handler=_handle_candidates_list)

    candidate_approve = candidate_commands.add_parser("approve", help="Approve a pending candidate.")
    candidate_approve.add_argument("candidate_id")
    candidate_approve.set_defaults(handler=_handle_candidate_approve)

    candidate_reject = candidate_commands.add_parser("reject", help="Reject a pending candidate.")
    candidate_reject.add_argument("candidate_id")
    candidate_reject.add_argument("--reason", default="explicit user rejection")
    candidate_reject.set_defaults(handler=_handle_candidate_reject)
    return parser


def _scope(args: argparse.Namespace) -> MemoryScope:
    return MemoryScope(
        tenant_id=args.tenant_id,
        user_id=args.user_id,
        namespace=args.namespace,
        agent_id=args.agent_id,
        session_id=args.session_id,
    )


def _memory(args: argparse.Namespace) -> Citefold:
    client = OpenRouterClient() if args.openrouter else None
    return Citefold(Path(args.root).expanduser(), openrouter=client)


def _load_json(path: Path | None, expected: type) -> Any:
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, expected):
        raise ValueError(f"{path} must contain a JSON {expected.__name__}")
    return value


def _read_text(value: str | None, path: Path | None) -> str:
    if value is not None and path is not None:
        raise ValueError("provide text or --file, not both")
    if path is not None:
        text = path.read_text(encoding="utf-8")
    elif value is not None:
        text = value
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        raise ValueError("text is required as an argument, --file, or stdin")
    if not text.strip():
        raise ValueError("text must not be empty")
    return text


def _metadata(args: argparse.Namespace) -> dict[str, Any]:
    value = _load_json(getattr(args, "metadata_json", None), dict)
    return value or {}


def _handle_init(args: argparse.Namespace) -> Any:
    rebuilt = _memory(args).rebuild(_scope(args))
    return {
        "status": "initialized",
        "root": str(Path(args.root).expanduser()),
        "scope": _scope(args).as_record(),
        "index": rebuilt,
    }


def _handle_doctor(args: argparse.Namespace) -> Any:
    root = Path(args.root).expanduser()
    storage = inspect_store(root)
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    root_writable = probe.is_dir() and os.access(probe, os.W_OK)
    if root.exists() and not root.is_dir():
        root_writable = False
    if not root_writable:
        raise ValueError(f"memory root is not writable: {root}")
    scope = _scope(args)
    scope_root = (
        root
        / "tenants"
        / scope.tenant_id
        / "users"
        / scope.user_id
        / "namespaces"
        / scope.namespace
    )
    return {
        "status": "ok",
        "version": __version__,
        "root": str(root),
        "storage": storage,
        "scope": scope.as_record(),
        "checks": {
            "root_writable": root_writable,
            "scope_initialized": (scope_root / "ledgers").is_dir(),
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "ffprobe_available": shutil.which("ffprobe") is not None,
            "openrouter_configured": bool(_env("OPENROUTER_API_KEY")),
        },
    }


def _handle_status(args: argparse.Namespace) -> Any:
    return inspect_store(Path(args.root).expanduser())


def _handle_migrate(args: argparse.Namespace) -> Any:
    return migrate_store(
        Path(args.root).expanduser(),
        backup_path=args.backup_to,
        dry_run=args.dry_run,
    )


def _handle_backup(args: argparse.Namespace) -> Any:
    return backup_store(Path(args.root).expanduser(), destination=args.output)


def _handle_restore(args: argparse.Namespace) -> Any:
    return restore_store(
        Path(args.root).expanduser(),
        args.archive,
        replace=args.replace,
    )


def _handle_demo(args: argparse.Namespace) -> Any:
    memory = _memory(args)
    scope = _scope(args)
    ingested = memory.ingest_text(
        scope,
        "The launch codeword is ORCHID-91.",
        source="citefold_demo",
        metadata={"role": "user"},
    )
    pack = memory.recall(scope, "What is the launch codeword?")
    return {"status": "ok", "ingest": ingested, "memory_pack": pack}


def _handle_ingest_text(args: argparse.Namespace) -> Any:
    metadata = {**_metadata(args), "role": args.role}
    return _memory(args).ingest_text(
        _scope(args),
        _read_text(args.text, args.file),
        source=args.source,
        mode=args.mode,
        final=not args.not_final,
        metadata=metadata,
    )


def _handle_ingest_image(args: argparse.Namespace) -> Any:
    return _memory(args).ingest_image(
        _scope(args),
        args.path,
        source=args.source,
        observations=_load_json(args.observations_json, list),
        mime_type=args.mime_type,
        metadata=_metadata(args),
    )


def _handle_ingest_audio(args: argparse.Namespace) -> Any:
    return _memory(args).ingest_audio(
        _scope(args),
        args.path,
        source=args.source,
        transcript_segments=_load_json(args.transcript_json, list),
        mime_type=args.mime_type,
        duration_ms=args.duration_ms,
        metadata=_metadata(args),
    )


def _handle_ingest_video(args: argparse.Namespace) -> Any:
    return _memory(args).ingest_video(
        _scope(args),
        args.path,
        source=args.source,
        transcript_segments=_load_json(args.transcript_json, list),
        frame_observations=_load_json(args.frames_json, list),
        mime_type=args.mime_type,
        duration_ms=args.duration_ms,
        metadata=_metadata(args),
    )


def _handle_recall(args: argparse.Namespace) -> Any:
    pack = _memory(args).recall(
        _scope(args),
        args.query,
        mode=args.mode,
        token_budget=args.token_budget,
        include_archived=args.include_archived,
    )
    return pack.markdown if args.markdown else pack


def _handle_consolidate(args: argparse.Namespace) -> Any:
    return _memory(args).consolidate(_scope(args), episode_ids=args.episode_ids)


def _handle_correct(args: argparse.Namespace) -> Any:
    return _memory(args).correct(
        _scope(args),
        args.record_id,
        _read_text(args.content, args.file),
        reason=args.reason,
    )


def _handle_pin(args: argparse.Namespace) -> Any:
    return _memory(args).pin(_scope(args), args.record_id, reason=args.reason)


def _handle_unpin(args: argparse.Namespace) -> Any:
    return _memory(args).unpin(_scope(args), args.record_id, reason=args.reason)


def _handle_archive(args: argparse.Namespace) -> Any:
    return _memory(args).archive(_scope(args), args.record_id, reason=args.reason)


def _handle_forget(args: argparse.Namespace) -> Any:
    return _memory(args).forget(
        _scope(args),
        args.evidence_ref,
        hard=args.hard,
        reason=args.reason,
    )


def _handle_rebuild(args: argparse.Namespace) -> Any:
    if args.embeddings and not args.openrouter:
        raise ValueError("--embeddings requires global --openrouter")
    return _memory(args).rebuild(_scope(args), embeddings=args.embeddings)


def _handle_list(args: argparse.Namespace) -> Any:
    return _memory(args).list_records(_scope(args), include_inactive=args.all)


def _handle_candidates_list(args: argparse.Namespace) -> Any:
    return _memory(args).list_candidates(_scope(args), status=args.status)


def _handle_candidate_approve(args: argparse.Namespace) -> Any:
    return _memory(args).approve_candidate(_scope(args), args.candidate_id)


def _handle_candidate_reject(args: argparse.Namespace) -> Any:
    return _memory(args).reject_candidate(
        _scope(args),
        args.candidate_id,
        reason=args.reason,
    )


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        result = args.handler(args)
        if isinstance(result, str) and getattr(args, "markdown", False):
            sys.stdout.write(result)
        else:
            sys.stdout.write(json.dumps(_jsonable(result), ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        sys.stderr.write(f"citefold: error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
