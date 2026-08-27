from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TranscribeMeetingPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.adapter_marker = self.root / "adapter-launched"
        self.output_root = self.root / "output"
        self.input_path = self.root / "recording.wav"
        self.input_path.write_bytes(b"audio")
        self.ffprobe = self.root / "ffprobe"
        self._write_executable(
            self.ffprobe,
            "#!/usr/bin/env bash\nprintf '%s\\n' \"${STUB_DURATION:-1.0}\"\n",
        )
        self.adapter = self.root / "assemblyai-adapter"
        self._write_executable(
            self.adapter,
            f"#!/usr/bin/env bash\ntouch '{self.adapter_marker}'\nexit 99\n",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _write_executable(path: Path, contents: str) -> None:
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "ASSEMBLYAI_API_KEY": "test-key",
                "MEETING_TRANSCRIBER_ASSEMBLYAI_BIN": str(self.adapter),
                "MEETING_TRANSCRIBER_FFPROBE_BIN": str(self.ffprobe),
                "MEETING_TRANSCRIBER_RUN_ID": "preflight",
            }
        )
        environment.update(overrides)
        return subprocess.run(
            [
                "/bin/bash",
                str(PROJECT_ROOT / "bin" / "transcribe-meeting"),
                str(self.input_path),
                str(self.output_root),
            ],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def test_size_limit_stops_before_ffprobe_or_upload(self) -> None:
        with self.input_path.open("r+b") as stream:
            stream.truncate(2_200_000_001)
        result = self._run(STUB_DURATION="not-a-number")
        self.assertEqual(result.returncode, 2)
        self.assertIn("2.2 GB", result.stderr)
        self.assertIn("Recording   safe at", result.stderr)
        self.assertNotIn("--local", result.stderr)
        self.assertFalse(self.adapter_marker.exists())

    def test_duration_limit_stops_before_upload(self) -> None:
        result = self._run(STUB_DURATION="36000.001")
        self.assertEqual(result.returncode, 2)
        self.assertIn("10 hours", result.stderr)
        self.assertNotIn("--local", result.stderr)
        self.assertFalse(self.adapter_marker.exists())

    def test_missing_credential_stops_before_preflight_or_upload(self) -> None:
        result = self._run(
            ASSEMBLYAI_API_KEY="",
            MEETING_TRANSCRIBER_DISABLE_KEYCHAIN="1",
            STUB_DURATION="not-a-number",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("meeting-transcriber-assemblyai-key", result.stderr)
        self.assertIn("meeting setup", result.stderr)
        self.assertNotIn("--local", result.stderr)
        self.assertFalse(self.adapter_marker.exists())

    def test_run_collision_stops_before_launching_the_adapter(self) -> None:
        self.output_root.mkdir()
        (self.output_root / "preflight").mkdir()
        result = self._run()
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists", result.stderr)
        self.assertFalse(self.adapter_marker.exists())


if __name__ == "__main__":
    unittest.main()
