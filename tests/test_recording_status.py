from __future__ import annotations

from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import recording_status  # noqa: E402
from recording_status import format_elapsed, level_to_block, render_lines, run  # noqa: E402


class RecordingStatusTests(unittest.TestCase):
    def test_rendered_status_names_the_microphone_and_shows_audio_history(self) -> None:
        first_line, second_line = render_lines(
            "MacBook Air Microphone",
            65.9,
            [-60.0, -42.0, -24.0, -12.0],
            color=False,
        )

        self.assertEqual(
            first_line,
            "● RECORDING · 00:01:05 · MacBook Air Microphone",
        )
        self.assertIn("▁▄▇█", second_line)
        self.assertIn("-12 dBFS", second_line)
        self.assertTrue(second_line.endswith("q stop"))

    def test_metadata_stream_updates_elapsed_time_and_level(self) -> None:
        output = StringIO()
        run(
            [
                "frame:0 pts:0 pts_time:0\n",
                "lavfi.astats.Overall.Peak_level=-48.0\n",
                "frame:1 pts:48000 pts_time:1.0\n",
                "lavfi.astats.Overall.Peak_level=-18.0\n",
            ],
            "Test Microphone",
            output,
        )

        rendered = output.getvalue()
        self.assertIn("● RECORDING · 00:00:00 · Test Microphone", rendered)
        self.assertIn("● RECORDING · 00:00:01 · Test Microphone", rendered)
        self.assertIn("-18 dBFS", rendered)
        self.assertNotIn("\033[31m", rendered)

    def test_level_and_elapsed_boundaries(self) -> None:
        self.assertEqual(format_elapsed(3661.8), "01:01:01")
        self.assertEqual(level_to_block(float("-inf")), "▁")
        self.assertEqual(level_to_block(-12.0), "█")

    def test_keyboard_interrupt_exits_without_a_traceback(self) -> None:
        with (
            patch.object(sys, "argv", ["recording_status.py", "--microphone", "Test"]),
            patch.object(recording_status, "run", side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(recording_status.main(), 130)


if __name__ == "__main__":
    unittest.main()
