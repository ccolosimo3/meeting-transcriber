from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

from render_transcript_markdown import render  # noqa: E402


class MarkdownRendererTests(unittest.TestCase):
    def test_agent_transcript_preserves_precise_timestamp_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "transcript.json"
            source.write_text(
                json.dumps(
                    {
                        "language": "en",
                        "segments": [
                            {
                                "speaker": "SPEAKER_00",
                                "start": 0,
                                "end": 125,
                                "text": "Contract proof.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            rendered = render(source)

        self.assertIn(
            "### SPEAKER_00 — 00:00:00.000–00:02:05.000",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
