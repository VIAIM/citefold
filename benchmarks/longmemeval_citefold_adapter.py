"""Citefold adapter for the public LongMemEval benchmark.

Protocol details are based on LongMemEval at the revision recorded in
``longmemeval_manifest.json``. LongMemEval is MIT-licensed; see
``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from citefold import MemoryScope, Citefold


DEFAULT_MANIFEST_PATH = Path(__file__).with_name("longmemeval_manifest.json")
DATE_DAY_PATTERN = re.compile(r"\s+\([A-Za-z]{3}\)")


class DatasetVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class CitefoldContext:
    context: str
    trace: dict[str, Any]


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def verify_dataset(dataset_path: Path, manifest_path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["dataset"]
    actual_sha256 = _sha256(dataset_path)
    actual_bytes = dataset_path.stat().st_size
    if actual_sha256 != expected["sha256"]:
        raise DatasetVerificationError(
            f"LongMemEval dataset SHA-256 mismatch: expected {expected['sha256']}, got {actual_sha256}"
        )
    if actual_bytes != expected["bytes"]:
        raise DatasetVerificationError(
            f"LongMemEval dataset size mismatch: expected {expected['bytes']}, got {actual_bytes}"
        )

    items = json.loads(dataset_path.read_text(encoding="utf-8"))
    question_types = Counter(item["question_type"] for item in items)
    abstention_questions = sum("_abs" in item["question_id"] for item in items)
    actual_shape = {
        "questions": len(items),
        "question_types": dict(sorted(question_types.items())),
        "abstention_questions": abstention_questions,
    }
    for key, actual in actual_shape.items():
        if actual != expected[key]:
            raise DatasetVerificationError(
                f"LongMemEval dataset {key} mismatch: expected {expected[key]!r}, got {actual!r}"
            )
    return {
        "path": dataset_path.name,
        "sha256": actual_sha256,
        "bytes": actual_bytes,
        **actual_shape,
        "revision": expected.get("revision"),
        "url": expected.get("url"),
    }


def build_citefold_context(
    item: dict[str, Any],
    root: Path,
    token_budget: int = 2200,
) -> CitefoldContext:
    first_date = _parse_longmemeval_date(item["haystack_dates"][0])
    clock = MutableClock(first_date)
    memory = Citefold(root, clock=clock)
    path_to_session_id: dict[str, str] = {}
    turns_ingested = 0
    ingest_started = time.perf_counter()

    for session_id, session_date, session in zip(
        item["haystack_session_ids"],
        item["haystack_dates"],
        item["haystack_sessions"],
    ):
        clock.current = _parse_longmemeval_date(session_date)
        clean_messages = _strip_answer_labels(session)
        turns_ingested += len(clean_messages)
        scope = _scope(item["question_id"], session_id)
        ingest_result = memory.ingest_chat(
            scope=scope,
            messages=clean_messages,
            source="longmemeval",
            metadata={
                "benchmark": "longmemeval_s_cleaned",
                "source_session_id": session_id,
                "source_date": session_date,
            },
            refresh_indexes=False,
        )
        for path in ingest_result.memory_paths:
            path_to_session_id[path] = session_id

    ingest_seconds = time.perf_counter() - ingest_started
    clock.current = _parse_longmemeval_date(item["question_date"])
    recall_started = time.perf_counter()
    pack = memory.recall(
        scope=_scope(item["question_id"], f"{item['question_id']}-question"),
        query=item["question"],
        token_budget=token_budget,
    )
    recall_seconds = time.perf_counter() - recall_started
    selected_session_ids = list(
        dict.fromkeys(
            path_to_session_id[node.path]
            for node in pack.selected_nodes
            if node.path in path_to_session_id
        )
    )
    trace = {
        "question_id": item["question_id"],
        "sessions_ingested": len(item["haystack_sessions"]),
        "turns_ingested": turns_ingested,
        "selected_nodes": [
            {"node_id": node.node_id, "path": node.path, "reason": node.reason}
            for node in pack.selected_nodes
        ],
        "selected_session_ids": selected_session_ids,
        "context_chars": len(pack.markdown),
        "token_budget": token_budget,
        "ingest_seconds": ingest_seconds,
        "recall_seconds": recall_seconds,
    }
    return CitefoldContext(context=pack.markdown, trace=trace)


def _scope(question_id: str, session_id: str) -> MemoryScope:
    return MemoryScope(
        tenant_id="longmemeval",
        user_id=question_id,
        namespace="benchmark",
        agent_id="citefold",
        session_id=session_id,
    )


def _strip_answer_labels(session: Any) -> list[dict[str, str]]:
    if not isinstance(session, list):
        raise ValueError("LongMemEval session must be a list of turns")
    cleaned: list[dict[str, str]] = []
    for turn in session:
        if not isinstance(turn, dict):
            raise ValueError("LongMemEval turn must be an object")
        cleaned.append({"role": str(turn.get("role", "unknown")), "content": str(turn.get("content", ""))})
    return cleaned


def _parse_longmemeval_date(value: str) -> datetime:
    normalized = DATE_DAY_PATTERN.sub("", value)
    return datetime.strptime(normalized, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
