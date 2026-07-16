from __future__ import annotations

import tempfile
import unittest

import citefold
from citefold import Citefold, MemoryScope


class CitefoldBrandApiTest(unittest.TestCase):
    def test_public_api_exposes_brand_and_version(self) -> None:
        self.assertEqual("0.1.0", citefold.__version__)
        self.assertIn("__version__", citefold.__all__)
        self.assertIn("Citefold", citefold.__all__)
        self.assertIn("Citefold", Citefold.__doc__ or "")

    def test_rejected_candidate_cannot_be_approved_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scope = MemoryScope("local", "me", "personal", "test-agent", "test-session")
            memory = Citefold(tmp)
            evidence = memory.append_event(scope, "test", {"text": "A candidate claim."})
            candidate = memory.submit_candidate(
                scope,
                source_agent="test-agent",
                memory_type="semantic",
                content="A candidate claim.",
                evidence_refs=[evidence.evidence_anchor],
                confidence=0.8,
            )

            rejected = memory.reject_candidate(scope, candidate.candidate_id, reason="not true")

            self.assertEqual("rejected", rejected.status)
            self.assertEqual("rejected", memory.approve_candidate(scope, candidate.candidate_id).status)
            self.assertEqual([], memory.list_candidates(scope, status="pending"))
            self.assertEqual("not true", memory.list_candidates(scope)[0]["metadata"]["rejection_reason"])
            self.assertEqual([], memory.list_records(scope))


if __name__ == "__main__":
    unittest.main()
