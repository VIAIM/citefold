from __future__ import annotations

import json
import math
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

from .models import MemoryScope
from .store import LedgerStore


TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?|[\u4e00-\u9fff]+")


class Embedder(Protocol):
    def embed(self, inputs: list[str]) -> list[list[float]]:
        ...


@dataclass(frozen=True)
class IndexedDocument:
    doc_id: str
    path: str
    kind: str
    content: str
    evidence_refs: list[str]
    metadata: dict[str, Any]


class HybridRecallIndex:
    """Rebuildable SQLite FTS/embedding index over file-ledger truth."""

    def __init__(self, store: LedgerStore) -> None:
        self.store = store
        self._snapshot: ContextVar[dict[str, list[IndexedDocument]] | None] = ContextVar(
            f"recall_snapshot_{id(self)}",
            default=None,
        )

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        token = self._snapshot.set({})
        try:
            yield
        finally:
            self._snapshot.reset(token)

    def path(self, scope: MemoryScope) -> Path:
        return self.store.scope_root(scope) / "indexes" / "memory.sqlite3"

    def is_stale(self, scope: MemoryScope) -> bool:
        index_path = self.path(scope)
        if not index_path.exists():
            return True
        index_mtime = index_path.stat().st_mtime
        root = self.store.scope_root(scope)
        sources = list(
            root / "ledgers" / name
            for name in ("observations.jsonl", "episodes.jsonl", "records.jsonl", "revisions.jsonl", "deletions.jsonl")
        )
        return any(path.exists() and path.stat().st_mtime > index_mtime for path in sources)

    def rebuild(self, scope: MemoryScope, embedder: Embedder | None = None) -> dict[str, Any]:
        self.store.ensure_scope(scope)
        documents, trusted_records = self._documents(scope)
        vectors: list[list[float] | None] = [None] * len(documents)
        target = self.path(scope)
        if embedder is not None and documents:
            embedded = embedder.embed([document.content for document in documents])
            if len(embedded) != len(documents):
                raise ValueError("Embedding provider returned the wrong number of vectors")
            vectors = embedded
        elif target.exists():
            existing = self._existing_embeddings(target)
            vectors = [existing.get((document.doc_id, document.content)) for document in documents]

        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".sqlite3", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            connection = sqlite3.connect(temporary_path)
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute(
                    "CREATE TABLE documents ("
                    "doc_id TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, "
                    "evidence_refs TEXT NOT NULL, embedding TEXT)"
                )
                connection.execute(
                    "CREATE VIRTUAL TABLE documents_fts USING fts5("
                    "doc_id UNINDEXED, path UNINDEXED, kind UNINDEXED, search_content)"
                )
                connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                for document, vector in zip(documents, vectors):
                    connection.execute(
                        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            document.doc_id,
                            document.path,
                            document.kind,
                            document.content,
                            json.dumps(document.evidence_refs, ensure_ascii=False),
                            json.dumps(vector, allow_nan=False) if vector is not None else None,
                        ),
                    )
                    expanded = document.content + "\n" + " ".join(self.tokens(document.content))
                    connection.execute(
                        "INSERT INTO documents_fts VALUES (?, ?, ?, ?)",
                        (document.doc_id, document.path, document.kind, expanded),
                    )
                metadata = {
                    "rebuilt_at": datetime.now(timezone.utc).isoformat(),
                    "documents": str(len(documents)),
                    "embeddings": str(sum(vector is not None for vector in vectors)),
                    "canonical_source": "jsonl_revision_ledger_and_evidence_files",
                }
                connection.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
                connection.commit()
            finally:
                connection.close()
            temporary_path.replace(target)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return {
            "documents": len(documents),
            "embeddings": sum(vector is not None for vector in vectors),
            "trusted_records": trusted_records,
            "path": str(target),
        }

    def rank_episodes(
        self,
        scope: MemoryScope,
        query: str,
        lexical: list[tuple[str, float]],
        embedder: Embedder | None,
        limit: int,
        query_terms: list[str] | None = None,
        allowed_fts_paths: set[str] | None = None,
        lexical_weight: float = 2.0,
    ) -> list[tuple[str, float, str]]:
        fts = self.search_fts(
            scope,
            query,
            kind="episode",
            limit=max(limit * 2, 10),
            query_terms=query_terms,
        )
        if allowed_fts_paths is not None:
            fts = [item for item in fts if item[0] in allowed_fts_paths]
        semantic = self.search_embeddings(scope, query, embedder, kind="episode", limit=max(limit * 2, 10))
        rankings = [
            ("lexical", [path for path, _score in lexical], lexical_weight),
            ("fts", [path for path, _score in fts], 1.0),
            ("embedding", [path for path, _score in semantic], 1.0),
        ]
        scores: dict[str, float] = {}
        channels: dict[str, list[str]] = {}
        for channel, paths, weight in rankings:
            for rank, path in enumerate(paths, start=1):
                scores[path] = scores.get(path, 0.0) + weight / (60 + rank)
                channels.setdefault(path, []).append(channel)
        return [
            (path, score, f"hybrid RRF ({', '.join(channels[path])})")
            for path, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        ]

    def search_fts(
        self,
        scope: MemoryScope,
        query: str,
        kind: str | None = None,
        limit: int = 10,
        query_terms: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        path = self.path(scope)
        terms = list(dict.fromkeys(query_terms if query_terms is not None else self.tokens(query)))
        if not path.exists() or not terms:
            return []
        expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:64])
        connection = sqlite3.connect(path)
        try:
            if kind is None:
                rows = connection.execute(
                    "SELECT path, bm25(documents_fts) FROM documents_fts "
                    "WHERE documents_fts MATCH ? ORDER BY bm25(documents_fts) LIMIT ?",
                    (expression, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT path, bm25(documents_fts) FROM documents_fts "
                    "WHERE documents_fts MATCH ? AND kind = ? ORDER BY bm25(documents_fts) LIMIT ?",
                    (expression, kind, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()
        return [(str(row[0]), -float(row[1])) for row in rows]

    def search_embeddings(
        self,
        scope: MemoryScope,
        query: str,
        embedder: Embedder | None,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[tuple[str, float]]:
        path = self.path(scope)
        if not path.exists() or embedder is None:
            return []
        connection = sqlite3.connect(path)
        try:
            if kind is None:
                rows = connection.execute(
                    "SELECT path, embedding FROM documents WHERE embedding IS NOT NULL"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT path, embedding FROM documents WHERE embedding IS NOT NULL AND kind = ?",
                    (kind,),
                ).fetchall()
        finally:
            connection.close()
        if not rows:
            return []
        try:
            query_vectors = embedder.embed([query])
        except Exception:
            return []
        if len(query_vectors) != 1:
            return []
        query_vector = query_vectors[0]
        try:
            scored = [
                (str(path_value), self._cosine(query_vector, json.loads(embedding)))
                for path_value, embedding in rows
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return [
            item
            for item in sorted(scored, key=lambda item: (-item[1], item[0]))
            if item[1] >= 0.20
        ][:limit]

    def episode_documents(self, scope: MemoryScope) -> list[IndexedDocument]:
        snapshot = self._snapshot.get()
        snapshot_key = str(self.store.scope_root(scope))
        if snapshot is not None and snapshot_key in snapshot:
            return snapshot[snapshot_key]
        root = self.store.scope_root(scope)
        observations = self.store.observations(scope)
        deleted = self.store.deleted_refs(scope)
        valid_observation_ids = self.store.valid_observation_ids(scope)
        documents: list[IndexedDocument] = []
        for episode in self.store.episodes(scope).values():
            episode_observation_ids = episode.get("observation_ids", [])
            if (
                not episode_observation_ids
                or episode["episode_id"] in deleted
                or f"episode:{episode['episode_id']}" in deleted
                or any(observation_id not in valid_observation_ids for observation_id in episode_observation_ids)
            ):
                continue
            path = episode.get("metadata", {}).get("markdown_path")
            if not path:
                matches = sorted((root / "episodes").rglob(f"*{episode['episode_id']}*.md"))
                if matches:
                    path = str(matches[0].relative_to(root))
            if not path:
                continue
            episode_observations = [
                observations[observation_id]
                for observation_id in episode.get("observation_ids", [])
                if observation_id in observations
            ]
            evidence_refs = [f"observation:{item['observation_id']}" for item in episode_observations]
            content = "\n".join(item.get("content", "") for item in episode_observations)
            documents.append(
                IndexedDocument(
                    doc_id=f"episode:{episode['episode_id']}",
                    path=str(path),
                    kind="episode",
                    content=content,
                    evidence_refs=evidence_refs,
                    metadata={
                        "episode": episode,
                        "source_origins": [item.get("source_origin") for item in episode_observations],
                    },
                )
            )
        if snapshot is not None:
            snapshot[snapshot_key] = documents
        return documents

    def episode_document(self, scope: MemoryScope, path: str) -> IndexedDocument | None:
        return next((document for document in self.episode_documents(scope) if document.path == path), None)

    def _documents(self, scope: MemoryScope) -> tuple[list[IndexedDocument], int]:
        documents = list(self.episode_documents(scope))
        valid_observation_ids = self.store.valid_observation_ids(scope)
        for observation in self.store.observations(scope).values():
            observation_id = str(observation.get("observation_id", ""))
            evidence_ref = f"observation:{observation_id}"
            if not observation_id or observation_id not in valid_observation_ids:
                continue
            documents.append(
                IndexedDocument(
                    doc_id=evidence_ref,
                    path=evidence_ref,
                    kind="observation",
                    content=str(observation.get("content", "")),
                    evidence_refs=[evidence_ref],
                    metadata={"observation": observation},
                )
            )
        trusted_records = 0
        for record in self.store.current_records(scope, include_inactive=False):
            evidence_refs = list(record.get("evidence_refs", []))
            if not evidence_refs or not all(self.store.validate_evidence(scope, ref) for ref in evidence_refs):
                continue
            trusted_records += 1
            documents.append(
                IndexedDocument(
                    doc_id=f"record:{record['record_id']}",
                    path=f"record:{record['record_id']}",
                    kind=record.get("memory_type", "record"),
                    content=record.get("content", ""),
                    evidence_refs=evidence_refs,
                    metadata={"record": record},
                )
            )
        return documents, trusted_records

    @staticmethod
    def _existing_embeddings(path: Path) -> dict[tuple[str, str], list[float]]:
        connection = sqlite3.connect(path)
        try:
            rows = connection.execute(
                "SELECT doc_id, content, embedding FROM documents WHERE embedding IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            connection.close()
        existing: dict[tuple[str, str], list[float]] = {}
        for doc_id, content, raw in rows:
            try:
                vector = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(vector, list):
                continue
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in vector
            ):
                continue
            existing[(str(doc_id), str(content))] = vector
        return existing

    @staticmethod
    def tokens(text: str) -> list[str]:
        result: list[str] = []
        for match in TOKEN_PATTERN.findall(text.lower()):
            if re.fullmatch(r"[\u4e00-\u9fff]+", match):
                if len(match) == 1:
                    result.append(match)
                else:
                    result.extend(match[index : index + 2] for index in range(len(match) - 1))
            else:
                result.append(match)
        return result

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        if any(not math.isfinite(float(value)) for value in [*left, *right]):
            return 0.0
        numerator = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
        right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return numerator / (left_norm * right_norm)
