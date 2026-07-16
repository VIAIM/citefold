from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from citefold import MemoryScope, Citefold


@dataclass(frozen=True)
class BenchmarkConfig:
    root: Path | None = None
    tenants: int = 2
    users_per_tenant: int = 2


@dataclass(frozen=True)
class Probe:
    probe_id: str
    category: str
    query: str
    expected_markers: list[str]
    forbidden_markers: list[str]
    requires_memory: bool
    mode: str = "text"


@dataclass(frozen=True)
class Scenario:
    scope: MemoryScope
    markers: dict[str, str]
    probes: list[Probe]


class StepClock:
    def __init__(self, start: datetime | None = None, step: timedelta | None = None) -> None:
        self.current = start or datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
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

    memory = Citefold(config.root, clock=StepClock())
    scenarios = _build_scenarios(config)
    for scenario in scenarios:
        _seed_scenario(memory, scenario)

    markers_by_scope = {_scope_key(scenario.scope): list(scenario.markers.values()) for scenario in scenarios}
    logs: list[dict[str, Any]] = []
    baseline_scores: dict[str, list[dict[str, Any]]] = {"no_memory": [], "memory_pack": []}

    for scenario in scenarios:
        scope_key = _scope_key(scenario.scope)
        foreign_markers = [
            marker
            for other_scope, markers in markers_by_scope.items()
            if other_scope != scope_key
            for marker in markers
        ]
        for probe in scenario.probes:
            probe_for_eval = replace(
                probe,
                forbidden_markers=probe.forbidden_markers + foreign_markers,
            )
            no_memory_score = score_context(probe.query, probe_for_eval, selected_node_count=0)
            pack = memory.recall(scenario.scope, probe.query, mode=probe.mode)
            memory_score = score_context(
                pack.markdown,
                probe_for_eval,
                selected_node_count=len(pack.selected_nodes),
            )
            baseline_scores["no_memory"].append(no_memory_score)
            baseline_scores["memory_pack"].append(memory_score)
            logs.append(
                {
                    "scope": scope_key,
                    "probe_id": probe.probe_id,
                    "category": probe.category,
                    "query": probe.query,
                    "requires_memory": probe.requires_memory,
                    "expected_markers": probe.expected_markers,
                    "forbidden_marker_count": len(probe_for_eval.forbidden_markers),
                    "no_memory": no_memory_score,
                    "memory_pack": memory_score,
                }
            )

    baselines = {name: _summarize_scores(scores) for name, scores in baseline_scores.items()}
    by_category = _summarize_by_category(logs)
    quality = _quality_summary(logs)
    return {
        "benchmark": "officelife_memory_bench",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "tenants": config.tenants,
            "users_per_tenant": config.users_per_tenant,
            "scenarios": len(scenarios),
            "probes": len(logs),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
        },
        "baselines": baselines,
        "memory_lift": {
            "task_success_rate": baselines["memory_pack"]["task_success_rate"] - baselines["no_memory"]["task_success_rate"],
            "expected_marker_hit_rate": baselines["memory_pack"]["expected_marker_hit_rate"] - baselines["no_memory"]["expected_marker_hit_rate"],
        },
        "by_category": by_category,
        "quality": quality,
        "probe_logs": logs,
        "caveats": [
            "This is a deterministic office/life scenario benchmark for Citefold, not a public leaderboard.",
            "It measures MemoryPack usefulness against a no-memory context on synthetic probes.",
            "No-memory is expected to fail memory-required probes; the important signal is lift without leakage or unsupported-context regressions.",
        ],
    }


def score_context(context: str, probe: Probe, selected_node_count: int) -> dict[str, Any]:
    expected_hits = [marker for marker in probe.expected_markers if marker in context]
    forbidden_hits = [marker for marker in probe.forbidden_markers if marker in context]
    expected_marker_hit_rate = (
        len(expected_hits) / len(probe.expected_markers) if probe.expected_markers else 1.0
    )
    return {
        "passed": expected_marker_hit_rate == 1.0 and not forbidden_hits,
        "expected_hits": expected_hits,
        "expected_marker_hit_rate": expected_marker_hit_rate,
        "forbidden_hits": forbidden_hits,
        "has_sources": "## Sources" in context or "Evidence:" in context,
        "selected_node_count": selected_node_count,
        "context_chars": len(context),
    }


def format_markdown(result: dict[str, Any]) -> str:
    baseline_rows = [
        "| Baseline | task_success_rate | expected_marker_hit_rate | forbidden_hit_count | citation_rate | avg_selected_nodes |",
        "|----------|------------------:|-------------------------:|--------------------:|--------------:|-------------------:|",
    ]
    for name, stats in result["baselines"].items():
        baseline_rows.append(
            f"| {name} | {stats['task_success_rate']:.4f} | {stats['expected_marker_hit_rate']:.4f} | "
            f"{stats['forbidden_hit_count']} | {stats['citation_rate']:.4f} | {stats['avg_selected_nodes']:.2f} |"
        )

    category_rows = [
        "| Category | n | no_memory | memory_pack | lift |",
        "|----------|--:|----------:|------------:|-----:|",
    ]
    for category, stats in result["by_category"].items():
        category_rows.append(
            f"| {category} | {stats['count']} | {stats['no_memory_task_success_rate']:.4f} | "
            f"{stats['memory_pack_task_success_rate']:.4f} | {stats['task_success_lift']:.4f} |"
        )

    parameters = json.dumps(result["parameters"], ensure_ascii=False, indent=2, sort_keys=True)
    environment = json.dumps(result["environment"], ensure_ascii=False, indent=2, sort_keys=True)
    caveats = "\n".join(f"- {item}" for item in result["caveats"])
    quality = result["quality"]

    return (
        "# OfficeLifeMemoryBench Report\n\n"
        f"Generated at: `{result['generated_at']}`\n\n"
        "## Scope\n\n"
        "This benchmark compares `no_memory` against `memory_pack` on synthetic office/life tasks: "
        "preferences, open loops, people follow-up, meeting/voice follow-up, ASR-noise guardrails, no-evidence prompts, and scope isolation.\n\n"
        "## Parameters\n\n"
        f"```json\n{parameters}\n```\n\n"
        "## Environment\n\n"
        f"```json\n{environment}\n```\n\n"
        "## Baseline Comparison\n\n"
        + "\n".join(baseline_rows)
        + "\n\n"
        "## Memory Lift\n\n"
        f"- Task success lift: {result['memory_lift']['task_success_rate']:.4f}\n"
        f"- Expected marker hit-rate lift: {result['memory_lift']['expected_marker_hit_rate']:.4f}\n\n"
        "## By Category\n\n"
        + "\n".join(category_rows)
        + "\n\n"
        "## Quality Checks\n\n"
        f"- Missing expected markers: {quality['missing_expected_marker_count']}\n"
        f"- Unsupported context hits: {quality['unsupported_context_count']}\n"
        f"- Scope leakage count: {quality['scope_leakage_count']}\n\n"
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


def _build_scenarios(config: BenchmarkConfig) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for tenant_index in range(config.tenants):
        for user_index in range(config.users_per_tenant):
            scope = MemoryScope(
                tenant_id=f"tenant-{tenant_index}",
                user_id=f"user-{user_index}",
                namespace="personal",
                agent_id="officelife-benchmark",
                session_id=f"session-{tenant_index}-{user_index}",
            )
            prefix = f"t{tenant_index}-u{user_index}"
            markers = {
                "preference": f"pref-{prefix}",
                "task": f"task-{prefix}",
                "waiting": f"waiting-{prefix}",
                "person": f"Alex-{prefix}",
                "voice_task": f"voice-task-{prefix}",
                "noise": f"asr-noise-{prefix}",
            }
            own_long_term_markers = [
                markers["preference"],
                markers["task"],
                markers["waiting"],
                markers["person"],
                markers["voice_task"],
            ]
            probes = [
                Probe(
                    probe_id=f"{prefix}-preference",
                    category="preference_recall",
                    query="我的回答风格偏好是什么？",
                    expected_markers=[markers["preference"]],
                    forbidden_markers=[],
                    requires_memory=True,
                ),
                Probe(
                    probe_id=f"{prefix}-task",
                    category="task_tracking",
                    query="我现在有什么待办，哪些事情还在等别人回复？",
                    expected_markers=[markers["task"], markers["waiting"]],
                    forbidden_markers=[],
                    requires_memory=True,
                ),
                Probe(
                    probe_id=f"{prefix}-people",
                    category="people_followup",
                    query="Alex 这个客户应该怎么跟进？",
                    expected_markers=[markers["person"]],
                    forbidden_markers=[],
                    requires_memory=True,
                ),
                Probe(
                    probe_id=f"{prefix}-meeting",
                    category="meeting_followup",
                    query="客户方案有什么安排？",
                    expected_markers=[markers["voice_task"]],
                    forbidden_markers=[],
                    requires_memory=True,
                ),
                Probe(
                    probe_id=f"{prefix}-noise",
                    category="asr_noise_guard",
                    query="我的长期偏好里有没有临时语音误听？",
                    expected_markers=[],
                    forbidden_markers=[markers["noise"]],
                    requires_memory=False,
                ),
                Probe(
                    probe_id=f"{prefix}-no-evidence",
                    category="no_evidence",
                    query="我的护照号码是什么？",
                    expected_markers=[],
                    forbidden_markers=own_long_term_markers,
                    requires_memory=False,
                ),
            ]
            scenarios.append(Scenario(scope=scope, markers=markers, probes=probes))
    return scenarios


def _seed_scenario(memory: Citefold, scenario: Scenario) -> None:
    markers = scenario.markers
    memory.ingest_chat(
        scope=scenario.scope,
        messages=[
            {
                "role": "user",
                "content": f"请记住：{markers['preference']} 我喜欢先结论后风险。",
            },
            {
                "role": "user",
                "content": f"提醒我明天上午10点处理{markers['task']}，等法务回复{markers['waiting']}后再推进。",
            },
        ],
        source="text_chat",
    )
    memory.ingest_text(
        scope=scenario.scope,
        text=f"实时语音片段：{markers['noise']} 是 ASR 临时误听，不应进入长期偏好。",
        source="voice_transcript",
        mode="voice",
        final=False,
    )
    memory.ingest_text(
        scope=scenario.scope,
        text=f"语音会话结束：客户方案结论是本周补齐报价。提醒我周五上午9点检查{markers['voice_task']}。",
        source="voice_transcript",
        mode="voice",
        final=True,
    )
    evidence = memory.append_event(
        scope=scenario.scope,
        source="crm_agent",
        payload={"text": f"CRM Agent 发现：{markers['person']} 偏好周五下午跟进。"},
    )
    candidate = memory.submit_candidate(
        scope=scenario.scope,
        source_agent="crm_agent",
        memory_type="people",
        content=f"{markers['person']} 偏好周五下午跟进。",
        evidence_refs=[evidence.evidence_ref],
        confidence=0.82,
    )
    memory.approve_candidate(scenario.scope, candidate.candidate_id)


def _summarize_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(scores),
        "task_success_rate": _mean_bool(score["passed"] for score in scores),
        "expected_marker_hit_rate": statistics.fmean(score["expected_marker_hit_rate"] for score in scores),
        "forbidden_hit_count": sum(len(score["forbidden_hits"]) for score in scores),
        "probes_with_forbidden_hits": sum(1 for score in scores if score["forbidden_hits"]),
        "citation_rate": _mean_bool(score["has_sources"] for score in scores),
        "avg_selected_nodes": statistics.fmean(score["selected_node_count"] for score in scores),
        "avg_context_chars": statistics.fmean(score["context_chars"] for score in scores),
    }


def _summarize_by_category(logs: list[dict[str, Any]]) -> dict[str, Any]:
    categories = sorted({log["category"] for log in logs})
    result: dict[str, Any] = {}
    for category in categories:
        selected = [log for log in logs if log["category"] == category]
        no_memory_rate = _mean_bool(log["no_memory"]["passed"] for log in selected)
        memory_rate = _mean_bool(log["memory_pack"]["passed"] for log in selected)
        result[category] = {
            "count": len(selected),
            "no_memory_task_success_rate": no_memory_rate,
            "memory_pack_task_success_rate": memory_rate,
            "task_success_lift": memory_rate - no_memory_rate,
        }
    return result


def _quality_summary(logs: list[dict[str, Any]]) -> dict[str, Any]:
    missing_expected = [
        {
            "scope": log["scope"],
            "probe_id": log["probe_id"],
            "missing": [
                marker
                for marker in log["expected_markers"]
                if marker not in log["memory_pack"]["expected_hits"]
            ],
        }
        for log in logs
        if log["expected_markers"] and log["memory_pack"]["expected_marker_hit_rate"] < 1.0
    ]
    unsupported = [
        {
            "scope": log["scope"],
            "probe_id": log["probe_id"],
            "category": log["category"],
            "forbidden_hits": log["memory_pack"]["forbidden_hits"],
        }
        for log in logs
        if log["memory_pack"]["forbidden_hits"] and log["category"] in {"no_evidence", "asr_noise_guard"}
    ]
    scope_leaks = [
        {
            "scope": log["scope"],
            "probe_id": log["probe_id"],
            "forbidden_hits": log["memory_pack"]["forbidden_hits"],
        }
        for log in logs
        if log["memory_pack"]["forbidden_hits"] and log["category"] not in {"no_evidence", "asr_noise_guard"}
    ]
    return {
        "missing_expected_markers": missing_expected,
        "missing_expected_marker_count": sum(len(item["missing"]) for item in missing_expected),
        "unsupported_context": unsupported,
        "unsupported_context_count": len(unsupported),
        "scope_leakage": scope_leaks,
        "scope_leakage_count": len(scope_leaks),
    }


def _mean_bool(values: Any) -> float:
    collected = list(values)
    if not collected:
        return 0.0
    return statistics.fmean(1.0 if value else 0.0 for value in collected)


def _scope_key(scope: MemoryScope) -> str:
    return f"{scope.tenant_id}/{scope.user_id}/{scope.namespace}"


def _validate_config(config: BenchmarkConfig) -> None:
    for field_name in ("tenants", "users_per_tenant"):
        value = getattr(config, field_name)
        if value < 1:
            raise ValueError(f"{field_name} must be >= 1")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OfficeLifeMemoryBench.")
    parser.add_argument("--tenants", type=int, default=2)
    parser.add_argument("--users-per-tenant", type=int, default=2)
    parser.add_argument("--root", type=Path, default=None, help="Optional benchmark memory root to keep on disk.")
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_benchmark(
        BenchmarkConfig(
            root=args.root,
            tenants=args.tenants,
            users_per_tenant=args.users_per_tenant,
        )
    )
    write_outputs(result, args.output_json, args.output_md)
    print(format_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
