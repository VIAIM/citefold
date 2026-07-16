from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from citefold import MemoryScope, Citefold


@dataclass(frozen=True)
class BenchmarkConfig:
    root: Path | None = None
    tenants: int = 3
    users_per_tenant: int = 5
    events_per_user: int = 8
    candidates_per_user: int = 2
    runs: int = 1
    isolation_samples: int = 12


class StepClock:
    def __init__(self, start: datetime | None = None, step: timedelta | None = None) -> None:
        self.current = start or datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
        self.step = step or timedelta(seconds=1)

    def __call__(self) -> datetime:
        value = self.current
        self.current = self.current + self.step
        return value


def run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    if config.root is None:
        with tempfile.TemporaryDirectory() as tmp:
            return _run_benchmark(replace(config, root=Path(tmp) / "memory-root"))
    return _run_benchmark(config)


def _run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    _validate_config(config)
    if config.root is None:
        raise ValueError("root must be set before running the benchmark")
    all_timings: dict[str, list[float]] = {}
    all_missing_markers: list[dict[str, str]] = []
    all_isolation_violations: list[dict[str, str]] = []
    run_totals: list[dict[str, Any]] = []
    run_footprints: list[dict[str, Any]] = []

    for run_index in range(config.runs):
        run_root = config.root / f"run-{run_index + 1}"
        clock = StepClock()
        memory = Citefold(run_root, clock=clock)
        timings: dict[str, list[float]] = {}
        expected_markers: dict[str, list[str]] = {}
        scopes: list[MemoryScope] = []

        def timed(name: str, operation: Callable[[], Any]) -> Any:
            started = time.perf_counter_ns()
            result = operation()
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            timings.setdefault(name, []).append(elapsed_ms)
            return result

        for tenant_index in range(config.tenants):
            for user_index in range(config.users_per_tenant):
                scope = _scope(run_index, tenant_index, user_index)
                scopes.append(scope)
                scope_key = _scope_key(scope)
                expected_markers[scope_key] = []

                for event_index in range(config.events_per_user):
                    pref_marker = f"pref-r{run_index}-t{tenant_index}-u{user_index}-e{event_index}"
                    task_marker = f"task-r{run_index}-t{tenant_index}-u{user_index}-e{event_index}"
                    waiting_marker = f"waiting-r{run_index}-t{tenant_index}-u{user_index}-e{event_index}"
                    if event_index == 0:
                        expected_markers[scope_key].extend([pref_marker, task_marker, waiting_marker])

                    timed(
                        "ingest_chat",
                        lambda scope=scope, pref_marker=pref_marker, task_marker=task_marker, waiting_marker=waiting_marker: memory.ingest_chat(
                            scope=scope,
                            messages=[
                                {
                                    "role": "user",
                                    "content": f"请记住：{pref_marker} 我喜欢先结论后细节。",
                                },
                                {
                                    "role": "user",
                                    "content": f"提醒我明天上午10点处理{task_marker}，等同事回复{waiting_marker}后再跟进。",
                                },
                            ],
                            source="text_chat",
                        ),
                    )

                timed(
                    "voice_partial",
                    lambda scope=scope: memory.ingest_text(
                        scope=scope,
                        text="实时语音片段：这只是临时讨论，不应进入长期偏好。",
                        source="voice_transcript",
                        mode="voice",
                        final=False,
                    ),
                )
                timed(
                    "voice_final",
                    lambda scope=scope: memory.ingest_text(
                        scope=scope,
                        text="语音会话结束：会议结论是周五前完成客户方案。提醒我周五上午9点检查客户方案。",
                        source="voice_transcript",
                        mode="voice",
                        final=True,
                    ),
                )

                for candidate_index in range(config.candidates_per_user):
                    person_marker = f"Alex-r{run_index}-t{tenant_index}-u{user_index}-c{candidate_index}"
                    if candidate_index == 0:
                        expected_markers[scope_key].append(person_marker)
                    evidence = timed(
                        "third_party_append_event",
                        lambda scope=scope, person_marker=person_marker: memory.append_event(
                            scope=scope,
                            source="crm_agent",
                            payload={"text": f"CRM Agent 发现：{person_marker} 偏好周五下午跟进。"},
                        ),
                    )
                    candidate = timed(
                        "submit_candidate",
                        lambda scope=scope, person_marker=person_marker, evidence=evidence: memory.submit_candidate(
                            scope=scope,
                            source_agent="crm_agent",
                            memory_type="people",
                            content=f"{person_marker} 偏好周五下午跟进。",
                            evidence_refs=[evidence.evidence_ref],
                            confidence=0.82,
                        ),
                    )
                    timed(
                        "approve_candidate",
                        lambda scope=scope, candidate=candidate: memory.approve_candidate(scope, candidate.candidate_id),
                    )

        sampled_scopes = scopes[: min(config.isolation_samples, len(scopes))]
        for scope in sampled_scopes:
            scope_key = _scope_key(scope)
            pack = timed(
                "recall_text",
                lambda scope=scope: memory.recall(scope, "我有什么偏好？我现在有什么待办？客户怎么跟进？"),
            )
            for marker in expected_markers[scope_key]:
                if marker not in pack.markdown:
                    all_missing_markers.append({"scope": scope_key, "marker": marker})

            timed(
                "recall_voice",
                lambda scope=scope: memory.recall(scope, "客户方案有什么安排？", mode="voice"),
            )

        for scope in sampled_scopes:
            scope_key = _scope_key(scope)
            pack = memory.recall(scope, "我有什么偏好？我现在有什么待办？客户怎么跟进？")
            other_markers = [
                marker
                for other_scope_key, markers in expected_markers.items()
                if other_scope_key != scope_key
                for marker in markers
            ]
            for marker in other_markers:
                if marker in pack.markdown:
                    all_isolation_violations.append({"scope": scope_key, "leaked_marker": marker})
                    break

        for name, values in timings.items():
            all_timings.setdefault(name, []).extend(values)

        run_totals.append(
            {
                "scopes": len(scopes),
                "ingest_chat_calls": config.tenants * config.users_per_tenant * config.events_per_user,
                "chat_messages": config.tenants * config.users_per_tenant * config.events_per_user * 2,
                "voice_partial_calls": len(scopes),
                "voice_final_calls": len(scopes),
                "candidate_cycles": config.tenants * config.users_per_tenant * config.candidates_per_user,
                "recall_text_calls": len(sampled_scopes),
                "recall_voice_calls": len(sampled_scopes),
                "isolation_checks": len(sampled_scopes),
            }
        )
        run_footprints.append(_footprint(run_root))

    totals = _sum_totals(run_totals)
    footprint = _summarize_footprints(run_footprints)
    return {
        "benchmark": "citefold_phase1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "tenants": config.tenants,
            "users_per_tenant": config.users_per_tenant,
            "events_per_user": config.events_per_user,
            "candidates_per_user": config.candidates_per_user,
            "runs": config.runs,
            "isolation_samples": config.isolation_samples,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
        },
        "totals": totals,
        "timings_ms": {name: _latency_summary(values) for name, values in sorted(all_timings.items())},
        "footprint": footprint,
        "quality": {
            "missing_expected_markers": all_missing_markers,
            "missing_expected_marker_count": len(all_missing_markers),
            "isolation_violations": all_isolation_violations,
            "isolation_violation_count": len(all_isolation_violations),
        },
        "caveats": [
            "This is a local engineering benchmark for the Phase 1 file-first prototype.",
            "It measures exact-marker recall, latency, isolation, and file footprint; it is not a semantic QA benchmark.",
            "Do not compare these numbers with LoCoMo, LongMemEval, or vendor leaderboards.",
        ],
    }


def format_markdown(result: dict[str, Any]) -> str:
    timing_rows = [
        "| Operation | n | mean ms | median ms | p95 ms | max ms |",
        "|-----------|---:|--------:|----------:|-------:|-------:|",
    ]
    for name, stats in result["timings_ms"].items():
        timing_rows.append(
            f"| {name} | {stats['count']} | {stats['mean']:.3f} | {stats['median']:.3f} | "
            f"{stats['p95']:.3f} | {stats['max']:.3f} |"
        )

    total_rows = [
        "| Metric | Value |",
        "|--------|------:|",
        *[f"| {key} | {value} |" for key, value in result["totals"].items()],
    ]
    footprint = result["footprint"]
    footprint_rows = [
        "| Metric | Value |",
        "|--------|------:|",
        f"| files_avg | {footprint['files_avg']:.1f} |",
        f"| bytes_avg | {footprint['bytes_avg']:.1f} |",
        f"| jsonl_lines_avg | {footprint['jsonl_lines_avg']:.1f} |",
    ]
    caveats = "\n".join(f"- {item}" for item in result["caveats"])
    parameters = json.dumps(result["parameters"], ensure_ascii=False, indent=2, sort_keys=True)
    environment = json.dumps(result["environment"], ensure_ascii=False, indent=2, sort_keys=True)

    return (
        "# Citefold Phase 1 Benchmark Report\n\n"
        f"Generated at: `{result['generated_at']}`\n\n"
        "## Scope\n\n"
        "This report benchmarks the local file-first `Citefold` Phase 1 prototype. "
        "It uses synthetic office/life memory events with explicit tenant, user, namespace, agent, and session scopes.\n\n"
        "## Parameters\n\n"
        f"```json\n{parameters}\n```\n\n"
        "## Environment\n\n"
        f"```json\n{environment}\n```\n\n"
        "## Totals\n\n"
        + "\n".join(total_rows)
        + "\n\n"
        "## Latency\n\n"
        + "\n".join(timing_rows)
        + "\n\n"
        "## File Footprint\n\n"
        + "\n".join(footprint_rows)
        + "\n\n"
        "## Quality Checks\n\n"
        f"- Missing expected markers: {result['quality']['missing_expected_marker_count']}\n"
        f"- Scope isolation violations: {result['quality']['isolation_violation_count']}\n\n"
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


def _scope(run_index: int, tenant_index: int, user_index: int) -> MemoryScope:
    return MemoryScope(
        tenant_id=f"tenant-{tenant_index}",
        user_id=f"user-{user_index}",
        namespace="personal",
        agent_id="benchmark-agent",
        session_id=f"run-{run_index}-session-{tenant_index}-{user_index}",
    )


def _scope_key(scope: MemoryScope) -> str:
    return f"{scope.tenant_id}/{scope.user_id}/{scope.namespace}"


def _validate_config(config: BenchmarkConfig) -> None:
    for field_name in ("tenants", "users_per_tenant", "events_per_user", "candidates_per_user", "runs"):
        value = getattr(config, field_name)
        if value < 1:
            raise ValueError(f"{field_name} must be >= 1")
    if config.isolation_samples < 0:
        raise ValueError("isolation_samples must be >= 0")


def _latency_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p95": _percentile(values, 95),
        "max": max(values),
        "min": min(values),
    }


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = round((len(ordered) - 1) * (percentile / 100))
    return ordered[index]


def _footprint(root: Path) -> dict[str, int]:
    files = [path for path in root.rglob("*") if path.is_file()]
    jsonl_lines = 0
    for path in files:
        if path.suffix == ".jsonl":
            jsonl_lines += len(path.read_text(encoding="utf-8").splitlines())
    return {
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "jsonl_lines": jsonl_lines,
    }


def _summarize_footprints(footprints: list[dict[str, int]]) -> dict[str, float]:
    return {
        "files_avg": statistics.fmean(item["files"] for item in footprints),
        "bytes_avg": statistics.fmean(item["bytes"] for item in footprints),
        "jsonl_lines_avg": statistics.fmean(item["jsonl_lines"] for item in footprints),
    }


def _sum_totals(totals: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in totals:
        for key, value in item.items():
            result[key] = result.get(key, 0) + value
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Citefold Phase 1 benchmark.")
    parser.add_argument("--tenants", type=int, default=3)
    parser.add_argument("--users-per-tenant", type=int, default=5)
    parser.add_argument("--events-per-user", type=int, default=8)
    parser.add_argument("--candidates-per-user", type=int, default=2)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--isolation-samples", type=int, default=12)
    parser.add_argument("--root", type=Path, default=None, help="Optional benchmark memory root to keep on disk.")
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = BenchmarkConfig(
        root=args.root,
        tenants=args.tenants,
        users_per_tenant=args.users_per_tenant,
        events_per_user=args.events_per_user,
        candidates_per_user=args.candidates_per_user,
        runs=args.runs,
        isolation_samples=args.isolation_samples,
    )
    result = run_benchmark(config)
    write_outputs(result, args.output_json, args.output_md)
    print(format_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
