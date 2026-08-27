from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

from render_transcript_html import render_html  # noqa: E402
from render_transcript_markdown import render_markdown  # noqa: E402
from transcribe_assemblyai import ModelMismatchError, convert_response  # noqa: E402
from transcript_bundle import (  # noqa: E402
    CanonicalTranscriptError,
    canonical_to_json,
    load_canonical,
    parse_canonical,
    save_canonical,
)


def provider_completion() -> dict[str, object]:
    return {
        "id": "job-123",
        "status": "completed",
        "speech_model_used": "universal-3-5-pro",
        "language_code": "en",
        "text": "Hello there. Reply.",
        "utterances": [
            {
                "speaker": "A",
                "start": 0,
                "end": 1000,
                "text": "Hello there.",
                "words": [
                    {"speaker": "A", "start": 0, "end": 400, "text": "Hello", "confidence": 0.99},
                    {"speaker": "A", "start": 400, "end": 1000, "text": "there.", "confidence": 0.97},
                ],
            },
            {
                "speaker": "B",
                "start": 1200,
                "end": 2000,
                "text": "Reply.",
                "words": [
                    {"speaker": "B", "start": 1200, "end": 2000, "text": "Reply.", "confidence": 0.95},
                ],
            },
        ],
    }


def canonical_payload() -> dict[str, object]:
    return canonical_to_json(convert_response(provider_completion()))


class ConversionTests(unittest.TestCase):
    def test_valid_completion_converts_with_empty_speaker_names(self) -> None:
        transcript = convert_response(provider_completion())

        self.assertEqual(transcript.language, "en")
        self.assertEqual(transcript.text, "Hello there. Reply.")
        self.assertEqual(transcript.speaker_names, {})
        self.assertEqual(
            [segment.speaker for segment in transcript.segments],
            ["SPEAKER_00", "SPEAKER_01"],
        )
        self.assertEqual(transcript.segments[0].words[0].word, "Hello")
        self.assertEqual(transcript.segments[0].words[0].probability, 0.99)
        self.assertEqual(transcript.segments[1].start, 1.2)

    def test_malformed_completions_are_rejected(self) -> None:
        empty = provider_completion()
        empty["utterances"] = []
        with self.assertRaises(ValueError):
            convert_response(empty)

        no_words = provider_completion()
        no_words["utterances"][0].pop("words")
        with self.assertRaises(ValueError):
            convert_response(no_words)

        no_text = provider_completion()
        no_text["text"] = ""
        with self.assertRaises(ValueError):
            convert_response(no_text)

    def test_model_mismatch_raises_the_dedicated_error(self) -> None:
        mismatch = provider_completion()
        mismatch["speech_model_used"] = "universal-2"
        with self.assertRaises(ModelMismatchError) as caught:
            convert_response(mismatch)
        self.assertEqual(caught.exception.reported, "universal-2")


class CanonicalValidationTests(unittest.TestCase):
    def test_new_schema_requires_persisted_speaker_names(self) -> None:
        payload = canonical_payload()
        del payload["speaker_names"]
        with self.assertRaises(CanonicalTranscriptError):
            parse_canonical(payload)
        historical_view = parse_canonical(payload, require_speaker_names=False)
        self.assertEqual(historical_view.speaker_names, {})

    def test_speaker_names_must_reference_transcript_labels(self) -> None:
        payload = canonical_payload()
        payload["speaker_names"] = {"SPEAKER_99": "Ghost"}
        with self.assertRaises(CanonicalTranscriptError):
            parse_canonical(payload)

        payload = canonical_payload()
        payload["speaker_names"] = {"SPEAKER_00": "   "}
        with self.assertRaises(CanonicalTranscriptError):
            parse_canonical(payload)

    def test_invalid_timing_and_probability_are_rejected(self) -> None:
        payload = canonical_payload()
        payload["segments"][0]["words"][0]["probability"] = 1.5
        with self.assertRaises(CanonicalTranscriptError):
            parse_canonical(payload)

        payload = canonical_payload()
        payload["segments"][0]["end"] = -1
        with self.assertRaises(CanonicalTranscriptError):
            parse_canonical(payload)

    def test_save_reload_and_view_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = (
                Path(temporary)
                / "20260826-093000-product-planning"
                / "transcripts"
                / "20260826-101500"
            )
            run_dir.mkdir(parents=True)
            json_path = run_dir / "transcript.json"
            transcript = convert_response(provider_completion())
            transcript.speaker_names = {"SPEAKER_00": "Casey"}

            reloaded = save_canonical(json_path, transcript)

            self.assertEqual(json_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(reloaded.speaker_names, {"SPEAKER_00": "Casey"})
            self.assertEqual(canonical_to_json(reloaded), canonical_to_json(transcript))

            markdown_path = render_markdown(json_path)
            html_path = render_html(json_path)
            markdown = markdown_path.read_text(encoding="utf-8")
            html = html_path.read_text(encoding="utf-8")
            self.assertEqual(markdown_path, run_dir / "transcript.md")
            self.assertEqual(html_path, run_dir / "transcript.html")
            self.assertIn("Casey (SPEAKER_00)", markdown)
            self.assertIn("Casey", html)
            self.assertEqual(markdown_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(html_path.stat().st_mode & 0o777, 0o600)

            round_trip = load_canonical(json_path)
            self.assertEqual(canonical_to_json(round_trip), canonical_to_json(transcript))


if __name__ == "__main__":
    unittest.main()
