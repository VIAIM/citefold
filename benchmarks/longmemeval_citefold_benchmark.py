"""Run Citefold's retrieval diagnostic on LongMemEval-S.

The evaluation shape follows LongMemEval at the revision recorded in
``longmemeval_manifest.json``. LongMemEval is MIT-licensed; see
``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from citefold import __version__


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.longmemeval_citefold_adapter import (
    DEFAULT_MANIFEST_PATH,
    build_citefold_context,
    verify_dataset,
)
from benchmarks.longmemeval_retrieval_benchmark import (
    DEFAULT_K_VALUES,
    _aggregate,
    _group_by,
    _score_item,
    load_longmemeval,
)


def _evaluate_item(arguments: tuple[dict[str, Any], list[int], int]) -> dict[str, Any]:
    item, k_values, token_budget = arguments
    with tempfile.TemporaryDirectory(prefix="longmemeval-citefold-retrieval-") as tmp:
        context_result = build_citefold_context(
            item=item,
            root=Path(tmp),
            token_budget=token_budget,
        )
    ranked_ids = context_result.trace["selected_session_ids"]
    row = _score_item(
        item=item,
        ranked_ids=ranked_ids,
        gold_ids=set(item.get("answer_session_ids", [])),
        k_values=k_values,
    )
    row["trace"] = context_result.trace
    return row


def run_benchmark(
    dataset_path: Path,
    k_values: Iterable[int] = DEFAULT_K_VALUES,
    token_budget: int = 2200,
    limit: int | None = None,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    verify_manifest: bool = True,
    workers: int = 1,
) -> dict[str, Any]:
    dataset_identity = verify_dataset(dataset_path, manifest_path) if verify_manifest else {
        "path": dataset_path.name,
        "questions": len(load_longmemeval(dataset_path)),
    }
    all_items = load_longmemeval(dataset_path, limit=limit)
    items = [item for item in all_items if "_abs" not in item["question_id"]]
    excluded_abstention_questions = len(all_items) - len(items)
    normalized_k_values = sorted(set(k_values))
    work = [(item, normalized_k_values, token_budget) for item in items]
    if workers <= 1:
        rows = [_evaluate_item(arguments) for arguments in work]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            rows = list(executor.map(_evaluate_item, work, chunksize=1))

    return {
        "benchmark": "longmemeval_s_cleaned_citefold_retrieval",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system": "citefold",
        "system_version": __version__,
        "dataset": {
            **dataset_identity,
            "evaluated_questions": len(items),
            "excluded_abstention_questions": excluded_abstention_questions,
        },
        "parameters": {
            "k_values": normalized_k_values,
            "token_budget": token_budget,
            "metric": "official_session_retrieval_shape",
            "workers": workers,
        },
        "overall": _aggregate(rows, normalized_k_values),
        "by_question_type": {
            question_type: _aggregate(type_rows, normalized_k_values)
            for question_type, type_rows in _group_by(rows, "question_type").items()
        },
        "rows": rows,
        "caveats": [
            "This is a public-dataset retrieval diagnostic, not the end-to-end LongMemEval QA score.",
            "Questions ending in _abs are excluded, matching the official retrieval evaluator.",
            "Only session ids represented by nodes returned in the MemoryPack are scored as retrieved.",
        ],
    }


def format_markdown(result: dict[str, Any]) -> str:
    overall = result["overall"]
    k_values = result["parameters"]["k_values"]
    rows = [
        "| metric | " + " | ".join(f"@{k}" for k in k_values) + " |",
        "|--------|" + "|".join("------:" for _ in k_values) + "|",
        "| recall_any | " + " | ".join(f"{overall['recall_any'][str(k)]:.4f}" for k in k_values) + " |",
        "| recall_all | " + " | ".join(f"{overall['recall_all'][str(k)]:.4f}" for k in k_values) + " |",
        "| ndcg_any | " + " | ".join(f"{overall['ndcg_any'][str(k)]:.4f}" for k in k_values) + " |",
    ]
    caveats = "\n".join(f"- {caveat}" for caveat in result["caveats"])
    return (
        "# LongMemEval-S Citefold Public Retrieval Diagnostic\n\n"
        f"Generated at: `{result['generated_at']}`\n\n"
        "This uses the public LongMemEval-S cleaned dataset. It is not the end-to-end QA score.\n\n"
        "## Dataset\n\n"
        f"- SHA-256: `{result['dataset'].get('sha256', 'not-verified')}`\n"
        f"- Evaluated questions: {result['dataset']['evaluated_questions']}\n"
        f"- Excluded abstention questions: {result['dataset']['excluded_abstention_questions']}\n\n"
        "## Overall\n\n"
        + "\n".join(rows)
        + f"\n\n- MRR: {overall['mrr']:.4f}\n"
        + "\n## Caveats\n\n"
        + caveats
        + "\n"
    )


def write_outputs(result: dict[str, Any], json_path: Path | None, markdown_path: Path | None) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if markdown_path is not None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(format_markdown(result), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Citefold on the LongMemEval-S retrieval diagnostic.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--k", type=int, nargs="+", default=list(DEFAULT_K_VALUES))
    parser.add_argument("--token-budget", type=int, default=2200)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_benchmark(
        dataset_path=args.dataset,
        k_values=args.k,
        token_budget=args.token_budget,
        limit=args.limit,
        manifest_path=args.manifest,
        workers=max(1, args.workers),
    )
    write_outputs(result, args.output_json, args.output_md)
    print(format_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
