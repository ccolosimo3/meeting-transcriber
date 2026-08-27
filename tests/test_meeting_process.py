from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MeetingProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.bin_dir = self.root / "bin"
        self.lib_dir = self.root / "lib"
        self.bin_dir.mkdir()
        self.lib_dir.mkdir()
        shutil.copy2(PROJECT_ROOT / "bin" / "meeting-process", self.bin_dir)

        self.input_path = self.root / "recording.wav"
        self.input_path.write_bytes(b"synthetic recording")
        self.output_dir = self.root / "transcript-run"
        self.argv_log = self.root / "transcribe-argv"
        self.prepare_log = self.root / "prepare-argv"
        self.notify_log = self.root / "notify"

        self._write_executable(
            self.lib_dir / "config.sh",
            r"""#!/usr/bin/env bash
meeting_data_root() { printf '%s\n' "$MEETING_DATA_DIR"; }
meeting_latest_recording() { return 1; }
meeting_notify() { printf '%s\n' "$1" >> "$STUB_NOTIFY_LOG"; }
""",
        )
        self._write_executable(
            self.bin_dir / "meeting-transcribe",
            r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$STUB_TRANSCRIBE_ARGV"
mkdir -p "$STUB_OUTPUT_DIR"
if [[ "${STUB_TRANSCRIBE_FAIL:-0}" -eq 1 ]]; then
  printf 'provider failed\n' >&2
  exit 7
fi
if [[ "${STUB_CANONICAL_JSON:-1}" -eq 1 ]]; then
  printf '{"segments":[]}' > "$STUB_OUTPUT_DIR/transcript.json"
fi
printf '{"unrelated":true}' > "$STUB_OUTPUT_DIR/diagnostics.json"
printf '%s\n' "$STUB_OUTPUT_DIR" > "$MEETING_TRANSCRIBER_RESULT_FILE"
""",
        )
        self._write_executable(
            self.bin_dir / "meeting-prepare",
            r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$STUB_PREPARE_ARGV"
""",
        )
        self._write_executable(
            self.bin_dir / "name-latest-speakers",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        self._write_executable(
            self.bin_dir / "view-latest-transcript",
            "#!/usr/bin/env bash\nexit 0\n",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_executable(self, path: Path, contents: str) -> None:
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _environment(self, **overrides: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "MEETING_DATA_DIR": str(self.root / "meetings"),
                "MEETING_NOTIFICATIONS": "0",
                "STUB_NOTIFY_LOG": str(self.notify_log),
                "STUB_OUTPUT_DIR": str(self.output_dir),
                "STUB_PREPARE_ARGV": str(self.prepare_log),
                "STUB_TRANSCRIBE_ARGV": str(self.argv_log),
            }
        )
        environment.update(overrides)
        return environment

    def _run(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(self.bin_dir / "meeting-process"), *arguments],
            text=True,
            capture_output=True,
            env=self._environment(**environment),
            check=False,
        )

    @staticmethod
    def _arguments(path: Path) -> list[str]:
        return [item.decode() for item in path.read_bytes().split(b"\0") if item]

    def test_bash_32_completes_with_empty_option_arrays_and_default_copy(self) -> None:
        result = self._run("--skip-names", "--no-open", str(self.input_path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._arguments(self.argv_log), [str(self.input_path)])
        self.assertEqual(
            self._arguments(self.prepare_log),
            [str(self.output_dir / "transcript.json")],
        )
        self.assertIn("Done. Transcript bundle:", result.stdout)
        self.assertIn("Provider: AssemblyAI", result.stdout)
        self.assertEqual(
            self.notify_log.read_text(encoding="utf-8").strip(),
            "Transcript ready: transcript-run",
        )

    def test_bash_32_preserves_populated_options_and_no_copy(self) -> None:
        result = self._run(
            "--backend",
            "mlx",
            "--hotwords",
            "Townchest names",
            "--no-diarize",
            "--skip-names",
            "--no-open",
            "--no-copy",
            str(self.input_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._arguments(self.argv_log),
            [
                "--backend",
                "mlx",
                "--hotwords",
                "Townchest names",
                "--no-diarize",
                str(self.input_path),
            ],
        )
        self.assertIn("Provider: local", result.stdout)
        self.assertEqual(
            self._arguments(self.prepare_log),
            ["--no-copy", str(self.output_dir / "transcript.json")],
        )

    def test_explicit_local_routes_without_changing_provider_neutral_options(self) -> None:
        result = self._run(
            "--local",
            "--speakers",
            "2",
            "--skip-names",
            "--no-open",
            str(self.input_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._arguments(self.argv_log),
            ["--local", "--speakers", "2", str(self.input_path)],
        )
        self.assertIn("Provider: local", result.stdout)

    def test_provider_failure_stops_before_prepare_notify_and_done(self) -> None:
        result = self._run(
            "--skip-names",
            "--no-open",
            str(self.input_path),
            STUB_TRANSCRIBE_FAIL="1",
        )

        self.assertEqual(result.returncode, 7)
        self.assertFalse(self.prepare_log.exists())
        self.assertFalse(self.notify_log.exists())
        self.assertNotIn("Done. Transcript bundle:", result.stdout)

    def test_missing_canonical_json_stops_before_completion(self) -> None:
        result = self._run(
            "--skip-names",
            "--no-open",
            str(self.input_path),
            STUB_CANONICAL_JSON="0",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            f"canonical transcript JSON is missing: {self.output_dir / 'transcript.json'}",
            result.stderr,
        )
        self.assertFalse(self.prepare_log.exists())
        self.assertFalse(self.notify_log.exists())
        self.assertNotIn("Done. Transcript bundle:", result.stdout)


if __name__ == "__main__":
    unittest.main()
