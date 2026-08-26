from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECORD_COMMAND = PROJECT_ROOT / "bin" / "record-meeting"
TIMER_PATTERN = re.compile(r"RECORDING · (\d{2}):(\d{2}):(\d{2})")
FINITE_LEVEL_PATTERN = re.compile(r"-\d+ dBFS")


class RecordMeetingTests(unittest.TestCase):
    def _write_executable(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run_with_fake_capture(self, *, valid: bool) -> tuple[subprocess.CompletedProcess[str], Path]:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        tools_dir = root / "tools"
        prefix = root / "ffmpeg-prefix"
        output_root = root / "meetings"

        self._write_executable(
            tools_dir / "brew",
            f"#!/usr/bin/env bash\nprintf '%s\\n' '{prefix}'\n",
        )
        self._write_executable(
            prefix / "bin" / "ffmpeg",
            r"""#!/usr/bin/env bash
set -euo pipefail
output=""
for argument in "$@"; do output="$argument"; done
printf 'partial audio' > "$output"
exit "${STUB_CAPTURE_STATUS:-0}"
""",
        )
        self._write_executable(
            prefix / "bin" / "ffprobe",
            "#!/usr/bin/env bash\nexit \"${STUB_PROBE_STATUS:-0}\"\n",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "MEETING_DATA_DIR": str(output_root),
                "MEETING_CONFIG_DIR": str(root / "config"),
                "PATH": f"{tools_dir}:{environment['PATH']}",
                "STUB_CAPTURE_STATUS": "0" if valid else "1",
                "STUB_PROBE_STATUS": "0" if valid else "1",
            }
        )
        result = subprocess.run(
            ["/bin/bash", str(RECORD_COMMAND), "noninteractive"],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        return result, output_root

    def test_noninteractive_capture_still_finalizes_one_recording(self) -> None:
        result, output_root = self._run_with_fake_capture(valid=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recording saved:", result.stdout)
        self.assertEqual(len(list(output_root.rglob("recording.wav"))), 1)
        self.assertEqual(list(output_root.rglob("recording.partial.wav")), [])

    def test_failed_capture_retains_only_the_partial_file(self) -> None:
        result, output_root = self._run_with_fake_capture(valid=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("recording did not finalize; inspect the partial file:", result.stderr)
        self.assertEqual(len(list(output_root.rglob("recording.partial.wav"))), 1)
        self.assertEqual(list(output_root.rglob("recording.wav")), [])

    @unittest.skipUnless(os.uname().sysname == "Darwin", "requires macOS PTY and ffmpeg@7")
    def test_interactive_lavfi_pipeline_streams_and_finalizes_on_one_q(self) -> None:
        try:
            ffmpeg_prefix = Path(
                subprocess.check_output(
                    ["brew", "--prefix", "ffmpeg@7"], text=True
                ).strip()
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("Homebrew ffmpeg@7 is unavailable")
        ffprobe = ffmpeg_prefix / "bin" / "ffprobe"
        if not ffprobe.is_file():
            self.skipTest("Homebrew ffprobe is unavailable")

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "meetings"
            environment = os.environ.copy()
            environment.update(
                {
                    "MEETING_DATA_DIR": str(output_root),
                    "MEETING_CONFIG_DIR": str(Path(temporary_directory) / "config"),
                    "MEETING_TRANSCRIBER_CAPTURE_INPUT": (
                        "sine=frequency=1000:sample_rate=48000"
                    ),
                    "NO_COLOR": "1",
                }
            )
            master, slave = os.openpty()
            started_at = time.monotonic()
            process = subprocess.Popen(
                ["/bin/bash", str(RECORD_COMMAND), "pty-lavfi"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=environment,
                close_fds=True,
            )
            os.close(slave)
            output = bytearray()
            first_metadata_at: float | None = None
            q_sent_at: float | None = None
            try:
                deadline = started_at + 8
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], 0.05)
                    if readable:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                break
                            raise
                        if not chunk:
                            break
                        output.extend(chunk)
                        rendered = output.decode(errors="replace")
                        if first_metadata_at is None and FINITE_LEVEL_PATTERN.search(rendered):
                            first_metadata_at = time.monotonic()
                        timer_values = [
                            int(hours) * 3600 + int(minutes) * 60 + int(seconds)
                            for hours, minutes, seconds in TIMER_PATTERN.findall(rendered)
                        ]
                        if q_sent_at is None and timer_values and max(timer_values) >= 2:
                            os.write(master, b"q")
                            q_sent_at = time.monotonic()
                    if process.poll() is not None:
                        break
                return_code = process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)

            rendered = output.decode(errors="replace")
            self.assertEqual(return_code, 0, rendered)
            self.assertIsNotNone(first_metadata_at, rendered)
            assert first_metadata_at is not None
            self.assertLess(first_metadata_at - started_at, 1.0, rendered)
            self.assertIsNotNone(q_sent_at, rendered)
            assert q_sent_at is not None
            self.assertLess(time.monotonic() - q_sent_at, 2.0, rendered)

            displayed_seconds = [
                int(hours) * 3600 + int(minutes) * 60 + int(seconds)
                for hours, minutes, seconds in TIMER_PATTERN.findall(rendered)
            ]
            self.assertTrue({0, 1, 2}.issubset(displayed_seconds), rendered)
            self.assertGreaterEqual(len(FINITE_LEVEL_PATTERN.findall(rendered)), 3)

            wav_files = list(output_root.rglob("*.wav"))
            self.assertEqual(len(wav_files), 1, wav_files)
            self.assertEqual(wav_files[0].name, "recording.wav")
            self.assertEqual(list(output_root.rglob("recording.partial.wav")), [])
            duration = float(
                subprocess.check_output(
                    [
                        str(ffprobe),
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(wav_files[0]),
                    ],
                    text=True,
                ).strip()
            )
            self.assertLess(abs(max(displayed_seconds) - duration), 1.0)


if __name__ == "__main__":
    unittest.main()
