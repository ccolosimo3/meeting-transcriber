from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

from render_transcript_markdown import render, render_markdown  # noqa: E402


def canonical_document(names: dict[str, str]) -> dict[str, object]:
    return {
        "language": "en",
        "text": "Contract proof. Agreed.",
        "segments": [
            {
                "id": 0,
                "start": 0,
                "end": 125,
                "text": "Contract proof.",
                "speaker": "SPEAKER_00",
                "words": [
                    {
                        "word": "Contract",
                        "start": 0,
                        "end": 60,
                        "probability": 0.99,
                        "speaker": "SPEAKER_00",
                    }
                ],
            },
            {
                "id": 1,
                "start": 126,
                "end": 185,
                "text": "Agreed.",
                "speaker": "SPEAKER_01",
                "words": [
                    {
                        "word": "Agreed.",
                        "start": 126,
                        "end": 185,
                        "probability": 0.97,
                        "speaker": "SPEAKER_01",
                    }
                ],
            },
        ],
        "speaker_names": names,
    }


class MarkdownRendererTests(unittest.TestCase):
    def _write(self, root: Path, names: dict[str, str]) -> Path:
        run_dir = root / "20260826-093000-Q3-API-review" / "transcripts" / "20260826-101500"
        run_dir.mkdir(parents=True)
        source = run_dir / "transcript.json"
        source.write_text(json.dumps(canonical_document(names)), encoding="utf-8")
        return source

    def test_markdown_carries_real_meeting_identity_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._write(Path(temporary), {})

            rendered = render(source)

        self.assertIn("# Q3 API review", rendered)
        self.assertNotIn("# transcript", rendered)
        self.assertIn("- Recorded: August 26, 2026 at 9:30 AM", rendered)
        self.assertIn("- Transcript run: 20260826-101500", rendered)
        self.assertIn("- Structured source: `transcript.json`", rendered)
        self.assertIn("- Language: en", rendered)
        self.assertIn("- Speaker turns: 2", rendered)
        self.assertIn("### SPEAKER_00 — 00:00:00.000–00:02:05.000", rendered)

    def test_regeneration_after_naming_keeps_identity_and_shows_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._write(Path(temporary), {"SPEAKER_00": "Casey"})

            destination = render_markdown(source)
            rendered = destination.read_text(encoding="utf-8")

            self.assertEqual(destination, source.parent / "transcript.md")
            self.assertIn("# Q3 API review", rendered)
            self.assertIn("- Recorded: August 26, 2026 at 9:30 AM", rendered)
            self.assertIn("### Casey (SPEAKER_00) — 00:00:00.000–00:02:05.000", rendered)
            self.assertIn("### SPEAKER_01 — 00:02:06.000–00:03:05.000", rendered)


if __name__ == "__main__":
    unittest.main()
