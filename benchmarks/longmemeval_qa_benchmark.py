"""Generate and judge LongMemEval-S QA hypotheses.

Reader and judge prompt text is adapted from LongMemEval at the revision
recorded in ``longmemeval_manifest.json``. LongMemEval is MIT-licensed; see
``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE: Path | None = None
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.longmemeval_retrieval_benchmark import (
    _build_docs,
    bm25_rank,
    load_longmemeval,
)
from benchmarks.longmemeval_citefold_adapter import (
    DEFAULT_MANIFEST_PATH,
    build_citefold_context,
    verify_dataset,
)


OFFICIAL_JUDGE_MODEL = "gpt-4o-2024-08-06"
CONTEXT_MODES = ("bm25-session", "citefold")


class FakeChatClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def chat(self, model: str, prompt: str, max_tokens: int) -> str:
        self.requests.append({"model": model, "prompt": prompt, "max_tokens": max_tokens})
        if not self.responses:
            raise RuntimeError("No fake response left")
        return self.responses.pop(0)


class OpenAICompatibleChatClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: int = 120,
        retries: int = 4,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def chat(self, model: str, prompt: str, max_tokens: int) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if self.base_url == "https://openrouter.ai/api/v1":
            payload["provider"] = {
                "zdr": True,
                "data_collection": "deny",
                "require_parameters": True,
            }
        body = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    data = json.loads(response.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
                json.JSONDecodeError,
                UnicodeDecodeError,
                TimeoutError,
            ) as error:
                last_error = error
                if attempt == self.retries:
                    break
                time.sleep(min(2**attempt, 30))
        raise RuntimeError(f"Chat completion failed after retries: {last_error!r}")


def load_env_file(path: Path | None = DEFAULT_ENV_FILE) -> None:
    if path is None or not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        if not name or name in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = value


def build_reader_prompt(item: dict[str, Any], ranked_session_ids: list[str] | None = None, top_k: int = 10) -> str:
    if ranked_session_ids is None:
        docs = _build_docs(item, include_dates=True)
        ranked_session_ids = [doc.session_id for doc, _score in bm25_rank(item["question"], docs)]

    selected_ids = set(ranked_session_ids[:top_k])
    selected_sessions: list[tuple[str, str, Any]] = []
    for session_id, date, session in zip(
        item["haystack_session_ids"],
        item["haystack_dates"],
        item["haystack_sessions"],
    ):
        if session_id in selected_ids:
            selected_sessions.append((date, session_id, _strip_answer_labels(session)))

    history = ""
    for index, (date, session_id, session) in enumerate(selected_sessions, start=1):
        history += (
            f"\n### Session {index}:\n"
            f"Session ID: {session_id}\n"
            f"Session Date: {date}\n"
            f"Session Content:\n{json.dumps(session, ensure_ascii=False)}\n"
        )

    return (
        "I will give you several history chats between you and a user.\n"
        "Please answer the question based on the relevant chat history. "
        "Answer the question step by step: first extract all the relevant information, "
        "and then reason over the information to get the answer.\n\n\n"
        f"History Chats:\n\n{history}\n\n"
        f"Current Date: {item['question_date']}\n"
        f"Question: {item['question']}\n"
        "Answer (step by step):"
    )


def build_memory_pack_reader_prompt(item: dict[str, Any], memory_context: str) -> str:
    return (
        "I will give you memory compiled from history chats between you and a user.\n"
        "Please answer the question based on the relevant memory. "
        "Answer the question step by step: first extract all the relevant information, "
        "and then reason over the information to get the answer.\n\n\n"
        f"Memory:\n\n{memory_context}\n\n"
        f"Current Date: {item['question_date']}\n"
        f"Question: {item['question']}\n"
        "Answer (step by step):"
    )


def generate_hypotheses(
    dataset_path: Path,
    output_path: Path,
    client: Any,
    model: str,
    top_k: int,
    limit: int | None,
    max_tokens: int = 800,
    context_mode: str = "bm25-session",
    memory_token_budget: int = 2200,
    trace_path: Path | None = None,
    manifest_path: Path | None = None,
    resume: bool = False,
    workers: int = 1,
    system_version: str | None = None,
) -> None:
    if context_mode not in CONTEXT_MODES:
        raise ValueError(f"Unsupported context_mode: {context_mode}")
    dataset_identity = verify_dataset(dataset_path, manifest_path) if manifest_path is not None else None
    if workers < 1:
        raise ValueError("workers must be at least 1")
    items = load_longmemeval(dataset_path, limit=limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_qids: set[str] = set()
    if resume and output_path.exists():
        existing_qids = {
            json.loads(line)["question_id"]
            for line in output_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    output_mode = "a" if resume else "w"
    trace_out = None
    if trace_path is not None:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_out = trace_path.open(output_mode, encoding="utf-8")

    def generate_one(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        trace: dict[str, Any] | None = None
        if context_mode == "bm25-session":
            prompt = build_reader_prompt(item, top_k=top_k)
        else:
            with tempfile.TemporaryDirectory(prefix="longmemeval-citefold-") as tmp:
                context_result = build_citefold_context(
                    item=item,
                    root=Path(tmp),
                    token_budget=memory_token_budget,
                )
            prompt = build_memory_pack_reader_prompt(item, context_result.context)
            trace = context_result.trace

        hypothesis = client.chat(model=model, prompt=prompt, max_tokens=max_tokens)
        generation = {
            "model": model,
            "context_mode": context_mode,
            "top_k": top_k if context_mode == "bm25-session" else None,
            "memory_token_budget": memory_token_budget if context_mode == "citefold" else None,
            "max_tokens": max_tokens,
            "system_version": system_version,
        }
        if dataset_identity is not None:
            generation["dataset_sha256"] = dataset_identity["sha256"]
            generation["dataset_revision"] = dataset_identity["revision"]
        return {
            "question_id": item["question_id"],
            "hypothesis": hypothesis,
            "generation": generation,
        }, trace

    pending_items = [item for item in items if item["question_id"] not in existing_qids]
    executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        with output_path.open(output_mode, encoding="utf-8") as out:
            generated = executor.map(generate_one, pending_items) if executor is not None else map(generate_one, pending_items)
            for row, trace in generated:
                print(json.dumps(row, ensure_ascii=False), file=out, flush=True)
                if trace_out is not None and trace is not None:
                    print(json.dumps(trace, ensure_ascii=False), file=trace_out, flush=True)
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
        if trace_out is not None:
            trace_out.close()


def evaluate_hypotheses(
    dataset_path: Path,
    hypothesis_path: Path,
    output_path: Path,
    client: Any,
    judge_model: str,
    manifest_path: Path | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    references = load_longmemeval(dataset_path)
    dataset_identity = verify_dataset(dataset_path, manifest_path) if manifest_path is not None else None
    qid_to_item = {item["question_id"]: item for item in references}
    hypotheses = [json.loads(line) for line in hypothesis_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    hypothesis_ids = [item["question_id"] for item in hypotheses]
    duplicate_ids = sorted(qid for qid, count in Counter(hypothesis_ids).items() if count > 1)
    if duplicate_ids:
        raise ValueError(f"Duplicate hypothesis question_id values: {duplicate_ids[:5]}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    labels: list[bool] = []
    answerable_labels: list[bool] = []
    abstention_labels: list[bool] = []
    type_to_labels: dict[str, list[bool]] = defaultdict(list)

    def evaluate_one(hypothesis: dict[str, Any]) -> tuple[dict[str, Any], bool, str, bool] | None:
        if hypothesis["question_id"] not in qid_to_item:
            return None
        item = qid_to_item[hypothesis["question_id"]]
        prompt = build_judge_prompt(
            question_type=item["question_type"],
            question=item["question"],
            answer=item["answer"],
            response=hypothesis["hypothesis"],
            abstention="_abs" in item["question_id"],
        )
        judge_response = client.chat(model=judge_model, prompt=prompt, max_tokens=10)
        label = "yes" in judge_response.lower()
        log = dict(hypothesis)
        log["autoeval_label"] = {"model": judge_model, "label": label, "raw_response": judge_response}
        return log, label, item["question_type"], "_abs" in item["question_id"]

    executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    with output_path.open("w", encoding="utf-8") as out:
        evaluated = executor.map(evaluate_one, hypotheses) if executor is not None else map(evaluate_one, hypotheses)
        for result_row in evaluated:
            if result_row is None:
                continue
            log, label, question_type, abstention = result_row
            print(json.dumps(log, ensure_ascii=False), file=out, flush=True)
            labels.append(label)
            type_to_labels[question_type].append(label)
            (abstention_labels if abstention else answerable_labels).append(label)
    if executor is not None:
        executor.shutdown(wait=True)

    return _build_qa_result(
        dataset_path=dataset_path,
        source_path=hypothesis_path,
        references=references,
        records=hypotheses,
        labels=labels,
        answerable_labels=answerable_labels,
        abstention_labels=abstention_labels,
        type_to_labels=type_to_labels,
        judge_model=judge_model,
        dataset_identity=dataset_identity,
    )


def summarize_evaluation_logs(
    dataset_path: Path,
    evaluation_path: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    references = load_longmemeval(dataset_path)
    qid_to_item = {item["question_id"]: item for item in references}
    records = [json.loads(line) for line in evaluation_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    judge_models = {record["autoeval_label"]["model"] for record in records}
    if len(judge_models) != 1:
        raise ValueError(f"Expected one judge model in evaluation log, got {sorted(judge_models)}")
    labels: list[bool] = []
    answerable_labels: list[bool] = []
    abstention_labels: list[bool] = []
    type_to_labels: dict[str, list[bool]] = defaultdict(list)
    for record in records:
        item = qid_to_item.get(record["question_id"])
        if item is None:
            continue
        label = bool(record["autoeval_label"]["label"])
        labels.append(label)
        type_to_labels[item["question_type"]].append(label)
        (abstention_labels if "_abs" in item["question_id"] else answerable_labels).append(label)
    dataset_identity = verify_dataset(dataset_path, manifest_path) if manifest_path is not None else None
    return _build_qa_result(
        dataset_path=dataset_path,
        source_path=evaluation_path,
        references=references,
        records=records,
        labels=labels,
        answerable_labels=answerable_labels,
        abstention_labels=abstention_labels,
        type_to_labels=type_to_labels,
        judge_model=next(iter(judge_models)),
        dataset_identity=dataset_identity,
    )


def _build_qa_result(
    dataset_path: Path,
    source_path: Path,
    references: list[dict[str, Any]],
    records: list[dict[str, Any]],
    labels: list[bool],
    answerable_labels: list[bool],
    abstention_labels: list[bool],
    type_to_labels: dict[str, list[bool]],
    judge_model: str,
    dataset_identity: dict[str, Any] | None,
) -> dict[str, Any]:
    reference_ids = {item["question_id"] for item in references}
    record_ids = [record["question_id"] for record in records]
    duplicate_ids = sorted(qid for qid, count in Counter(record_ids).items() if count > 1)
    if duplicate_ids:
        raise ValueError(f"Duplicate evaluation question_id values: {duplicate_ids[:5]}")
    unknown_ids = sorted(set(record_ids) - reference_ids)
    missing_ids = sorted(reference_ids - set(record_ids))
    dataset = {"path": dataset_path.name, "questions": len(references)}
    if dataset_identity is not None:
        dataset.update(dataset_identity)
    generation = records[0].get("generation", {}) if records else {}
    generation_consistent = all(record.get("generation", {}) == generation for record in records)
    return {
        "benchmark": "longmemeval_s_cleaned_qa",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": dataset,
        "hypotheses": {"path": source_path.name, "count": len(records)},
        "coverage": {
            "expected": len(references),
            "evaluated": len(labels),
            "complete": not missing_ids and not unknown_ids,
            "missing_question_ids": missing_ids,
            "unknown_question_ids": unknown_ids,
        },
        "generation": generation,
        "generation_config_consistent": generation_consistent,
        "judge_model": judge_model,
        "judge_prompt": "official_longmemeval_evaluate_qa",
        "official_judge_model": OFFICIAL_JUDGE_MODEL,
        "official_judge_compatible": judge_model == OFFICIAL_JUDGE_MODEL,
        "overall_accuracy": _mean_bool(labels),
        "answerable_accuracy": _mean_bool(answerable_labels),
        "answerable_count": len(answerable_labels),
        "abstention_accuracy": _mean_bool(abstention_labels),
        "abstention_count": len(abstention_labels),
        "by_question_type": {
            question_type: {"count": len(values), "accuracy": _mean_bool(values)}
            for question_type, values in sorted(type_to_labels.items())
        },
    }


def build_judge_prompt(
    question_type: str,
    question: str,
    answer: str,
    response: str,
    abstention: bool = False,
) -> str:
    if abstention:
        return (
            "I will give you an unanswerable question, an explanation, and a response from a model. "
            "Please answer yes if the model correctly identifies the question as unanswerable. "
            "The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\n"
            f"Question: {question}\n\nExplanation: {answer}\n\nModel Response: {response}\n\n"
            "Does the model correctly identify the question as unanswerable? Answer yes or no only."
        )
    if question_type == "temporal-reasoning":
        return (
            "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. "
            "Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, "
            "you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. "
            "In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., "
            "and the model makes off-by-one errors, the model's response is still correct.\n\n"
            f"Question: {question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    if question_type == "knowledge-update":
        return (
            "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. "
            "Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered "
            "as correct as long as the updated answer is the required answer.\n\n"
            f"Question: {question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    if question_type == "single-session-preference":
        return (
            "I will give you a question, a rubric for desired personalized response, and a response from a model. "
            "Please answer yes if the response satisfies the desired response. Otherwise, answer no. "
            "The model does not need to reflect all the points in the rubric. "
            "The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\n"
            f"Question: {question}\n\nRubric: {answer}\n\nModel Response: {response}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    return (
        "I will give you a question, a correct answer, and a response from a model.\n"
        "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
        "If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. "
        "If the response only contains a subset of the information required by the answer, answer no.\n\n"
        f"Question: {question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
        "Is the model response correct? Answer yes or no only."
    )


def format_qa_markdown(result: dict[str, Any]) -> str:
    rows = [
        "| question_type | n | accuracy |",
        "|---------------|--:|---------:|",
    ]
    for question_type, stats in result["by_question_type"].items():
        rows.append(f"| {question_type} | {stats['count']} | {stats['accuracy']:.4f} |")
    coverage = result["coverage"]
    coverage_label = "complete" if coverage["complete"] else "partial"
    official_label = "yes" if result["official_judge_compatible"] else "no"
    generation = result.get("generation", {})
    dataset = result.get("dataset", {})
    return (
        "# LongMemEval-S Cleaned QA Benchmark Report\n\n"
        f"Generated at: `{result['generated_at']}`\n\n"
        "## Scope\n\n"
        "This report is an end-to-end QA evaluation using generated hypotheses and an LLM judge.\n\n"
        "## Overall\n\n"
        f"- Accuracy: {result['overall_accuracy']:.4f}\n"
        f"- Answerable accuracy: {result['answerable_accuracy']:.4f} ({result['answerable_count']})\n"
        f"- Abstention accuracy: {result['abstention_accuracy']:.4f} ({result['abstention_count']})\n"
        f"- Hypotheses: {result['hypotheses']['count']}\n"
        f"- Coverage: {coverage['evaluated']}/{coverage['expected']} ({coverage_label})\n"
        f"- Context mode: `{generation.get('context_mode', 'unknown')}`\n"
        f"- Reader model: `{generation.get('model', 'unknown')}`\n"
        f"- Judge model: `{result['judge_model']}`\n\n"
        f"- Official judge compatible: {official_label}\n"
        f"- Dataset SHA-256: `{dataset.get('sha256', 'not-recorded')}`\n\n"
        "## By Question Type\n\n"
        + "\n".join(rows)
        + "\n"
    )


def _strip_answer_labels(session: Any) -> Any:
    if not isinstance(session, list):
        return session
    cleaned = []
    for turn in session:
        if isinstance(turn, dict):
            copied = dict(turn)
            copied.pop("has_answer", None)
            cleaned.append(copied)
        else:
            cleaned.append(turn)
    return cleaned


def _mean_bool(values: list[bool]) -> float:
    if not values:
        return 0.0
    return statistics.fmean(1.0 if value else 0.0 for value in values)


def _client_from_env(
    api_key_env: str,
    base_url: str,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> OpenAICompatibleChatClient:
    load_env_file(env_file)
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"{api_key_env} is not set; cannot call the LLM API")
    return OpenAICompatibleChatClient(api_key=api_key, base_url=base_url)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and judge LongMemEval-S QA hypotheses.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("dataset", type=Path)
    generate.add_argument("output", type=Path)
    generate.add_argument("--model", default="gpt-4o-mini-2024-07-18")
    generate.add_argument("--top-k", type=int, default=10)
    generate.add_argument("--limit", type=int, default=None)
    generate.add_argument("--max-tokens", type=int, default=800)
    generate.add_argument("--context-mode", choices=CONTEXT_MODES, default="bm25-session")
    generate.add_argument("--memory-token-budget", type=int, default=2200)
    generate.add_argument("--trace-output", type=Path, default=None)
    generate.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    generate.add_argument("--resume", action="store_true")
    generate.add_argument("--workers", type=int, default=1)
    generate.add_argument("--system-version", default=None)
    generate.add_argument("--api-key-env", default="OPENAI_API_KEY")
    generate.add_argument("--base-url", default="https://api.openai.com/v1")
    generate.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)

    judge = subparsers.add_parser("judge")
    judge.add_argument("dataset", type=Path)
    judge.add_argument("hypotheses", type=Path)
    judge.add_argument("output", type=Path)
    judge.add_argument("--judge-model", default="gpt-4o-2024-08-06")
    judge.add_argument("--summary-json", type=Path, default=None)
    judge.add_argument("--summary-md", type=Path, default=None)
    judge.add_argument("--api-key-env", default="OPENAI_API_KEY")
    judge.add_argument("--base-url", default="https://api.openai.com/v1")
    judge.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    judge.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    judge.add_argument("--workers", type=int, default=1)

    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("dataset", type=Path)
    summarize.add_argument("evaluation", type=Path)
    summarize.add_argument("--summary-json", type=Path, default=None)
    summarize.add_argument("--summary-md", type=Path, default=None)
    summarize.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "summarize":
        result = summarize_evaluation_logs(
            dataset_path=args.dataset,
            evaluation_path=args.evaluation,
            manifest_path=args.manifest,
        )
        _write_summary_outputs(result, args.summary_json, args.summary_md)
        print(format_qa_markdown(result))
        return 0

    client = _client_from_env(args.api_key_env, args.base_url, env_file=args.env_file)
    if args.command == "generate":
        generate_hypotheses(
            dataset_path=args.dataset,
            output_path=args.output,
            client=client,
            model=args.model,
            top_k=args.top_k,
            limit=args.limit,
            max_tokens=args.max_tokens,
            context_mode=args.context_mode,
            memory_token_budget=args.memory_token_budget,
            trace_path=args.trace_output,
            manifest_path=args.manifest,
            resume=args.resume,
            workers=args.workers,
            system_version=args.system_version,
        )
        return 0

    result = evaluate_hypotheses(
        dataset_path=args.dataset,
        hypothesis_path=args.hypotheses,
        output_path=args.output,
        client=client,
        judge_model=args.judge_model,
        manifest_path=args.manifest,
        workers=args.workers,
    )
    _write_summary_outputs(result, args.summary_json, args.summary_md)
    print(format_qa_markdown(result))
    return 0


def _write_summary_outputs(result: dict[str, Any], json_path: Path | None, markdown_path: Path | None) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if markdown_path is not None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(format_qa_markdown(result), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
