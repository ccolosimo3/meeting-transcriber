from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import time
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECORD_COMMAND = PROJECT_ROOT / "bin" / "record-meeting"
TIMER_PATTERN = re.compile(r"RECORDING · (\d{2}):(\d{2}):(\d{2})")
FINITE_LEVEL_PATTERN = re.compile(r"-\d+ dBFS")


class RecordMeetingTests(unittest.TestCase):
    @staticmethod
    def _ffmpeg_descendant(root_pid: int) -> int | None:
        process_rows: dict[int, tuple[int, str]] = {}
        listing = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,ppid=,comm="], text=True
        )
        for row in listing.splitlines():
            fields = row.split(maxsplit=2)
            if len(fields) == 3:
                process_rows[int(fields[0])] = (int(fields[1]), fields[2])
        descendants = {root_pid}
        changed = True
        while changed:
            changed = False
            for pid, (parent_pid, command) in process_rows.items():
                if parent_pid not in descendants or pid in descendants:
                    continue
                descendants.add(pid)
                changed = True
                if Path(command).name == "ffmpeg":
                    return pid
        return None

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _write_executable(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run_with_fake_capture(
        self, *, valid: bool, capture_status: str | None = None, result_file: bool = False
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        tools_dir = root / "tools"
        prefix = root / "ffmpeg-prefix"
        output_root = root / "meetings"
        capture_argv = root / "capture-argv"
        capture_key_leak = root / "capture-key-leak"
        bootstrap_key_leak = root / "bootstrap-key-leak"

        self._write_executable(
            tools_dir / "dirname",
            r'''#!/usr/bin/env bash
[[ -z "${ASSEMBLYAI_API_KEY:-}" ]] || touch "$STUB_BOOTSTRAP_KEY_LEAK"
exec /usr/bin/dirname "$@"
''',
        )

        self._write_executable(
            tools_dir / "brew",
            f"#!/usr/bin/env bash\nprintf '%s\\n' '{prefix}'\n",
        )
        self._write_executable(
            prefix / "bin" / "ffmpeg",
            r"""#!/usr/bin/env bash
set -euo pipefail
[[ -z "${ASSEMBLYAI_API_KEY:-}" ]] || touch "$STUB_CAPTURE_KEY_LEAK"
printf '%s\0' "$@" > "$STUB_CAPTURE_ARGV"
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
        record_result = root / "record-result.json"
        environment = os.environ.copy()
        environment.update(
            {
                "MEETING_DATA_DIR": str(output_root),
                "MEETING_CONFIG_DIR": str(root / "config"),
                "PATH": f"{tools_dir}:{environment['PATH']}",
                "STUB_CAPTURE_STATUS": capture_status or ("0" if valid else "1"),
                "STUB_CAPTURE_ARGV": str(capture_argv),
                "STUB_CAPTURE_KEY_LEAK": str(capture_key_leak),
                "STUB_BOOTSTRAP_KEY_LEAK": str(bootstrap_key_leak),
                "STUB_PROBE_STATUS": "0" if valid else "1",
                "MEETING_RECORDING_DEVICE": "0",
                "ASSEMBLYAI_API_KEY": "recorder-key-sentinel",
            }
        )
        environment.pop("MEETING_TRANSCRIBER_CAPTURE_INPUT", None)
        environment.pop("MEETING_TRANSCRIBER_RECORD_RESULT_FILE", None)
        if result_file:
            environment["MEETING_TRANSCRIBER_RECORD_RESULT_FILE"] = str(record_result)
        result = subprocess.run(
            ["/bin/bash", str(RECORD_COMMAND), "noninteractive"],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertFalse(capture_key_leak.exists())
        self.assertFalse(bootstrap_key_leak.exists())
        return result, output_root, capture_argv, record_result

    def test_noninteractive_capture_still_finalizes_one_recording(self) -> None:
        result, output_root, capture_argv, record_result = self._run_with_fake_capture(
            valid=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recording saved", result.stderr)
        self.assertIn("meeting transcribe", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(record_result.exists())
        self.assertEqual(len(list(output_root.rglob("recording.wav"))), 1)
        self.assertEqual(list(output_root.rglob("recording.partial.wav")), [])
        arguments = [
            value.decode()
            for value in capture_argv.read_bytes().split(b"\0")
            if value
        ]
        self.assertTrue(
            any(
                arguments[index : index + 4]
                == ["-f", "avfoundation", "-i", ":0"]
                for index in range(len(arguments) - 3)
            ),
            arguments,
        )
        self.assertNotIn("lavfi", arguments)
        self.assertNotIn("-re", arguments)

    def test_failed_capture_retains_only_the_partial_file(self) -> None:
        result, output_root, _, record_result = self._run_with_fake_capture(
            valid=False, result_file=True
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("recording did not finalize; inspect the partial file:", result.stderr)
        self.assertEqual(len(list(output_root.rglob("recording.partial.wav"))), 1)
        self.assertEqual(list(output_root.rglob("recording.wav")), [])
        self.assertFalse(record_result.exists())

    def test_result_file_reports_stopped_and_interrupted_outcomes(self) -> None:
        result, output_root, _, record_result = self._run_with_fake_capture(
            valid=True, result_file=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(record_result.read_text(encoding="utf-8"))
        recordings = list(output_root.rglob("recording.wav"))
        self.assertEqual(payload["outcome"], "stopped")
        self.assertEqual(payload["recording"], str(recordings[0]))

        result, output_root, _, record_result = self._run_with_fake_capture(
            valid=True, capture_status="1", result_file=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("interrupted", result.stderr)
        payload = json.loads(record_result.read_text(encoding="utf-8"))
        self.assertEqual(payload["outcome"], "interrupted")
        recordings = list(output_root.rglob("recording.wav"))
        self.assertEqual(payload["recording"], str(recordings[0]))

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
        ffmpeg = ffmpeg_prefix / "bin" / "ffmpeg"
        if not ffprobe.is_file() or not ffmpeg.is_file():
            self.skipTest("Homebrew ffmpeg@7 is incomplete")
        subprocess.run(
            [str(ffmpeg), "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

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
            os.set_blocking(master, False)
            output = bytearray()
            status_started_at: float | None = None
            metadata_arrivals: list[float] = []
            finite_level_count = 0
            ffmpeg_pid: int | None = None
            ffmpeg_exited_at: float | None = None
            q_sent_at: float | None = None
            try:
                deadline = started_at + 8
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], 0.05)
                    if readable:
                        try:
                            chunk = os.read(master, 65536)
                        except BlockingIOError:
                            continue
                        except OSError as error:
                            if error.errno == errno.EIO:
                                break
                            raise
                        if not chunk:
                            break
                        output.extend(chunk)
                        rendered = output.decode(errors="replace")
                        if status_started_at is None and "-∞ dBFS" in rendered:
                            status_started_at = time.monotonic()
                        if status_started_at is not None and ffmpeg_pid is None:
                            ffmpeg_pid = self._ffmpeg_descendant(process.pid)
                        next_finite_level_count = len(
                            FINITE_LEVEL_PATTERN.findall(rendered)
                        )
                        if next_finite_level_count > finite_level_count:
                            metadata_arrivals.append(time.monotonic())
                            finite_level_count = next_finite_level_count
                        timer_values = [
                            int(hours) * 3600 + int(minutes) * 60 + int(seconds)
                            for hours, minutes, seconds in TIMER_PATTERN.findall(rendered)
                        ]
                        if q_sent_at is None and timer_values and max(timer_values) >= 2:
                            os.write(master, b"q")
                            q_sent_at = time.monotonic()
                    if (
                        q_sent_at is not None
                        and ffmpeg_pid is not None
                        and ffmpeg_exited_at is None
                        and not self._pid_exists(ffmpeg_pid)
                    ):
                        ffmpeg_exited_at = time.monotonic()
                    if process.poll() is not None:
                        break
                return_code = process.wait(timeout=2)
                finalized_at = time.monotonic()
                if (
                    ffmpeg_exited_at is None
                    and ffmpeg_pid is not None
                    and not self._pid_exists(ffmpeg_pid)
                ):
                    ffmpeg_exited_at = finalized_at
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)

            rendered = output.decode(errors="replace")
            self.assertEqual(return_code, 0, rendered)
            self.assertIsNotNone(status_started_at, rendered)
            assert status_started_at is not None
            self.assertGreaterEqual(len(metadata_arrivals), 3, rendered)
            self.assertLess(metadata_arrivals[0] - status_started_at, 1.0, rendered)
            self.assertGreater(metadata_arrivals[-1] - metadata_arrivals[0], 1.0)
            self.assertLess(
                max(
                    later - earlier
                    for earlier, later in zip(
                        metadata_arrivals, metadata_arrivals[1:]
                    )
                ),
                1.0,
                rendered,
            )
            self.assertIsNotNone(q_sent_at, rendered)
            assert q_sent_at is not None
            self.assertIsNotNone(ffmpeg_pid, rendered)
            self.assertIsNotNone(ffmpeg_exited_at, rendered)
            assert ffmpeg_exited_at is not None
            self.assertLess(ffmpeg_exited_at - q_sent_at, 2.0, rendered)
            self.assertLess(finalized_at - q_sent_at, 3.0, rendered)

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
