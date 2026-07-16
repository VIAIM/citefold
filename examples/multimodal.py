"""Offline text + image ingestion with a supplied, source-linked observation."""

import base64
from tempfile import TemporaryDirectory

from citefold import Citefold, MemoryScope


# A valid transparent 1x1 PNG, embedded so the example needs no fixture files.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


def main() -> None:
    with TemporaryDirectory() as root:
        memory = Citefold(root)
        scope = MemoryScope("acme", "alex", "work", "copilot", "design-review")

        image = memory.ingest_image(
            scope,
            ONE_PIXEL_PNG,
            source="whiteboard_camera",
            mime_type="image/png",
            observations=[
                {
                    "content": "The whiteboard says: launch codename ORCHID-7; owner Maya.",
                    "confidence": 0.96,
                    "locator": {},
                }
            ],
        )
        memory.ingest_text(
            scope,
            "Maya confirmed that the design review is on Friday.",
            source="meeting_chat",
        )

        pack = memory.recall(scope, "What did the whiteboard say about ORCHID-7?")
        print(f"Stored asset: {image.asset_ids[0]}")
        print(pack.markdown)


if __name__ == "__main__":
    main()
