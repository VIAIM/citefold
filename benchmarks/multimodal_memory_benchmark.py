from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import wave
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from citefold import MemoryPack, MemoryScope, Citefold


DEFAULT_FIXTURE = REPO_ROOT / "benchmarks" / "fixtures" / "multimodal_memory_bench_v1.json"
EXPECTED_CATEGORIES = {
    "text_fact",
    "image_text",
    "audio_commitment",
    "video_audio_visual",
    "low_confidence_asr",
    "preference_correction",
    "unresolved_conflict",
    "no_evidence",
    "media_prompt_injection",
    "deletion_cascade",
}


@dataclass(frozen=True)
class BenchmarkConfig:
    root: Path | None = None
    fixture_path: Path = DEFAULT_FIXTURE
    token_budget: int = 2200


class StepClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current = self.current + timedelta(seconds=1)
        return value


class DeterministicMediaProcessor:
    """Minimal local media adapter for supplied observations.

    The benchmark evaluates memory behavior, not codec quality. It writes a
    deterministic 16 kHz mono WAV so audio ingestion follows the real derived
    asset path without invoking FFmpeg or a network model.
    """

    def standardize_audio(self, source: Path, target: Path) -> None:
        del source
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 1600)

    def probe_duration_ms(self, path: Path) -> int:
        del path
        return 100

    def extract_audio(self, source: Path, target: Path) -> bool:
        del source, target
        return False

    def scene_times_ms(self, source: Path, max_frames: int = 12) -> list[int]:
        del source, max_frames
        return [0]

    def extract_frame(self, source: Path, timestamp_ms: int, target: Path) -> None:
        del source, timestamp_ms
        target.write_bytes(b"deterministic-frame")


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if payload.get("fixture_version") != "1.0" or not isinstance(cases, list):
        raise ValueError("Fixture must contain fixture_version=1.0 and a cases list")
    case_ids = [str(case.get("case_id", "")) for case in cases]
    categories = {str(case.get("category", "")) for case in cases}
    if len(cases) != 10 or len(set(case_ids)) != 10 or categories != EXPECTED_CATEGORIES:
        raise ValueError("Fixture must contain exactly one unique case for each required category")
    for case in cases:
        if case.get("expected_coverage") not in {"none", "supported", "partial"}:
            raise ValueError(f"Invalid expected_coverage in {case['case_id']}")
        for field in ("expected_markers", "forbidden_markers", "forbidden_trusted_markers", "setup"):
            if not isinstance(case.get(field), list):
                raise ValueError(f"{case['case_id']}.{field} must be a list")
    return payload


def run_benchmark(config: BenchmarkConfig = BenchmarkConfig()) -> dict[str, Any]:
    if config.token_budget < 256:
        raise ValueError("token_budget must be >= 256")
    if config.root is None:
        with tempfile.TemporaryDirectory() as tmp:
            return _run_benchmark(replace(config, root=Path(tmp) / "memory-root"))
    return _run_benchmark(config)


def _run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    if config.root is None:
        raise ValueError("root must be set before running the benchmark")
    fixture = load_fixture(config.fixture_path)
    memory = Citefold(
        config.root,
        clock=StepClock(),
        media_processor=DeterministicMediaProcessor(),
    )
    foreign_markers = [str(item) for item in fixture.get("foreign_markers", [])]
    _seed_foreign_scope(memory, foreign_markers)

    logs: list[dict[str, Any]] = []
    scores: dict[str, list[dict[str, Any]]] = {"no_memory": [], "memory_pack": []}
    for index, case in enumerate(fixture["cases"]):
        scope = MemoryScope(
            tenant_id="multimodal-bench",
            user_id=f"case-{index:02d}",
            namespace="personal",
            agent_id="benchmark-agent",
            session_id=f"session-{index:02d}",
        )
        runtime = _seed_case(memory, scope, case)
        pack = memory.recall(scope, str(case["query"]), token_budget=config.token_budget)
        runtime.update(_runtime_facts(memory, scope, pack, runtime))
        no_memory = score_context(
            context="",
            trusted_context="",
            coverage="none",
            citations=[],
            case=case,
            foreign_markers=foreign_markers,
            runtime={},
            apply_runtime_assertions=False,
        )
        memory_pack = score_context(
            context=pack.markdown,
            trusted_context=_trusted_context(memory, scope),
            coverage=pack.coverage,
            citations=pack.citations,
            case=case,
            foreign_markers=foreign_markers,
            runtime=runtime,
            apply_runtime_assertions=True,
        )
        scores["no_memory"].append(no_memory)
        scores["memory_pack"].append(memory_pack)
        logs.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "modality": case["modality"],
                "query": case["query"],
                "requires_memory": bool(case["requires_memory"]),
                "expected_markers": case["expected_markers"],
                "expected_coverage": case["expected_coverage"],
                "no_memory": no_memory,
                "memory_pack": memory_pack,
                "memory_pack_summary": {
                    "coverage": pack.coverage,
                    "citation_count": len(pack.citations),
                    "citation_modalities": sorted(
                        {str(item.get("modality")) for item in pack.citations if item.get("modality")}
                    ),
                    "selected_nodes": [item.path for item in pack.selected_nodes],
                    "conflict_count": len(pack.conflicts),
                    "active_record_count": runtime["active_record_count"],
                    "sources_heading_count": runtime["sources_heading_count"],
                    "hard_delete_cascade_passed": runtime.get("hard_delete_cascade_passed"),
                },
            }
        )

    baselines = {name: _summarize(value) for name, value in scores.items()}
    by_category = _by_category(logs)
    fixture_bytes = config.fixture_path.read_bytes()
    try:
        fixture_display_path = str(config.fixture_path.relative_to(REPO_ROOT))
    except ValueError:
        fixture_display_path = str(config.fixture_path)
    return {
        "benchmark": "multimodal_memory_pack_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture": {
            "path": fixture_display_path,
            "version": fixture["fixture_version"],
            "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "case_count": len(fixture["cases"]),
            "categories": sorted(EXPECTED_CATEGORIES),
        },
        "parameters": {
            "token_budget": config.token_budget,
            "network_calls": 0,
            "model_calls": 0,
            "reader": "deterministic_contract_scorer",
            "comparison": "same query and seeded history; only historical memory context is withheld from no_memory",
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
        },
        "baselines": baselines,
        "memory_lift": {
            "task_success_rate": (
                baselines["memory_pack"]["task_success_rate"]
                - baselines["no_memory"]["task_success_rate"]
            ),
            "expected_marker_hit_rate": (
                baselines["memory_pack"]["expected_marker_hit_rate"]
                - baselines["no_memory"]["expected_marker_hit_rate"]
            ),
        },
        "quality": {
            "unsupported_count": baselines["memory_pack"]["unsupported_count"],
            "forbidden_count": baselines["memory_pack"]["forbidden_count"],
            "scope_leakage_count": baselines["memory_pack"]["scope_leakage_count"],
            "runtime_assertion_failure_count": baselines["memory_pack"]["runtime_assertion_failure_count"],
        },
        "by_category": by_category,
        "probe_logs": logs,
        "caveats": [
            "This is a deterministic local regression benchmark, not a public leaderboard.",
            "It does not measure reader-LLM answer quality; both arms use the same deterministic contract scorer.",
            "Supplied observations isolate memory lifecycle behavior from OCR, ASR, vision-model, and codec quality.",
            "Task success measures whether MemoryPack exposes the expected evidence while respecting safety, coverage, deletion, and scope contracts.",
            "Raw media text may be present only as quoted untrusted evidence; forbidden checks separately inspect trusted active memory.",
        ],
    }


def score_context(
    context: str,
    trusted_context: str,
    coverage: str,
    citations: list[dict[str, Any]],
    case: dict[str, Any],
    foreign_markers: list[str],
    runtime: dict[str, Any],
    apply_runtime_assertions: bool,
) -> dict[str, Any]:
    expected = [str(item) for item in case["expected_markers"]]
    expected_hits = [marker for marker in expected if marker in context]
    forbidden_hits = [str(marker) for marker in case["forbidden_markers"] if str(marker) in context]
    trusted_forbidden_hits = [
        str(marker)
        for marker in case["forbidden_trusted_markers"]
        if str(marker) in trusted_context
    ]
    scope_leakage_hits = [marker for marker in foreign_markers if marker in context]
    unsupported: list[str] = []
    if expected_hits and not citations:
        unsupported.extend(f"uncited:{marker}" for marker in expected_hits)
    if case["expected_coverage"] == "none" and coverage != "none":
        unsupported.append(f"unexpected_coverage:{coverage}")
    coverage_matches = coverage == case["expected_coverage"]
    expected_hit_rate = len(expected_hits) / len(expected) if expected else 1.0
    runtime_failures = _assert_runtime(case.get("assertions", {}), runtime) if apply_runtime_assertions else []
    forbidden = forbidden_hits + [f"trusted:{item}" for item in trusted_forbidden_hits]
    passed = (
        expected_hit_rate == 1.0
        and coverage_matches
        and not unsupported
        and not forbidden
        and not scope_leakage_hits
        and not runtime_failures
    )
    return {
        "passed": passed,
        "expected_hits": expected_hits,
        "expected_marker_hit_rate": expected_hit_rate,
        "coverage": coverage,
        "coverage_matches": coverage_matches,
        "citation_count": len(citations),
        "unsupported": unsupported,
        "forbidden": forbidden,
        "scope_leakage_hits": scope_leakage_hits,
        "runtime_failures": runtime_failures,
        "context_chars": len(context),
    }


def _seed_foreign_scope(memory: Citefold, markers: list[str]) -> None:
    if not markers:
        return
    scope = MemoryScope(
        "multimodal-bench",
        "foreign-user",
        "personal",
        "benchmark-agent",
        "foreign-session",
    )
    memory.ingest_chat(
        scope,
        [{"role": "user", "content": f"隔离测试标记：{marker}"} for marker in markers],
        source="benchmark_fixture",
    )


def _seed_case(
    memory: Citefold,
    scope: MemoryScope,
    case: dict[str, Any],
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "asset_ids": [],
        "deleted_asset_refs": [],
        "deleted_asset_paths": [],
        "forget_outcomes": [],
    }
    for action in case["setup"]:
        operation = action["op"]
        if operation == "ingest_chat":
            result = memory.ingest_chat(
                scope,
                action["messages"],
                source="benchmark_fixture",
            )
            state["asset_ids"].extend(result.asset_ids)
        elif operation == "ingest_image":
            result = memory.ingest_image(
                scope,
                b"\x89PNG\r\n\x1a\nmultimodal-benchmark",
                source="benchmark_fixture",
                observations=action["observations"],
                mime_type="image/png",
            )
            state["asset_ids"].extend(result.asset_ids)
        elif operation == "ingest_audio":
            result = memory.ingest_audio(
                scope,
                b"deterministic-audio-fixture",
                source="benchmark_fixture",
                transcript_segments=action["segments"],
                mime_type="audio/wav",
                duration_ms=int(action["duration_ms"]),
            )
            state["asset_ids"].extend(result.asset_ids)
        elif operation == "ingest_video":
            result = memory.ingest_video(
                scope,
                b"deterministic-video-fixture",
                source="benchmark_fixture",
                transcript_segments=action["segments"],
                frame_observations=action["frames"],
                mime_type="video/mp4",
                duration_ms=int(action["duration_ms"]),
            )
            state["asset_ids"].extend(result.asset_ids)
        elif operation == "correct_active_record":
            matches = [
                record
                for record in memory.list_records(scope)
                if record.get("metadata", {}).get("category") == action["category"]
            ]
            if len(matches) != 1:
                raise AssertionError(f"Expected one correction target, found {len(matches)}")
            memory.correct(scope, matches[0]["record_id"], str(action["content"]))
        elif operation == "forget_first_asset":
            if not state["asset_ids"]:
                raise AssertionError("forget_first_asset requires an earlier media asset")
            asset_id = state["asset_ids"][0]
            asset = memory.store.assets(scope)[asset_id]
            asset_path = memory.store.scope_root(scope) / asset["storage_path"]
            state["deleted_asset_refs"].append(f"asset:{asset_id}")
            state["deleted_asset_paths"].append(asset_path)
            state["forget_outcomes"].append(
                memory.forget(scope, f"asset:{asset_id}", hard=bool(action.get("hard", False)))
            )
        else:
            raise ValueError(f"Unsupported fixture operation: {operation}")
    return state


def _runtime_facts(
    memory: Citefold,
    scope: MemoryScope,
    pack: MemoryPack,
    state: dict[str, Any],
) -> dict[str, Any]:
    deleted_refs = list(state.get("deleted_asset_refs", []))
    outcomes = list(state.get("forget_outcomes", []))
    hard_delete_passed: bool | None = None
    if deleted_refs:
        hard_delete_passed = (
            all(not memory.validate_evidence(scope, ref) for ref in deleted_refs)
            and all(not path.exists() for path in state.get("deleted_asset_paths", []))
            and all(outcome.get("hard") for outcome in outcomes)
            and all(outcome.get("invalidated_episode_ids") for outcome in outcomes)
            and not pack.citations
        )
    return {
        "active_record_count": len(memory.list_records(scope)),
        "citation_modalities": sorted(
            {str(item.get("modality")) for item in pack.citations if item.get("modality")}
        ),
        "conflict_count": len(pack.conflicts),
        "sources_heading_count": sum(line == "## Sources" for line in pack.markdown.split("\n")),
        "hard_delete_cascade_passed": hard_delete_passed,
    }


def _trusted_context(memory: Citefold, scope: MemoryScope) -> str:
    return "\n".join(record.get("content", "") for record in memory.list_records(scope))


def _assert_runtime(assertions: dict[str, Any], runtime: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if "max_active_records" in assertions:
        actual = int(runtime.get("active_record_count", 0))
        expected = int(assertions["max_active_records"])
        if actual > expected:
            failures.append(f"active_record_count:{actual}>{expected}")
    if "min_conflicts" in assertions:
        actual = int(runtime.get("conflict_count", 0))
        expected = int(assertions["min_conflicts"])
        if actual < expected:
            failures.append(f"conflict_count:{actual}<{expected}")
    if "max_sources_headings" in assertions:
        actual = int(runtime.get("sources_heading_count", 0))
        expected = int(assertions["max_sources_headings"])
        if actual > expected:
            failures.append(f"sources_heading_count:{actual}>{expected}")
    required_modalities = set(str(item) for item in assertions.get("required_citation_modalities", []))
    actual_modalities = set(runtime.get("citation_modalities", []))
    missing_modalities = sorted(required_modalities - actual_modalities)
    if missing_modalities:
        failures.append(f"missing_citation_modalities:{','.join(missing_modalities)}")
    if assertions.get("hard_delete_cascade") and runtime.get("hard_delete_cascade_passed") is not True:
        failures.append("hard_delete_cascade_failed")
    return failures


def _summarize(scores: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(scores),
        "task_success_rate": _mean(score["passed"] for score in scores),
        "expected_marker_hit_rate": statistics.fmean(score["expected_marker_hit_rate"] for score in scores),
        "coverage_match_rate": _mean(score["coverage_matches"] for score in scores),
        "citation_rate": _mean(score["citation_count"] > 0 for score in scores),
        "unsupported_count": sum(1 for score in scores if score["unsupported"]),
        "forbidden_count": sum(1 for score in scores if score["forbidden"]),
        "scope_leakage_count": sum(1 for score in scores if score["scope_leakage_hits"]),
        "runtime_assertion_failure_count": sum(1 for score in scores if score["runtime_failures"]),
        "avg_context_chars": statistics.fmean(score["context_chars"] for score in scores),
    }


def _by_category(logs: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for log in logs:
        result[log["category"]] = {
            "no_memory_task_success": log["no_memory"]["passed"],
            "memory_pack_task_success": log["memory_pack"]["passed"],
            "task_success_lift": int(log["memory_pack"]["passed"]) - int(log["no_memory"]["passed"]),
            "unsupported": bool(log["memory_pack"]["unsupported"]),
            "forbidden": bool(log["memory_pack"]["forbidden"]),
            "scope_leakage": bool(log["memory_pack"]["scope_leakage_hits"]),
        }
    return result


def _mean(values: Any) -> float:
    collected = list(values)
    if not collected:
        return 0.0
    return statistics.fmean(1.0 if value else 0.0 for value in collected)


def format_markdown(result: dict[str, Any]) -> str:
    baseline_rows = [
        "| Baseline | task success | expected hit | coverage match | unsupported | forbidden | scope leakage |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, stats in result["baselines"].items():
        baseline_rows.append(
            f"| {name} | {stats['task_success_rate']:.4f} | {stats['expected_marker_hit_rate']:.4f} | "
            f"{stats['coverage_match_rate']:.4f} | {stats['unsupported_count']} | "
            f"{stats['forbidden_count']} | {stats['scope_leakage_count']} |"
        )
    case_rows = [
        "| Case | Modality | No Memory | MemoryPack | Coverage | Citations | Safety |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for log in result["probe_logs"]:
        score = log["memory_pack"]
        safety = "pass" if not (
            score["unsupported"]
            or score["forbidden"]
            or score["scope_leakage_hits"]
            or score["runtime_failures"]
        ) else "fail"
        case_rows.append(
            f"| {log['case_id']} | {log['modality']} | {int(log['no_memory']['passed'])} | "
            f"{int(score['passed'])} | {score['coverage']} | {score['citation_count']} | {safety} |"
        )
    caveats = "\n".join(f"- {item}" for item in result["caveats"])
    return (
        "# Multimodal MemoryPack vs No Memory Benchmark\n\n"
        f"Generated at: `{result['generated_at']}`\n\n"
        "## Outcome\n\n"
        f"- MemoryPack task success: {result['baselines']['memory_pack']['task_success_rate']:.4f}\n"
        f"- No Memory task success: {result['baselines']['no_memory']['task_success_rate']:.4f}\n"
        f"- Memory lift: {result['memory_lift']['task_success_rate']:.4f}\n"
        f"- Unsupported / forbidden / scope leakage: {result['quality']['unsupported_count']} / "
        f"{result['quality']['forbidden_count']} / {result['quality']['scope_leakage_count']}\n\n"
        "## Method\n\n"
        "Each arm receives the same task contract and seeded history. `no_memory` receives no historical context; "
        "`memory_pack` receives the real `Citefold.recall()` result. Supplied observations keep the run "
        "offline and deterministic.\n\n"
        "## Baseline Comparison\n\n"
        + "\n".join(baseline_rows)
        + "\n\n## Cases\n\n"
        + "\n".join(case_rows)
        + "\n\n## Fixture\n\n"
        f"- Path: `{result['fixture']['path']}`\n"
        f"- SHA-256: `{result['fixture']['sha256']}`\n"
        f"- Cases: {result['fixture']['case_count']}\n"
        f"- Network/model calls: {result['parameters']['network_calls']}/{result['parameters']['model_calls']}\n\n"
        "## Caveats\n\n"
        f"{caveats}\n"
    )


def write_outputs(
    result: dict[str, Any],
    json_path: Path | None,
    markdown_path: Path | None,
) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if markdown_path is not None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(format_markdown(result), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline multimodal MemoryPack benchmark.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--token-budget", type=int, default=2200)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_benchmark(
        BenchmarkConfig(
            root=args.root,
            fixture_path=args.fixture,
            token_budget=args.token_budget,
        )
    )
    write_outputs(result, args.output_json, args.output_md)
    print(format_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
