from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import benchmarks.officelife_track_b_latency as latency_module
from benchmarks.officelife_track_b_latency import (
    LATENCY_CONFIG_SCHEMA_VERSION,
    LatencyAssayError,
    main,
    nearest_rank,
    run_latency_assay,
    validate_latency_bundle,
)
from citefold import MemoryPack, MemoryScope, SelectedNode


SCOPE = MemoryScope(
    tenant_id="tenant-a",
    user_id="user-a",
    namespace="personal",
    agent_id="agent-a",
    session_id="session-a",
)


def canonical_json(value: dict) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def canonical_line(value: dict) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PairClock:
    def __init__(self, durations_ns: list[int]) -> None:
        self.durations = iter(durations_ns)
        self.current = 0
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        if self.calls % 2 == 0:
            self.current += next(self.durations)
        return self.current


class FakeStore:
    def __init__(self, root: Path, observation_count: int) -> None:
        self.root = root
        self.observation_count = observation_count

    def scope_root(self, scope: MemoryScope) -> Path:
        return (
            self.root
            / "tenants"
            / scope.tenant_id
            / "users"
            / scope.user_id
            / "namespaces"
            / scope.namespace
        )

    def observations(self, scope: MemoryScope) -> dict[str, dict]:
        return {
            f"obs-{index:04d}": {
                "observation_id": f"obs-{index:04d}",
                "scope": scope.as_record(),
                "metadata": {"final": True, "mode": "text"},
            }
            for index in range(self.observation_count)
        }


class FakeCitefold:
    observation_count = 1000
    instances: list["FakeCitefold"] = []

    def __init__(self, root: Path, *, openrouter=None) -> None:
        if openrouter is not None:
            raise AssertionError("the latency runner must disable OpenRouter")
        self.root = root
        self.store = FakeStore(root, self.observation_count)
        self.calls: list[dict] = []
        self.instances.append(self)

    def recall(
        self,
        scope: MemoryScope,
        query: str,
        mode: str = "text",
        token_budget: int = 2200,
        include_archived: bool = False,
    ) -> MemoryPack:
        self.calls.append(
            {
                "scope": scope.as_record(),
                "query": query,
                "mode": mode,
                "token_budget": token_budget,
                "include_archived": include_archived,
            }
        )
        return MemoryPack(
            markdown=f"# MemoryPack\n\n{query}\n",
            selected_nodes=[SelectedNode("node-1", "profile/preferences.md", "query-match")],
            identity_scope=scope.as_record(),
            citations=[{"ref": "observation:obs-0000"}],
            coverage="supported",
        )


class OfficeLifeTrackBLatencyTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeCitefold.instances.clear()
        FakeCitefold.observation_count = 1000

    def _inputs(self, root: Path, *, query_count: int = 100) -> tuple[Path, Path, Path]:
        fixture = root / "latency-fixture.zip"
        fixture.write_bytes(b"frozen citefold backup fixture")
        release = root / "citefold-0.2.0-py3-none-any.whl"
        release.write_bytes(b"frozen citefold release distribution")
        queries = root / "latency-queries.jsonl"
        queries.write_bytes(
            b"".join(
                canonical_line(
                    {
                        "mode": "voice" if index % 5 == 0 else "text",
                        "query": f"representative recall query {index:03d}",
                        "query_id": f"query-{index:03d}",
                        "token_budget": 512,
                    }
                )
                for index in range(query_count)
            )
        )
        config = {
            "schema_version": LATENCY_CONFIG_SCHEMA_VERSION,
            "fixture": {
                "filename": fixture.name,
                "sha256": sha256(fixture),
            },
            "queries": {
                "filename": queries.name,
                "sha256": sha256(queries),
            },
            "reference_environment": {
                "cpu_model": "Test CPU",
                "filesystem_type": "apfs",
                "hardware_model": "Test Machine",
                "logical_cpu_count": 8,
                "memory_bytes": 17_179_869_184,
                "operating_system": "macOS",
                "operating_system_version": "15.0",
                "python_implementation": "CPython",
                "python_version": "3.13.5",
                "storage_type": "local-ssd",
            },
            "release_distribution": {
                "filename": release.name,
                "sha256": sha256(release),
            },
            "scope": SCOPE.as_record(),
        }
        config_path = root / "latency-config.json"
        config_path.write_bytes(canonical_json(config))
        return fixture, queries, config_path

    def _restore(self, root: Path, archive: Path, replace: bool = False) -> SimpleNamespace:
        del archive, replace
        scope_root = (
            root
            / "tenants"
            / SCOPE.tenant_id
            / "users"
            / SCOPE.user_id
            / "namespaces"
            / SCOPE.namespace
        )
        scope_root.mkdir(parents=True)
        return SimpleNamespace(status="restored", fingerprint="a" * 64)

    def _run(
        self,
        root: Path,
        output_name: str,
        *,
        durations_ns: list[int] | None = None,
        query_count: int = 100,
    ) -> tuple[dict, Path, PairClock]:
        fixture, queries, config = self._inputs(root, query_count=query_count)
        output = root / output_name
        clock = PairClock(durations_ns or [1_000_000] * 1000)
        verified = SimpleNamespace(
            verified=True,
            sha256=sha256(fixture),
            fingerprint="a" * 64,
            schema_version=2,
            file_count=10,
            total_bytes=1000,
        )
        status = SimpleNamespace(state="current", scope_count=1)
        with (
            patch.object(latency_module, "verify_backup", return_value=verified),
            patch.object(latency_module, "restore_store", side_effect=self._restore),
            patch.object(latency_module, "inspect_store", return_value=status),
            patch.object(latency_module, "Citefold", FakeCitefold),
            patch.object(latency_module.time, "perf_counter_ns", side_effect=clock),
        ):
            report = run_latency_assay(fixture, queries, config, output)
        return report, output, clock

    def test_run_performs_one_warm_pass_and_ten_measured_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, output, clock = self._run(Path(tmp), "latency-bundle")

            self.assertTrue(report["passed"])
            self.assertTrue(report["measurement_complete"])
            self.assertFalse(report["qualification_eligible"])
            self.assertFalse(report["claimable"])
            self.assertEqual(
                [
                    "custodian_signature_missing",
                    "os_sandbox_attestation_missing",
                    "reference_environment_attestation_missing",
                    "release_runtime_binding_unverified",
                ],
                report["nonqualification_reasons"],
            )
            self.assertNotIn("clock_ns", inspect.signature(run_latency_assay).parameters)
            self.assertEqual(1100, len(FakeCitefold.instances[-1].calls))
            self.assertEqual(2000, clock.calls)
            raw = [
                json.loads(line)
                for line in (output / "raw-durations.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(1000, len(raw))
            self.assertEqual((1, 1, 1, "query-000"), (
                raw[0]["pass_index"],
                raw[0]["query_index"],
                raw[0]["measurement_order"],
                raw[0]["query_id"],
            ))
            self.assertEqual((10, 100, 1000, "query-099"), (
                raw[-1]["pass_index"],
                raw[-1]["query_index"],
                raw[-1]["measurement_order"],
                raw[-1]["query_id"],
            ))
            self.assertTrue(all(len(item["response_sha256"]) == 64 for item in raw))

    def test_nearest_rank_and_300_ms_gate_boundary(self) -> None:
        self.assertEqual(300.0, nearest_rank([300.0] * 950 + [301.0] * 50, 95))
        self.assertEqual(301.0, nearest_rank([300.0] * 949 + [301.0] * 51, 95))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            passed, _, _ = self._run(
                root,
                "passed",
                durations_ns=[300_000_000] * 950 + [301_000_000] * 50,
            )
            failed, _, _ = self._run(
                root,
                "failed",
                durations_ns=[300_000_000] * 949 + [301_000_000] * 51,
            )
            self.assertTrue(passed["gate_passed"])
            self.assertEqual(300.0, passed["p95_ms"])
            self.assertFalse(failed["gate_passed"])
            self.assertEqual(301.0, failed["p95_ms"])

    def test_rejects_999_and_1001_observations(self) -> None:
        for count in (999, 1001):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                FakeCitefold.observation_count = count
                with self.assertRaisesRegex(LatencyAssayError, "exactly 1000 finalized observations"):
                    self._run(Path(tmp), "latency-bundle")

    def test_rejects_99_and_101_queries(self) -> None:
        for count in (99, 101):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(LatencyAssayError, "exactly 100 queries"):
                    self._run(Path(tmp), "latency-bundle", query_count=count)

    def test_rejects_fixture_and_release_hash_drift(self) -> None:
        for field_name in ("fixture", "release_distribution"):
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture, queries, config_path = self._inputs(root)
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config[field_name]["sha256"] = "0" * 64
                config_path.write_bytes(canonical_json(config))
                with self.assertRaisesRegex(LatencyAssayError, "SHA-256"):
                    run_latency_assay(fixture, queries, config_path, root / "output")

    def test_validation_rejects_tamper_extra_symlink_and_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pristine, _ = self._run(root, "pristine")
            self.assertTrue(validate_latency_bundle(pristine)["passed"])

            tampered = root / "tampered"
            shutil.copytree(pristine, tampered)
            with (tampered / "latency-summary.json").open("ab") as handle:
                handle.write(b" ")
            self.assertFalse(validate_latency_bundle(tampered)["passed"])

            extra = root / "extra"
            shutil.copytree(pristine, extra)
            (extra / "unexpected.json").write_text("{}\n", encoding="utf-8")
            self.assertFalse(validate_latency_bundle(extra)["passed"])

            symlinked = root / "symlinked"
            shutil.copytree(pristine, symlinked)
            os.symlink("latency-summary.json", symlinked / "unexpected-link.json")
            self.assertFalse(validate_latency_bundle(symlinked)["passed"])

            hardlinked = root / "hardlinked"
            shutil.copytree(pristine, hardlinked)
            os.link(hardlinked / "latency-config.json", hardlinked / "duplicate.json")
            self.assertFalse(validate_latency_bundle(hardlinked)["passed"])

    def test_cli_validate_routes_to_bundle_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, output, _ = self._run(Path(tmp), "latency-bundle")
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                result = main(["validate", str(output)])
            self.assertEqual(0, result)
            self.assertTrue(json.loads(stdout.getvalue())["passed"])


if __name__ == "__main__":
    unittest.main()
