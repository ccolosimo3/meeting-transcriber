from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

from render_transcript_html import (  # noqa: E402
    duration_label,
    human_timestamp,
    meeting_identity,
    render,
)


class HtmlRendererTests(unittest.TestCase):
    def test_timestamp_omits_milliseconds_and_unnecessary_hours(self) -> None:
        self.assertEqual(human_timestamp(237.9), "3:57")
        self.assertEqual(human_timestamp(3721.9), "1:02:01")

    def test_meeting_identity_uses_canonical_bundle_name(self) -> None:
        source = Path(
            "/tmp/20260826-093000-product-planning/transcripts/run-1/transcript.json"
        )

        title, recorded_at, run_id = meeting_identity(source)

        self.assertEqual(title, "Product planning")
        self.assertEqual(recorded_at, "August 26, 2026 at 9:30 AM")
        self.assertEqual(run_id, "run-1")

    def test_meeting_identity_preserves_meaningful_title_case(self) -> None:
        source = Path(
            "/tmp/20260826-093000-Q3-API-review/transcripts/run-1/transcript.json"
        )

        title, _, _ = meeting_identity(source)

        self.assertEqual(title, "Q3 API review")

    def test_meeting_identity_falls_back_for_noncanonical_and_invalid_dates(self) -> None:
        for source in (
            Path("/tmp/external/run/transcript.json"),
            Path(
                "/tmp/20260230-120000-review/transcripts/run-1/transcript.json"
            ),
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    meeting_identity(source),
                    ("Transcript", "Date unavailable", "transcript"),
                )

        fallback_source = Path("/tmp/external/run/Q3-API-review.json")
        self.assertEqual(
            meeting_identity(fallback_source),
            ("Q3 API review", "Date unavailable", "Q3-API-review"),
        )

    def test_duration_label_uses_compact_human_units(self) -> None:
        self.assertEqual(duration_label(185), "3 min")
        self.assertEqual(duration_label(3721), "1 hr 2 min")

    def test_render_includes_editorial_layout_and_theme_controls(self) -> None:
        source = Path(
            "/tmp/20260826-093000-product-planning/transcripts/run-1/transcript.json"
        )
        segments = [
            {
                "speaker": "SPEAKER_00",
                "start": 0,
                "end": 125,
                "text": "Review <the> plan & next steps.",
            },
            {
                "speaker": "SPEAKER_01",
                "start": 126,
                "end": 185,
                "text": "I agree.",
            },
        ]

        rendered = render(
            source,
            "en",
            segments,
            {"SPEAKER_00": "Chris", "SPEAKER_01": "Bethany"},
        )

        self.assertIn("Product planning", rendered)
        self.assertIn("August 26, 2026 at 9:30 AM", rendered)
        self.assertIn("<dd>2</dd>", rendered)
        self.assertIn('id="theme-toggle"', rendered)
        self.assertIn('aria-label="Switch to dark mode"', rendered)
        self.assertIn('aria-pressed="false"', rendered)
        self.assertIn('role="group" aria-label="Speaker color legend"', rendered)
        self.assertIn('class="transcript editorial"', rendered)
        self.assertIn("0:00–2:05", rendered)
        self.assertIn("2:06–3:05", rendered)
        self.assertIn("Chris", rendered)
        self.assertIn("SPEAKER_00", rendered)
        self.assertIn("Review &lt;the&gt; plan &amp; next steps.", rendered)
        self.assertNotIn("Review <the> plan & next steps.", rendered)


if __name__ == "__main__":
    unittest.main()
