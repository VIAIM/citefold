"""Small, local Citefold example with no model or network calls."""

from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from citefold import Citefold, MemoryScope


def fixed_clock() -> datetime:
    return datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)


def main() -> None:
    with TemporaryDirectory() as root:
        memory = Citefold(root, clock=fixed_clock)
        scope = MemoryScope(
            tenant_id="acme",
            user_id="alex",
            namespace="work",
            agent_id="copilot",
            session_id="launch-planning",
        )

        memory.ingest_text(
            scope,
            "The launch codename is ORCHID-77. Send the launch brief on Friday at 10:00.",
            source="chat",
        )

        pack = memory.recall(
            scope,
            "What is the launch codename and when should I send the brief?",
            token_budget=800,
        )
        print(pack.markdown)


if __name__ == "__main__":
    main()
