from __future__ import annotations

import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

from render_transcript_markdown import render  # noqa: E402
from transcribe_mlx import finalize_transcript, write_outputs  # noqa: E402
from transcript_bundle import format_timestamp  # noqa: E402


class MlxAdapterTests(unittest.TestCase):
    def test_shared_formatter_preserves_fractional_and_hour_plus_bytes(self) -> None:
        self.assertEqual(format_timestamp(1.234), "00:01.234")
        self.assertEqual(
            format_timestamp(3661.234, always_include_hours=True, decimal_marker=","),
            "01:01:01,234",
        )
        result = {
            "language": "en",
            "text": "Hour-plus segment.",
            "segments": [
                {
                    "start": 3661.234,
                    "end": 3662.346,
                    "text": " Hour-plus segment.",
                    "words": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = Path(temporary_dir)
            write_outputs(result, output_dir, "transcript")
            self.assertEqual(
                (output_dir / "transcript.srt").read_text(encoding="utf-8"),
                "1\n01:01:01,234 --> 01:01:02,346\nHour-plus segment.\n",
            )
            self.assertEqual(
                (output_dir / "transcript.vtt").read_text(encoding="utf-8"),
                "WEBVTT\n\n01:01:01.234 --> 01:01:02.346\nHour-plus segment.\n",
            )

    def test_shared_writer_import_does_not_load_local_model_stack(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, transcript_bundle; "
                "assert not any(name == 'mlx' or name.startswith('mlx.') "
                "for name in sys.modules)",
            ],
            cwd=PROJECT_ROOT / "lib",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_word_level_jitter_does_not_split_a_phrase_turn(self) -> None:
        assigned = {
            "language": "en",
            "segments": [
                {
                    "speaker": "SPEAKER_00",
                    "start": 74.22,
                    "end": 74.80,
                    "text": " That's what I'm testing.",
                    "words": [
                        {
                            "word": " That's",
                            "start": 74.22,
                            "end": 74.36,
                            "speaker": "SPEAKER_01",
                        },
                        {
                            "word": " what I'm testing.",
                            "start": 74.36,
                            "end": 74.80,
                            "speaker": "SPEAKER_00",
                        },
                    ],
                }
            ],
        }

        result = finalize_transcript(assigned)
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["speaker"], "SPEAKER_00")

        with tempfile.TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "meeting.json"
            source.write_text(json.dumps(result), encoding="utf-8")
            source.with_suffix(".speakers.json").write_text(
                json.dumps({"SPEAKER_00": "Chris", "SPEAKER_01": "Bethany"}),
                encoding="utf-8",
            )
            markdown = render(source)

        self.assertIn("### Chris (SPEAKER_00)", markdown)
        self.assertIn("That's what I'm testing.", markdown)
        self.assertNotIn("### Bethany (SPEAKER_01)", markdown)

    def test_bundle_preserves_dotted_name_and_speaker_labels(self) -> None:
        result = {
            "language": "en",
            "text": "A --> B is a complete phrase.",
            "segments": [
                {
                    "speaker": "SPEAKER_01",
                    "start": 1.25,
                    "end": 2.5,
                    "text": " A --> B is a complete phrase.",
                    "words": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = Path(temporary_dir)
            write_outputs(result, output_dir, "weekly sync.v1")
            expected = output_dir / "weekly sync.v1"
            for extension in ("json", "txt", "srt", "vtt", "tsv"):
                self.assertTrue(expected.with_name(f"{expected.name}.{extension}").is_file())
            self.assertIn(
                "[SPEAKER_01]: A --> B is a complete phrase.",
                expected.with_name(f"{expected.name}.txt").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "[SPEAKER_01]: A -> B is a complete phrase.",
                expected.with_name(f"{expected.name}.srt").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "[SPEAKER_01]: A -> B is a complete phrase.",
                expected.with_name(f"{expected.name}.vtt").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
