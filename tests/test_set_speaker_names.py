from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import set_speaker_names  # noqa: E402
from transcribe_assemblyai import convert_response  # noqa: E402
from transcript_bundle import canonical_to_json, save_canonical  # noqa: E402

from test_transcript_bundle import provider_completion  # noqa: E402


class SetSpeakerNamesTests(unittest.TestCase):
    def _make_run(self, root: Path) -> Path:
        run_dir = root / "20260826-093000-weekly-sync" / "transcripts" / "20260826-101500"
        run_dir.mkdir(parents=True)
        json_path = run_dir / "transcript.json"
        save_canonical(json_path, convert_response(provider_completion()))
        return json_path

    def _run_main(self, json_path: Path, answers: str) -> int:
        with (
            mock.patch.object(sys, "argv", ["set_speaker_names.py", str(json_path)]),
            mock.patch.object(sys, "stdin", io.StringIO(answers)),
            mock.patch.object(sys, "stderr", io.StringIO()),
        ):
            return set_speaker_names.main()

    def test_naming_changes_only_speaker_names_and_creates_no_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            json_path = self._make_run(Path(temporary))
            before = json.loads(json_path.read_text(encoding="utf-8"))

            status = self._run_main(json_path, "Casey\n\n")

            self.assertEqual(status, 0)
            after = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(after["speaker_names"], {"SPEAKER_00": "Casey"})
            unchanged_before = {k: v for k, v in before.items() if k != "speaker_names"}
            unchanged_after = {k: v for k, v in after.items() if k != "speaker_names"}
            self.assertEqual(unchanged_before, unchanged_after)

            run_dir = json_path.parent
            self.assertFalse((run_dir / "transcript.speakers.json").exists())
            markdown = (run_dir / "transcript.md").read_text(encoding="utf-8")
            html = (run_dir / "transcript.html").read_text(encoding="utf-8")
            self.assertIn("Casey (SPEAKER_00)", markdown)
            self.assertIn("SPEAKER_01", markdown)
            self.assertIn("Casey", html)
            for name in ("transcript.json", "transcript.md", "transcript.html"):
                self.assertEqual(
                    (run_dir / name).stat().st_mode & 0o777, 0o600, name
                )

    def test_names_survive_save_reload_and_renaming_keeps_other_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            json_path = self._make_run(Path(temporary))
            self.assertEqual(self._run_main(json_path, "Casey\nMorgan\n"), 0)
            self.assertEqual(self._run_main(json_path, "\nJordan\n"), 0)

            after = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                after["speaker_names"],
                {"SPEAKER_00": "Casey", "SPEAKER_01": "Jordan"},
            )

    def test_historical_transcript_without_speaker_names_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "old" / "transcripts" / "run-1"
            run_dir.mkdir(parents=True)
            json_path = run_dir / "transcript.json"
            payload = canonical_to_json(convert_response(provider_completion()))
            del payload["speaker_names"]
            json_path.write_text(json.dumps(payload), encoding="utf-8")

            status = self._run_main(json_path, "Casey\n\n")

            self.assertEqual(status, 2)
            self.assertNotIn(
                "speaker_names", json.loads(json_path.read_text(encoding="utf-8"))
            )


if __name__ == "__main__":
    unittest.main()
