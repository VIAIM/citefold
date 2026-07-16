from __future__ import annotations

import io
import json
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from citefold import Citefold, MemoryScope, __version__
from citefold.cli import build_parser, main


class CitefoldCliTest(unittest.TestCase):
    def _base(self, root: str) -> list[str]:
        return [
            "--root",
            root,
            "--tenant-id",
            "tenant-a",
            "--user-id",
            "user-1",
            "--namespace",
            "personal",
            "--agent-id",
            "cli-agent",
            "--session-id",
            "cli-session",
        ]

    def _run(self, root: str, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main([*self._base(root), *arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def _run_args(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(list(arguments))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_parser_exposes_the_complete_operator_surface(self) -> None:
        choices = build_parser()._subparsers._group_actions[0].choices
        self.assertEqual(
            {
                "ingest-text",
                "ingest-image",
                "ingest-audio",
                "ingest-video",
                "recall",
                "consolidate",
                "correct",
                "archive",
                "forget",
                "rebuild",
                "list",
                "init",
                "doctor",
                "demo",
                "candidates",
            },
            set(choices),
        )

    def test_version_and_local_defaults_work_without_configuration(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["--version"])
        self.assertEqual(0, raised.exception.code)
        self.assertEqual(f"citefold {__version__}\n", stdout.getvalue())

        with patch.dict("os.environ", {}, clear=True):
            args = build_parser().parse_args(["init"])
        self.assertEqual(str(Path.home() / ".citefold"), args.root)
        self.assertEqual("local", args.tenant_id)
        self.assertEqual("me", args.user_id)
        self.assertEqual("personal", args.namespace)
        self.assertEqual("citefold-cli", args.agent_id)
        self.assertEqual("default", args.session_id)

    def test_environment_and_arguments_override_local_defaults(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CITEFOLD_ROOT": "/tmp/citefold-from-env",
                "CITEFOLD_TENANT_ID": "env-tenant",
                "CITEFOLD_USER_ID": "env-user",
            },
            clear=True,
        ):
            args = build_parser().parse_args(
                ["--root", "/tmp/citefold-explicit", "--tenant-id", "explicit-tenant", "init"]
            )
        self.assertEqual("/tmp/citefold-explicit", args.root)
        self.assertEqual("explicit-tenant", args.tenant_id)
        self.assertEqual("env-user", args.user_id)

    def test_init_doctor_and_demo_provide_a_zero_configuration_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status, output, error = self._run_args("--root", tmp, "init")
            self.assertEqual((0, ""), (status, error))
            initialized = json.loads(output)
            self.assertEqual("initialized", initialized["status"])
            self.assertEqual("me", initialized["scope"]["user_id"])

            status, output, error = self._run_args("--root", tmp, "doctor")
            self.assertEqual((0, ""), (status, error))
            diagnosis = json.loads(output)
            self.assertEqual("ok", diagnosis["status"])
            self.assertTrue(diagnosis["checks"]["root_writable"])
            self.assertTrue(diagnosis["checks"]["scope_initialized"])

            status, output, error = self._run_args("--root", tmp, "demo")
            self.assertEqual((0, ""), (status, error))
            demo = json.loads(output)
            self.assertEqual("supported", demo["memory_pack"]["coverage"])
            self.assertIn("ORCHID-91", demo["memory_pack"]["markdown"])
            self.assertTrue(demo["memory_pack"]["citations"])

    def test_candidate_commands_list_approve_and_reject_pending_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scope = MemoryScope("tenant-a", "user-1", "personal", "cli-agent", "cli-session")
            memory = Citefold(tmp)
            evidence = memory.append_event(scope, "crm", {"text": "Alex prefers Friday."})
            approved = memory.submit_candidate(
                scope,
                source_agent="crm",
                memory_type="people",
                content="Alex prefers Friday follow-ups.",
                evidence_refs=[evidence.evidence_anchor],
                confidence=0.9,
            )
            rejected = memory.submit_candidate(
                scope,
                source_agent="crm",
                memory_type="people",
                content="Alex prefers Monday follow-ups.",
                evidence_refs=[evidence.evidence_anchor],
                confidence=0.7,
            )

            status, output, error = self._run(tmp, "candidates", "list")
            self.assertEqual((0, ""), (status, error))
            self.assertEqual(2, len(json.loads(output)))

            status, output, error = self._run(
                tmp, "candidates", "approve", approved.candidate_id
            )
            self.assertEqual((0, ""), (status, error))
            self.assertEqual("approved", json.loads(output)["status"])

            status, output, error = self._run(
                tmp,
                "candidates",
                "reject",
                rejected.candidate_id,
                "--reason",
                "conflicts with user statement",
            )
            self.assertEqual((0, ""), (status, error))
            self.assertEqual("rejected", json.loads(output)["status"])

            status, output, error = self._run(tmp, "candidates", "list", "--status", "pending")
            self.assertEqual((0, ""), (status, error))
            self.assertEqual([], json.loads(output))

    def test_text_lifecycle_commands_emit_machine_readable_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status, output, error = self._run(
                tmp,
                "ingest-text",
                "请记住：我喜欢上午开会。",
                "--source",
                "cli_test",
            )
            self.assertEqual((0, ""), (status, error))
            ingested = json.loads(output)
            self.assertEqual(1, len(ingested["record_ids"]))
            self.assertEqual(1, len(ingested["observation_ids"]))

            status, output, error = self._run(tmp, "list")
            self.assertEqual((0, ""), (status, error))
            records = json.loads(output)
            self.assertEqual(ingested["record_ids"][0], records[0]["record_id"])

            status, output, error = self._run(
                tmp,
                "correct",
                records[0]["record_id"],
                "我不喜欢上午开会，改为下午。",
            )
            self.assertEqual((0, ""), (status, error))
            corrected = json.loads(output)
            self.assertEqual(2, corrected["version"])

            status, output, error = self._run(tmp, "archive", corrected["record_id"])
            self.assertEqual((0, ""), (status, error))
            self.assertEqual("archived", json.loads(output)["status"])

            status, output, error = self._run(tmp, "list", "--all")
            self.assertEqual((0, ""), (status, error))
            self.assertEqual(2, len(json.loads(output)))

            status, output, error = self._run(tmp, "rebuild")
            self.assertEqual((0, ""), (status, error))
            self.assertGreaterEqual(json.loads(output)["documents"], 1)

            status, output, error = self._run(tmp, "consolidate")
            self.assertEqual((0, ""), (status, error))
            self.assertEqual([], json.loads(output))

            status, output, error = self._run(
                tmp,
                "forget",
                f"observation:{ingested['observation_ids'][0]}",
                "--reason",
                "CLI deletion test",
            )
            self.assertEqual((0, ""), (status, error))
            self.assertEqual(ingested["observation_ids"][0], json.loads(output)["evidence_ref"].split(":", 1)[1])

    def test_recall_can_emit_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(0, self._run(tmp, "ingest-text", "提醒我周五提交报价。")[0])

            status, output, error = self._run(tmp, "recall", "我有什么待办？", "--markdown")

            self.assertEqual((0, ""), (status, error))
            self.assertTrue(output.startswith("# MemoryPack\n"))
            self.assertIn("周五提交报价", output)

    def test_multimodal_commands_accept_recorded_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "whiteboard.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            observations = root / "image-observations.json"
            observations.write_text(
                json.dumps([{"content": "项目代号 ORCHID-7", "confidence": 0.96, "locator": {}}]),
                encoding="utf-8",
            )

            status, output, error = self._run(
                tmp,
                "ingest-image",
                str(image),
                "--observations-json",
                str(observations),
            )
            self.assertEqual((0, ""), (status, error))
            self.assertEqual(1, len(json.loads(output)["observation_ids"]))

            audio = root / "meeting.wav"
            with wave.open(str(audio), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 1600)
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps(
                    [{"start_ms": 0, "end_ms": 100, "text": "周五提交报价", "confidence": 0.9}]
                ),
                encoding="utf-8",
            )
            status, output, error = self._run(
                tmp,
                "ingest-audio",
                str(audio),
                "--transcript-json",
                str(transcript),
                "--duration-ms",
                "100",
            )
            self.assertEqual((0, ""), (status, error))
            self.assertGreaterEqual(len(json.loads(output)["asset_ids"]), 1)

            video = root / "meeting.mp4"
            video.write_bytes(b"fixture-video")
            frames = root / "frames.json"
            frames.write_text(
                json.dumps([{"timestamp_ms": 50, "content": "屏幕显示负责人王明", "confidence": 0.9}]),
                encoding="utf-8",
            )
            status, output, error = self._run(
                tmp,
                "ingest-video",
                str(video),
                "--transcript-json",
                str(transcript),
                "--frames-json",
                str(frames),
                "--duration-ms",
                "100",
            )
            self.assertEqual((0, ""), (status, error))
            self.assertEqual(2, len(json.loads(output)["observation_ids"]))

    def test_invalid_json_shape_returns_nonzero_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "whiteboard.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            observations = root / "bad.json"
            observations.write_text('{"content": "not an array"}', encoding="utf-8")

            status, output, error = self._run(
                tmp,
                "ingest-image",
                str(image),
                "--observations-json",
                str(observations),
            )

            self.assertEqual(1, status)
            self.assertEqual("", output)
            self.assertIn("must contain a JSON list", error)
            self.assertNotIn("Traceback", error)


if __name__ == "__main__":
    unittest.main()
