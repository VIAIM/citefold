import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from benchmarks.longmemeval_qa_benchmark import (
    FakeChatClient,
    OpenAICompatibleChatClient,
    _client_from_env,
    build_memory_pack_reader_prompt,
    build_reader_prompt,
    evaluate_hypotheses,
    format_qa_markdown,
    generate_hypotheses,
    load_env_file,
    summarize_evaluation_logs,
)


def tiny_item() -> dict:
    return {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What degree did I graduate with?",
        "question_date": "2023/05/30 (Tue) 23:40",
        "answer": "Business Administration",
        "answer_session_ids": ["answer_session"],
        "haystack_dates": [
            "2023/05/20 (Sat) 02:21",
            "2023/05/21 (Sun) 03:24",
        ],
        "haystack_session_ids": [
            "distractor_session",
            "answer_session",
        ],
        "haystack_sessions": [
            [
                {
                    "role": "user",
                    "content": "Can you recommend a workout plan?",
                },
                {
                    "role": "assistant",
                    "content": "A brisk walking routine is a good start.",
                },
            ],
            [
                {
                    "role": "user",
                    "content": "I graduated with a Business Administration degree.",
                },
                {
                    "role": "assistant",
                    "content": "I'll remember your Business Administration background.",
                },
            ],
        ],
    }


class LongMemEvalQABenchmarkTest(unittest.TestCase):
    def test_openai_compatible_client_retries_remote_disconnects(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"choices":[{"message":{"content":"recovered"}}]}'

        client = OpenAICompatibleChatClient(
            api_key="test-key",
            base_url="https://example.test/v1",
            retries=1,
        )
        with patch(
            "benchmarks.longmemeval_qa_benchmark.urllib.request.urlopen",
            side_effect=[http.client.RemoteDisconnected("closed"), Response()],
        ) as urlopen, patch("benchmarks.longmemeval_qa_benchmark.time.sleep"):
            result = client.chat("test-model", "prompt", 10)

        self.assertEqual("recovered", result)
        self.assertEqual(2, urlopen.call_count)

    def test_openai_compatible_client_retries_incomplete_response_bodies(self) -> None:
        class IncompleteResponse:
            def __enter__(self) -> "IncompleteResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                raise http.client.IncompleteRead(b'{"choices":')

        class CompleteResponse:
            def __enter__(self) -> "CompleteResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"choices":[{"message":{"content":"recovered"}}]}'

        client = OpenAICompatibleChatClient(
            api_key="test-key",
            base_url="https://example.test/v1",
            retries=1,
        )
        with patch(
            "benchmarks.longmemeval_qa_benchmark.urllib.request.urlopen",
            side_effect=[IncompleteResponse(), CompleteResponse()],
        ) as urlopen, patch("benchmarks.longmemeval_qa_benchmark.time.sleep"):
            result = client.chat("test-model", "prompt", 10)

        self.assertEqual("recovered", result)
        self.assertEqual(2, urlopen.call_count)

    def test_load_env_file_sets_missing_values_without_overriding_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.local"
            env_path.write_text(
                "# local secrets\n"
                "QA_TEST_EXISTING=from-file\n"
                "QA_TEST_VALUE=file-secret\n"
                "QA_TEST_QUOTED=\"quoted secret\"\n",
                encoding="utf-8",
            )

            original_existing = os.environ.get("QA_TEST_EXISTING")
            original_value = os.environ.get("QA_TEST_VALUE")
            original_quoted = os.environ.get("QA_TEST_QUOTED")
            try:
                os.environ["QA_TEST_EXISTING"] = "from-env"
                os.environ.pop("QA_TEST_VALUE", None)
                os.environ.pop("QA_TEST_QUOTED", None)

                load_env_file(env_path)

                self.assertEqual("from-env", os.environ["QA_TEST_EXISTING"])
                self.assertEqual("file-secret", os.environ["QA_TEST_VALUE"])
                self.assertEqual("quoted secret", os.environ["QA_TEST_QUOTED"])
            finally:
                _restore_env("QA_TEST_EXISTING", original_existing)
                _restore_env("QA_TEST_VALUE", original_value)
                _restore_env("QA_TEST_QUOTED", original_quoted)

    def test_client_from_env_reads_api_key_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.local"
            env_path.write_text("QA_CLIENT_KEY=file-key\n", encoding="utf-8")
            original = os.environ.get("QA_CLIENT_KEY")
            try:
                os.environ.pop("QA_CLIENT_KEY", None)

                client = _client_from_env("QA_CLIENT_KEY", "https://example.test", env_file=env_path)

                self.assertEqual("file-key", client.api_key)
                self.assertEqual("https://example.test", client.base_url)
            finally:
                _restore_env("QA_CLIENT_KEY", original)

    def test_reader_prompt_uses_ranked_sessions_and_question_date(self) -> None:
        prompt = build_reader_prompt(tiny_item(), ranked_session_ids=["answer_session"], top_k=1)

        self.assertIn("History Chats", prompt)
        self.assertIn("2023/05/30 (Tue) 23:40", prompt)
        self.assertIn("Business Administration", prompt)
        self.assertIn("What degree did I graduate with?", prompt)
        self.assertNotIn("workout plan", prompt)

    def test_memory_pack_prompt_uses_official_con_reading_shape(self) -> None:
        prompt = build_memory_pack_reader_prompt(tiny_item(), "# MemoryPack\n\nRelevant memory")

        self.assertIn("first extract all the relevant information", prompt)
        self.assertIn("# MemoryPack", prompt)
        self.assertIn("2023/05/30 (Tue) 23:40", prompt)
        self.assertNotIn("Business Administration", prompt)

    def test_generate_hypotheses_writes_jsonl_with_question_id_and_hypothesis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = Path(tmp) / "longmemeval.json"
            out_path = Path(tmp) / "hypotheses.jsonl"
            dataset_path.write_text(json.dumps([tiny_item()]), encoding="utf-8")
            client = FakeChatClient(["Business Administration"])

            generate_hypotheses(
                dataset_path=dataset_path,
                output_path=out_path,
                client=client,
                model="fake-reader",
                top_k=1,
                limit=None,
            )

            rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual("q1", rows[0]["question_id"])
        self.assertEqual("Business Administration", rows[0]["hypothesis"])
        self.assertEqual("bm25-session", rows[0]["generation"]["context_mode"])
        self.assertEqual("fake-reader", rows[0]["generation"]["model"])
        self.assertEqual(1, len(client.requests))
        self.assertEqual("fake-reader", client.requests[0]["model"])

    def test_evaluate_hypotheses_reports_accuracy_by_question_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = Path(tmp) / "longmemeval.json"
            hyp_path = Path(tmp) / "hypotheses.jsonl"
            out_path = Path(tmp) / "eval.jsonl"
            dataset_path.write_text(json.dumps([tiny_item()]), encoding="utf-8")
            hyp_path.write_text(json.dumps({"question_id": "q1", "hypothesis": "Business Administration"}) + "\n", encoding="utf-8")
            client = FakeChatClient(["yes"])

            result = evaluate_hypotheses(
                dataset_path=dataset_path,
                hypothesis_path=hyp_path,
                output_path=out_path,
                client=client,
                judge_model="fake-judge",
            )
            eval_log = out_path.read_text(encoding="utf-8")
            summarized = summarize_evaluation_logs(
                dataset_path=dataset_path,
                evaluation_path=out_path,
                manifest_path=None,
            )

        self.assertEqual(1.0, result["overall_accuracy"])
        self.assertEqual(1.0, result["answerable_accuracy"])
        self.assertEqual(0.0, result["abstention_accuracy"])
        self.assertEqual(1.0, result["by_question_type"]["single-session-user"]["accuracy"])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual("longmemeval.json", result["dataset"]["path"])
        self.assertEqual("hypotheses.jsonl", result["hypotheses"]["path"])
        self.assertEqual("eval.jsonl", summarized["hypotheses"]["path"])
        self.assertIn("autoeval_label", eval_log)

        markdown = format_qa_markdown(result)
        self.assertIn("Coverage: 1/1 (complete)", markdown)
        self.assertIn("Official judge compatible: no", markdown)
        self.assertIn("Answerable accuracy: 1.0000", markdown)

        self.assertEqual(result["overall_accuracy"], summarized["overall_accuracy"])
        self.assertEqual(result["answerable_accuracy"], summarized["answerable_accuracy"])


def _restore_env(name: str, value: Optional[str]) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
