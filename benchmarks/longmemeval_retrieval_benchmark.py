"""Retrieval-stage baseline for the public LongMemEval benchmark.

The dataset and evaluation conventions follow LongMemEval at the revision
recorded in ``longmemeval_manifest.json``. LongMemEval is MIT-licensed; see
``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
DEFAULT_K_VALUES = (1, 3, 5, 10)
LONGMEMEVAL_S_CLEANED_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
LONGMEMEVAL_S_CLEANED_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/"
    f"{LONGMEMEVAL_S_CLEANED_REVISION}/longmemeval_s_cleaned.json"
)


@dataclass(frozen=True)
class SessionDoc:
    session_id: str
    date: str
    text: str
    tokens: list[str]


def load_longmemeval(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("LongMemEval file must contain a JSON list")
    if limit is not None:
        return data[:limit]
    return data


def run_retrieval_benchmark(
    dataset_path: Path,
    k_values: Iterable[int] = DEFAULT_K_VALUES,
    limit: int | None = None,
    include_dates: bool = True,
    exclude_abstention: bool = True,
) -> dict[str, Any]:
    items = load_longmemeval(dataset_path, limit=limit)
    excluded_abstention_questions = 0
    if exclude_abstention:
        answerable_items = [item for item in items if "_abs" not in item["question_id"]]
        excluded_abstention_questions = len(items) - len(answerable_items)
        items = answerable_items
    normalized_k_values = sorted(set(k_values))
    rows: list[dict[str, Any]] = []

    for item in items:
        docs = _build_docs(item, include_dates=include_dates)
        ranked_ids = [doc.session_id for doc, _score in bm25_rank(item["question"], docs)]
        gold_ids = set(item.get("answer_session_ids", []))
        rows.append(_score_item(item, ranked_ids, gold_ids, normalized_k_values))

    return _summarize(
        rows,
        normalized_k_values,
        dataset_path,
        include_dates,
        excluded_abstention_questions,
        exclude_abstention,
    )


def bm25_rank(query: str, docs: list[SessionDoc]) -> list[tuple[SessionDoc, float]]:
    query_terms = tokenize(query)
    if not query_terms:
        return [(doc, 0.0) for doc in docs]

    doc_term_counts = [Counter(doc.tokens) for doc in docs]
    doc_lengths = [len(doc.tokens) for doc in docs]
    avg_doc_length = statistics.fmean(doc_lengths) if doc_lengths else 0.0
    document_frequency: Counter[str] = Counter()
    for term_counts in doc_term_counts:
        document_frequency.update(term_counts.keys())

    n_docs = len(docs)
    k1 = 1.5
    b = 0.75
    scored: list[tuple[SessionDoc, float]] = []
    for doc, term_counts, doc_length in zip(docs, doc_term_counts, doc_lengths):
        score = 0.0
        for term in query_terms:
            term_frequency = term_counts.get(term, 0)
            if not term_frequency:
                continue
            idf = math.log(1 + (n_docs - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = term_frequency + k1 * (1 - b + b * (doc_length / max(avg_doc_length, 1)))
            score += idf * ((term_frequency * (k1 + 1)) / denominator)
        scored.append((doc, score))
    return sorted(scored, key=lambda item: (-item[1], item[0].session_id))


def tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def format_markdown(result: dict[str, Any]) -> str:
    overall_rows = [
        "| Metric | Value |",
        "|--------|------:|",
        f"| questions | {result['overall']['questions']} |",
        f"| mean_session_count | {result['overall']['mean_session_count']:.2f} |",
        f"| mean_gold_session_count | {result['overall']['mean_gold_session_count']:.2f} |",
        f"| mrr | {result['overall']['mrr']:.4f} |",
        f"| median_first_gold_rank | {result['overall']['median_first_gold_rank']:.2f} |",
    ]
    for key, value in result["overall"]["recall_any"].items():
        overall_rows.append(f"| recall_any@{key} | {value:.4f} |")
    for key, value in result["overall"]["recall_all"].items():
        overall_rows.append(f"| recall_all@{key} | {value:.4f} |")
    for key, value in result["overall"]["ndcg_any"].items():
        overall_rows.append(f"| ndcg_any@{key} | {value:.4f} |")

    type_rows = [
        "| question_type | n | mrr | recall_any@1 | recall_any@5 | recall_any@10 |",
        "|---------------|--:|----:|-------------:|-------------:|--------------:|",
    ]
    for question_type, stats in sorted(result["by_question_type"].items()):
        recall_any = stats["recall_any"]
        type_rows.append(
            f"| {question_type} | {stats['questions']} | {stats['mrr']:.4f} | "
            f"{recall_any.get('1', 0.0):.4f} | {recall_any.get('5', 0.0):.4f} | {recall_any.get('10', 0.0):.4f} |"
        )

    parameters = json.dumps(result["parameters"], ensure_ascii=False, indent=2, sort_keys=True)
    dataset = json.dumps(result["dataset"], ensure_ascii=False, indent=2, sort_keys=True)
    caveats = "\n".join(f"- {item}" for item in result["caveats"])
    return (
        "# LongMemEval-S Cleaned Retrieval Benchmark Report\n\n"
        f"Generated at: `{result['generated_at']}`\n\n"
        "## Scope\n\n"
        "This is a retrieval-stage public memory benchmark on LongMemEval-S cleaned. "
        "It ranks haystack sessions with a stdlib BM25-style lexical scorer and scores strict hits against `answer_session_ids`.\n\n"
        "## Dataset\n\n"
        f"```json\n{dataset}\n```\n\n"
        "## Parameters\n\n"
        f"```json\n{parameters}\n```\n\n"
        "## Overall\n\n"
        + "\n".join(overall_rows)
        + "\n\n"
        "## By Question Type\n\n"
        + "\n".join(type_rows)
        + "\n\n"
        "## Caveats\n\n"
        f"{caveats}\n"
    )


def write_outputs(result: dict[str, Any], json_path: Path | None, markdown_path: Path | None) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if markdown_path is not None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(format_markdown(result), encoding="utf-8")


def _build_docs(item: dict[str, Any], include_dates: bool) -> list[SessionDoc]:
    docs: list[SessionDoc] = []
    for session_id, date, session in zip(
        item["haystack_session_ids"],
        item["haystack_dates"],
        item["haystack_sessions"],
    ):
        text = _session_text(session)
        indexed_text = f"{date}\n{text}" if include_dates else text
        docs.append(SessionDoc(session_id=session_id, date=date, text=indexed_text, tokens=tokenize(indexed_text)))
    return docs


def _session_text(session: Any) -> str:
    if isinstance(session, str):
        return session
    if not isinstance(session, list):
        return json.dumps(session, ensure_ascii=False, sort_keys=True)
    lines: list[str] = []
    for turn in session:
        if isinstance(turn, dict):
            role = turn.get("role", "unknown")
            content = turn.get("content", "")
            lines.append(f"{role}: {content}")
        else:
            lines.append(str(turn))
    return "\n".join(lines)


def _score_item(
    item: dict[str, Any],
    ranked_ids: list[str],
    gold_ids: set[str],
    k_values: list[int],
) -> dict[str, Any]:
    first_gold_rank = None
    for index, session_id in enumerate(ranked_ids, start=1):
        if session_id in gold_ids:
            first_gold_rank = index
            break
    recall_any = {}
    recall_all = {}
    ndcg_any = {}
    for k in k_values:
        top_k = set(ranked_ids[:k])
        recall_any[str(k)] = bool(gold_ids & top_k)
        recall_all[str(k)] = gold_ids.issubset(top_k)
        ndcg_any[str(k)] = _ndcg(ranked_ids, gold_ids, k)
    return {
        "question_id": item["question_id"],
        "question_type": item["question_type"],
        "session_count": len(ranked_ids),
        "gold_session_count": len(gold_ids),
        "first_gold_rank": first_gold_rank,
        "mrr": 0.0 if first_gold_rank is None else 1.0 / first_gold_rank,
        "recall_any": recall_any,
        "recall_all": recall_all,
        "ndcg_any": ndcg_any,
    }


def _summarize(
    rows: list[dict[str, Any]],
    k_values: list[int],
    dataset_path: Path,
    include_dates: bool,
    excluded_abstention_questions: int,
    exclude_abstention: bool,
) -> dict[str, Any]:
    return {
        "benchmark": "longmemeval_s_cleaned_retrieval",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(dataset_path),
            "questions": len(rows),
            "source": "LongMemEval-S cleaned public JSON",
            "source_url": LONGMEMEVAL_S_CLEANED_URL,
            "revision": LONGMEMEVAL_S_CLEANED_REVISION,
            "file_bytes": dataset_path.stat().st_size if dataset_path.exists() else None,
            "sha256": _sha256_file(dataset_path) if dataset_path.exists() else None,
            "excluded_abstention_questions": excluded_abstention_questions,
        },
        "parameters": {
            "k_values": k_values,
            "include_dates": include_dates,
            "scorer": "stdlib_bm25_lexical",
            "metric": "strict_answer_session_id_hit",
            "exclude_abstention": exclude_abstention,
        },
        "overall": _aggregate(rows, k_values),
        "by_question_type": {
            question_type: _aggregate(type_rows, k_values)
            for question_type, type_rows in _group_by(rows, "question_type").items()
        },
        "caveats": [
            "This is retrieval-stage only; it does not generate answers or run the official LLM judge.",
            "Scores are strict session-id hits against answer_session_ids.",
            "Retrieval metrics exclude question_id values ending in _abs, matching the official evaluator.",
            "The scorer is a simple lexical BM25-style baseline, not a dense retriever and not the Citefold MemoryPack compiler.",
            "Use this to validate public benchmark plumbing before end-to-end QA evaluation.",
        ],
    }


def _aggregate(rows: list[dict[str, Any]], k_values: list[int]) -> dict[str, Any]:
    first_gold_ranks = [row["first_gold_rank"] for row in rows if row["first_gold_rank"] is not None]
    return {
        "questions": len(rows),
        "mean_session_count": statistics.fmean(row["session_count"] for row in rows) if rows else 0.0,
        "mean_gold_session_count": statistics.fmean(row["gold_session_count"] for row in rows) if rows else 0.0,
        "mrr": statistics.fmean(row["mrr"] for row in rows) if rows else 0.0,
        "median_first_gold_rank": statistics.median(first_gold_ranks) if first_gold_ranks else 0.0,
        "recall_any": {
            str(k): statistics.fmean(1.0 if row["recall_any"][str(k)] else 0.0 for row in rows) if rows else 0.0
            for k in k_values
        },
        "recall_all": {
            str(k): statistics.fmean(1.0 if row["recall_all"][str(k)] else 0.0 for row in rows) if rows else 0.0
            for k in k_values
        },
        "ndcg_any": {
            str(k): statistics.fmean(row["ndcg_any"][str(k)] for row in rows) if rows else 0.0
            for k in k_values
        },
    }


def _ndcg(ranked_ids: list[str], gold_ids: set[str], k: int) -> float:
    relevances = [1.0 if session_id in gold_ids else 0.0 for session_id in ranked_ids[:k]]
    ideal = [1.0] * min(len(gold_ids), k)

    def dcg(values: list[float]) -> float:
        if not values:
            return 0.0
        return values[0] + sum(value / math.log2(index + 1) for index, value in enumerate(values[1:], start=1))

    ideal_dcg = dcg(ideal)
    return 0.0 if ideal_dcg == 0.0 else dcg(relevances) / ideal_dcg


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    return dict(grouped)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a LongMemEval-S cleaned retrieval-stage benchmark.")
    parser.add_argument("dataset", type=Path, help="Path to longmemeval_s_cleaned.json")
    parser.add_argument("--k", type=int, nargs="+", default=list(DEFAULT_K_VALUES))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-dates", action="store_true", help="Exclude session dates from indexed text.")
    parser.add_argument("--include-abstention", action="store_true", help="Include _abs questions in retrieval metrics.")
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_retrieval_benchmark(
        dataset_path=args.dataset,
        k_values=args.k,
        limit=args.limit,
        include_dates=not args.no_dates,
        exclude_abstention=not args.include_abstention,
    )
    write_outputs(result, args.output_json, args.output_md)
    print(format_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
