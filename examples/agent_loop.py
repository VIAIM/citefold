"""Framework-neutral before-turn recall and after-turn ingest hooks."""

from collections.abc import Callable
from tempfile import TemporaryDirectory

from citefold import Citefold, MemoryScope


Responder = Callable[[str, str], str]


def run_turn(
    memory: Citefold,
    scope: MemoryScope,
    user_message: str,
    respond: Responder,
) -> str:
    """Recall before the model call, then record the completed turn."""
    turn = memory.prepare_agent_turn(scope, user_message, token_budget=1_200)
    assistant_message = respond(user_message, turn.memory_pack.markdown)
    memory.complete_agent_turn(
        turn,
        assistant_message,
        source="agent_loop",
    )
    return assistant_message


def demo_responder(_message: str, memory_context: str) -> str:
    """Stand-in for an LLM call so the example stays offline."""
    if "ORCHID-77" in memory_context:
        return "The launch codename is ORCHID-77 (from the cited prior session)."
    return "I do not have supported evidence for that answer."


def main() -> None:
    with TemporaryDirectory() as root:
        memory = Citefold(root)
        prior = MemoryScope("acme", "alex", "work", "copilot", "session-1")
        current = MemoryScope("acme", "alex", "work", "copilot", "session-2")

        memory.ingest_text(
            prior,
            "The launch codename is ORCHID-77.",
            source="chat",
        )
        answer = run_turn(memory, current, "What is the launch codename?", demo_responder)
        print(answer)


if __name__ == "__main__":
    main()
