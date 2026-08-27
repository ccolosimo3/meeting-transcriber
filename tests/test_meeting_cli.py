from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

import sys  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from test_assemblyai_adapter import running_server, write_published_run  # noqa: E402


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def canonical_document() -> dict[str, object]:
    return {
        "language": "en",
        "text": "Hello. Reply.",
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": 1.0,
                "text": "Hello.",
                "speaker": "SPEAKER_00",
                "words": [
                    {
                        "word": "Hello.",
                        "start": 0.0,
                        "end": 1.0,
                        "probability": 0.99,
                        "speaker": "SPEAKER_00",
                    }
                ],
            },
            {
                "id": 1,
                "start": 1.2,
                "end": 2.0,
                "text": "Reply.",
                "speaker": "SPEAKER_01",
                "words": [
                    {
                        "word": "Reply.",
                        "start": 1.2,
                        "end": 2.0,
                        "probability": 0.97,
                        "speaker": "SPEAKER_01",
                    }
                ],
            },
        ],
        "speaker_names": {},
    }


def run_pty(
    command: list[str],
    input_bytes: bytes,
    env: dict[str, str],
    timeout: int = 30,
) -> tuple[int, str, str]:
    master, slave = os.openpty()
    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    os.write(master, input_bytes)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()
    return process.returncode, stdout.decode(), stderr.decode()


class TranscribeOrchestrationTests(unittest.TestCase):
    """Public `meeting transcribe` orchestration across stubbed layer seams."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.bin_dir = self.root / "bin"
        self.lib_dir = self.root / "lib"
        self.bin_dir.mkdir()
        self.lib_dir.mkdir()
        shutil.copy2(PROJECT_ROOT / "bin" / "meeting-transcribe", self.bin_dir)
        shutil.copy2(PROJECT_ROOT / "lib" / "format.sh", self.lib_dir)
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "bin" / "python").symlink_to(VENV_PYTHON)

        self.meetings_root = self.root / "meetings"
        self.bundle = self.meetings_root / "20260826-100000-weekly-sync"
        self.bundle.mkdir(parents=True)
        self.recording = self.bundle / "recording.wav"
        self.recording.write_bytes(b"synthetic recording")

        self.output_dir = self.bundle / "transcripts" / "cli-run"
        self.argv_log = self.root / "transcribe-argv"
        self.key_env_log = self.root / "transcribe-key-env"
        self.notify_log = self.root / "notify-log"
        self.speakers_log = self.root / "speakers-argv"
        self.speakers_key_log = self.root / "speakers-key-env"
        self.open_log = self.root / "open-argv"
        self.open_key_log = self.root / "open-key-env"

        write_executable(
            self.lib_dir / "config.sh",
            r"""#!/usr/bin/env bash
meeting_data_root() { printf '%s\n' "$MEETING_DATA_DIR"; }
meeting_python_bin() { printf '%s\n' "$STUB_PYTHON_BIN"; }
meeting_latest_recording() {
  [[ -n "${STUB_LATEST_RECORDING:-}" ]] || return 1
  printf '%s\n' "$STUB_LATEST_RECORDING"
}
meeting_notify() {
  if [[ -n "${ASSEMBLYAI_API_KEY:-}" ]]; then
    printf 'key-present ' >> "$STUB_NOTIFY_LOG"
  fi
  printf '%s\n' "$1" >> "$STUB_NOTIFY_LOG"
}
""",
        )
        write_executable(
            self.bin_dir / "transcribe-meeting",
            r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$STUB_TRANSCRIBE_ARGV"
if [[ -n "${ASSEMBLYAI_API_KEY:-}" ]]; then
  printf 'present\n' > "$STUB_TRANSCRIBE_KEY_ENV"
else
  printf 'absent\n' > "$STUB_TRANSCRIBE_KEY_ENV"
fi
status="${STUB_TRANSCRIBE_STATUS:-0}"
if [[ "$status" != "0" && "$status" != "3" ]]; then
  printf 'provider failed\n' >&2
  exit "$status"
fi
mkdir -p "$STUB_OUTPUT_DIR"
if [[ "${STUB_OMIT_CANONICAL:-0}" != "1" ]]; then
  cp "$STUB_CANONICAL_SOURCE" "$STUB_OUTPUT_DIR/transcript.json"
fi
printf '# stub\n' > "$STUB_OUTPUT_DIR/transcript.md"
printf '<!doctype html>\n' > "$STUB_OUTPUT_DIR/transcript.html"
printf '{"state":"published"}' > "$STUB_OUTPUT_DIR/.assemblyai.json"
printf '%s\n' "$STUB_OUTPUT_DIR" > "$MEETING_TRANSCRIBER_RESULT_FILE"
printf '%s\n' "$STUB_OUTPUT_DIR/transcript.json"
exit "$status"
""",
        )
        write_executable(
            self.bin_dir / "meeting-speakers",
            r"""#!/usr/bin/env bash
printf '%s\0' "$@" > "$STUB_SPEAKERS_ARGV"
if [[ -n "${ASSEMBLYAI_API_KEY:-}" ]]; then
  printf 'present\n' > "$STUB_SPEAKERS_KEY_ENV"
else
  printf 'absent\n' > "$STUB_SPEAKERS_KEY_ENV"
fi
""",
        )
        self.open_stub = self.root / "open-stub"
        write_executable(
            self.open_stub,
            r"""#!/usr/bin/env bash
printf '%s\0' "$@" > "$STUB_OPEN_ARGV"
if [[ -n "${ASSEMBLYAI_API_KEY:-}" ]]; then
  printf 'present\n' > "$STUB_OPEN_KEY_ENV"
else
  printf 'absent\n' > "$STUB_OPEN_KEY_ENV"
fi
""",
        )
        canonical_source = self.root / "canonical-source.json"
        canonical_source.write_text(json.dumps(canonical_document()), encoding="utf-8")
        self.canonical_source = canonical_source

    def _environment(self, **overrides: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("ASSEMBLYAI_API_KEY", None)
        environment.update(
            {
                "MEETING_DATA_DIR": str(self.meetings_root),
                "MEETING_NOTIFICATIONS": "1",
                "MEETING_TRANSCRIBER_OPEN_BIN": str(self.open_stub),
                "STUB_PYTHON_BIN": str(VENV_PYTHON),
                "STUB_OUTPUT_DIR": str(self.output_dir),
                "STUB_CANONICAL_SOURCE": str(self.canonical_source),
                "STUB_TRANSCRIBE_ARGV": str(self.argv_log),
                "STUB_TRANSCRIBE_KEY_ENV": str(self.key_env_log),
                "STUB_NOTIFY_LOG": str(self.notify_log),
                "STUB_SPEAKERS_ARGV": str(self.speakers_log),
                "STUB_SPEAKERS_KEY_ENV": str(self.speakers_key_log),
                "STUB_OPEN_ARGV": str(self.open_log),
                "STUB_OPEN_KEY_ENV": str(self.open_key_log),
            }
        )
        environment.update(overrides)
        return environment

    def _run(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(self.bin_dir / "meeting-transcribe"), *arguments],
            text=True,
            capture_output=True,
            env=self._environment(**environment),
            check=False,
        )

    @staticmethod
    def _arguments(path: Path) -> list[str]:
        return [item.decode() for item in path.read_bytes().split(b"\0") if item]

    def test_noninteractive_success_prints_one_stdout_path_and_contains_key(self) -> None:
        result = self._run(
            str(self.recording),
            ASSEMBLYAI_API_KEY="managed-key-sentinel",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip().splitlines(),
            [str(self.output_dir / "transcript.json")],
        )
        self.assertEqual(
            self._arguments(self.argv_log),
            [str(self.recording), str(self.bundle / "transcripts")],
        )
        self.assertEqual(self.key_env_log.read_text(encoding="utf-8").strip(), "present")
        self.assertEqual(
            self.notify_log.read_text(encoding="utf-8").strip(), "Transcript ready"
        )
        self.assertFalse(self.speakers_log.exists())
        self.assertFalse(self.open_log.exists())
        self.assertIn("Transcript ready", result.stderr)
        self.assertIn("Use $meeting to digest:", result.stderr)
        self.assertNotIn("managed-key-sentinel", result.stdout + result.stderr)

    def test_retained_speaker_option_is_forwarded_to_the_run_owner(self) -> None:
        result = self._run("--speakers", "4", "--skip-names", "--no-open", str(self.recording))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._arguments(self.argv_log),
            ["--speakers", "4", str(self.recording), str(self.bundle / "transcripts")],
        )

    def test_latest_selection_is_deterministic_without_a_prompt(self) -> None:
        result = self._run(STUB_LATEST_RECORDING=str(self.recording))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Transcribe this recording?", result.stderr)
        self.assertEqual(
            self._arguments(self.argv_log),
            [str(self.recording), str(self.bundle / "transcripts")],
        )

    def test_no_recording_found_recommends_meeting_record(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("No completed recording found", result.stderr)
        self.assertIn("meeting record", result.stderr)

    def test_provider_failure_passes_status_through_without_success_actions(self) -> None:
        result = self._run(str(self.recording), STUB_TRANSCRIBE_STATUS="7")

        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.notify_log.exists())
        self.assertFalse(self.speakers_log.exists())
        self.assertFalse(self.open_log.exists())

    def test_cleanup_required_exit_3_skips_prompts_and_sends_truthful_notice(self) -> None:
        result = self._run(str(self.recording), STUB_TRANSCRIBE_STATUS="3")

        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            result.stdout.strip().splitlines(),
            [str(self.output_dir / "transcript.json")],
        )
        self.assertEqual(
            self.notify_log.read_text(encoding="utf-8").strip(),
            "Transcript saved; cleanup required",
        )
        self.assertFalse(self.speakers_log.exists())
        self.assertFalse(self.open_log.exists())

    def test_missing_canonical_json_from_the_run_owner_stops_the_flow(self) -> None:
        result = self._run(
            str(self.recording), STUB_OMIT_CANONICAL="1"
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("canonical transcript JSON is missing", result.stderr)
        self.assertFalse(self.notify_log.exists())
        self.assertFalse(self.speakers_log.exists())
        self.assertFalse(self.open_log.exists())

    def test_tty_flow_prompts_names_and_opens_without_leaking_the_key(self) -> None:
        returncode, stdout, stderr = run_pty(
            ["/bin/bash", str(self.bin_dir / "meeting-transcribe")],
            b"y\ny\n",
            self._environment(
                STUB_LATEST_RECORDING=str(self.recording),
                ASSEMBLYAI_API_KEY="managed-key-sentinel",
            ),
        )

        self.assertEqual(returncode, 0, stderr)
        self.assertIn("Transcribe this recording?", stderr)
        self.assertIn("Name the detected speakers now?", stderr)
        self.assertEqual(
            stdout.strip().splitlines(), [str(self.output_dir / "transcript.json")]
        )
        self.assertEqual(
            self._arguments(self.speakers_log),
            [str(self.output_dir / "transcript.json"), "--no-open"],
        )
        self.assertEqual(
            self.speakers_key_log.read_text(encoding="utf-8").strip(), "absent"
        )
        self.assertEqual(
            self._arguments(self.open_log),
            [str(self.output_dir / "transcript.html")],
        )
        self.assertEqual(self.open_key_log.read_text(encoding="utf-8").strip(), "absent")
        self.assertNotIn("managed-key-sentinel", stdout + stderr)

    def test_tty_skip_names_and_no_open_suppress_both_actions(self) -> None:
        returncode, _, stderr = run_pty(
            ["/bin/bash", str(self.bin_dir / "meeting-transcribe"),
             "--skip-names", "--no-open", str(self.recording)],
            b"",
            self._environment(),
        )

        self.assertEqual(returncode, 0, stderr)
        self.assertNotIn("Name the detected speakers now?", stderr)
        self.assertFalse(self.speakers_log.exists())
        self.assertFalse(self.open_log.exists())


class FrontDoorSurfaceTests(unittest.TestCase):
    """Canonical command surface, migration errors, and routing."""

    def _run_meeting(self, *arguments: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(overrides)
        return subprocess.run(
            ["/bin/bash", str(PROJECT_ROOT / "bin" / "meeting"), *arguments],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def test_help_exposes_only_the_canonical_surface(self) -> None:
        result = self._run_meeting("help")

        self.assertEqual(result.returncode, 0)
        for command in (
            "meeting record",
            "meeting transcribe",
            "meeting open",
            "meeting speakers",
            "meeting folder",
            "meeting setup",
            "meeting doctor",
            "meeting help",
            "meeting cleanup",
        ):
            self.assertIn(command, result.stdout)
        for removed in (
            "meeting process",
            "meeting prepare",
            "meeting verify",
            "meeting start",
            "meeting view",
            "meeting files",
            "--local",
        ):
            self.assertNotIn(removed, result.stdout)

    def test_noninteractive_no_arguments_prints_usage_without_a_menu(self) -> None:
        result = self._run_meeting()

        self.assertEqual(result.returncode, 0)
        self.assertIn("meeting transcribe", result.stdout)
        self.assertNotIn("What would you like to do?", result.stdout + result.stderr)

    def test_removed_commands_fail_with_actionable_migration_errors(self) -> None:
        cases = [
            (["process"], "`meeting process` was replaced by `meeting transcribe`."),
            (
                ["prepare"],
                "`meeting prepare` is no longer needed; every transcript now includes transcript.md.",
            ),
            (["verify", "smoke"], "`meeting verify` was removed"),
            (["start"], "`meeting start` was removed; use `meeting record`."),
            (["view"], "`meeting view` was removed; use `meeting open`."),
            (["files"], "`meeting files` was removed; use `meeting folder`."),
        ]
        for arguments, expected in cases:
            with self.subTest(command=arguments[0]):
                result = self._run_meeting(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected, result.stderr)

    def test_unknown_command_shows_concise_help(self) -> None:
        result = self._run_meeting("bogus")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown command: bogus", result.stderr)
        self.assertIn("meeting transcribe", result.stderr)

    def test_removed_flags_fail_with_exit_2_and_one_replacement(self) -> None:
        local_removed = "Local transcription was removed; run `meeting transcribe` to use AssemblyAI."
        cases = [
            (["record", "--output-root", "/tmp/x"], "`--output-root` was removed"),
            (["record", "--dry-run"], "`--dry-run` was removed"),
            (["transcribe", "--local"], local_removed),
            (["transcribe", "--backend", "mlx"], local_removed),
            (["transcribe", "--model", "turbo"], local_removed),
            (["transcribe", "--hotwords", "names"], local_removed),
            (["transcribe", "--no-diarize"], local_removed),
            (["transcribe", "--no-copy"], "`--no-copy` was removed"),
            (["transcribe", "--meetings-root", "/tmp/x"], "`--meetings-root` was removed"),
            (["transcribe", "--recordings-root", "/tmp/x"], "`--recordings-root` was removed"),
            (["transcribe", "--output-root", "/tmp/x"], "`--output-root` was removed"),
            (["open", "--json"], "`--json` was removed"),
            (["open", "--meetings-root"], "`--meetings-root` was removed"),
            (["open", "--transcripts-root"], "`--transcripts-root` was removed"),
            (["open", "--no-open"], "`--no-open` was removed"),
            (["speakers", "--json", "/tmp/x.json"], "`--json` was removed"),
            (["speakers", "--meetings-root", "/tmp/x"], "`--meetings-root` was removed"),
            (["speakers", "--transcripts-root", "/tmp/x"], "`--transcripts-root` was removed"),
            (["setup", "--skip-token"], "`--skip-token` was removed"),
            (["setup", "--skip-assemblyai-key"], "`--skip-assemblyai-key` was removed"),
            (["setup", "--skip-skill"], "`--skip-skill` was removed"),
            (["cleanup", "--force"], "accepts only an optional transcript path"),
            (["doctor", "extra"], "meeting doctor accepts no arguments"),
            (["folder", "extra"], "meeting folder accepts no arguments"),
            (["help", "extra"], "meeting help accepts no arguments"),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = self._run_meeting(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected, result.stderr)

    def test_retained_commands_route_to_their_owners(self) -> None:
        for arguments, expected in (
            (["record", "--help"], "meeting record [--device NAME_OR_INDEX] [LABEL]"),
            (["transcribe", "--help"], "meeting transcribe [INPUT]"),
            (["open", "--help"], "meeting open"),
            (["speakers", "--help"], "meeting speakers [TRANSCRIPT_JSON]"),
            (["folder", "--help"], "meeting folder"),
            (["setup", "--help"], "meeting setup [--device NAME_OR_INDEX]"),
            (["doctor", "--help"], "meeting doctor"),
            (["cleanup", "--help"], "meeting cleanup [TRANSCRIPT_JSON]"),
        ):
            with self.subTest(arguments=arguments):
                result = self._run_meeting(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_record_device_option_routes_to_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            prefix = root / "ffmpeg-prefix"
            capture_argv = root / "capture-argv"
            write_executable(
                tools / "brew", f"#!/usr/bin/env bash\nprintf '%s\\n' '{prefix}'\n"
            )
            write_executable(
                prefix / "bin" / "ffmpeg",
                r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$STUB_CAPTURE_ARGV"
output=""
for argument in "$@"; do output="$argument"; done
printf 'audio' > "$output"
""",
            )
            write_executable(prefix / "bin" / "ffprobe", "#!/usr/bin/env bash\nprintf '1.0\\n'\n")
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{tools}:{environment['PATH']}",
                    "MEETING_DATA_DIR": str(root / "meetings"),
                    "MEETING_CONFIG_DIR": str(root / "config"),
                    "STUB_CAPTURE_ARGV": str(capture_argv),
                }
            )
            environment.pop("MEETING_TRANSCRIBER_CAPTURE_INPUT", None)
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(PROJECT_ROOT / "bin" / "meeting"),
                    "record",
                    "--device",
                    "5",
                    "routing",
                ],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = [
                value.decode()
                for value in capture_argv.read_bytes().split(b"\0")
                if value
            ]
            self.assertIn(":5", arguments)


class DiscoveryTests(unittest.TestCase):
    """Latest-run discovery through the real `meeting open` and `meeting speakers`."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.meetings_root = self.root / "meetings"
        self.meetings_root.mkdir()
        self.open_log = self.root / "open-argv"
        self.open_stub = self.root / "open-stub"
        write_executable(
            self.open_stub,
            '#!/usr/bin/env bash\nprintf \'%s\\0\' "$@" > "$STUB_OPEN_ARGV"\n',
        )

    def _make_new_run(
        self,
        bundle_name: str,
        run_name: str,
        *,
        state: str = "published",
        deletion: dict[str, object] | None | str = "confirmed",
        names: dict[str, str] | None = None,
        omit: tuple[str, ...] = (),
    ) -> Path:
        run_dir = self.meetings_root / bundle_name / "transcripts" / run_name
        run_dir.mkdir(parents=True)
        document = canonical_document()
        if names:
            document["speaker_names"] = names
        if "transcript.json" not in omit:
            (run_dir / "transcript.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
        if "transcript.md" not in omit:
            (run_dir / "transcript.md").write_text("# run\n", encoding="utf-8")
        if "transcript.html" not in omit:
            (run_dir / "transcript.html").write_text("<!doctype html>\n", encoding="utf-8")
        receipt: dict[str, object] = {
            "provider": "assemblyai",
            "requested_model": "universal-3-5-pro",
            "used_model": "universal-3-5-pro",
            "transcript_id": "job-123",
            "state": state,
            "provider_status": "completed",
            "timestamps": {
                "created_at": "2026-08-26T10:00:00Z",
                "submitted_at": "2026-08-26T10:01:00Z",
                "completed_at": "2026-08-26T10:02:00Z",
            },
        }
        if deletion == "confirmed":
            receipt["deletion"] = {"confirmed": True, "confirmed_at": "2026-08-26T10:03:00Z"}
        elif deletion == "unconfirmed":
            receipt["deletion"] = {"confirmed": False, "last_error": "boom"}
        elif isinstance(deletion, dict):
            receipt["deletion"] = deletion
        (run_dir / ".assemblyai.json").write_text(json.dumps(receipt), encoding="utf-8")
        return run_dir

    def _make_historical_run(
        self, bundle_name: str, run_name: str, *, old_receipt: dict[str, object] | None = None
    ) -> Path:
        run_dir = self.meetings_root / bundle_name / "transcripts" / run_name
        run_dir.mkdir(parents=True)
        (run_dir / "transcript.json").write_text(
            json.dumps({"language": "en", "segments": []}), encoding="utf-8"
        )
        (run_dir / "transcript.html").write_text("<!doctype html>old\n", encoding="utf-8")
        for legacy in ("transcript.txt", "transcript.srt", "transcript.vtt", "transcript.tsv"):
            (run_dir / legacy).write_text("legacy\n", encoding="utf-8")
        (run_dir / "transcript.speakers.json").write_text(
            json.dumps({"SPEAKER_00": "Casey"}), encoding="utf-8"
        )
        (run_dir / "transcript.agent.md").write_text("# old agent\n", encoding="utf-8")
        if old_receipt is not None:
            (run_dir / "transcript.assemblyai.json").write_text(
                json.dumps(old_receipt), encoding="utf-8"
            )
        return run_dir

    def _run_open(self) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "MEETING_DATA_DIR": str(self.meetings_root),
                "MEETING_TRANSCRIBER_OPEN_BIN": str(self.open_stub),
                "STUB_OPEN_ARGV": str(self.open_log),
            }
        )
        return subprocess.run(
            ["/bin/bash", str(PROJECT_ROOT / "bin" / "meeting-open")],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def _opened_path(self) -> str:
        values = [item.decode() for item in self.open_log.read_bytes().split(b"\0") if item]
        self.assertEqual(len(values), 1)
        return values[0]

    def test_clean_latest_run_opens_without_a_warning(self) -> None:
        self._make_new_run("20260826-100000-a", "20260826-110000")
        expected = self._make_new_run("20260826-120000-b", "20260826-130000")

        result = self._run_open()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(expected / "transcript.html"))
        self.assertNotIn("Remote cleanup required", result.stderr)

    def test_newer_unconfirmed_run_is_selected_with_cleanup_warning_only(self) -> None:
        self._make_new_run("20260826-100000-a", "20260826-110000")
        newer = self._make_new_run(
            "20260826-120000-b", "20260826-130000", deletion="unconfirmed"
        )

        result = self._run_open()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(newer / "transcript.html"))
        self.assertIn("Remote cleanup required", result.stderr)
        self.assertIn("job-123", result.stderr)
        self.assertIn(f"meeting cleanup {newer / 'transcript.json'}", result.stderr)
        self.assertNotIn("Retry", result.stderr)
        self.assertNotIn("meeting transcribe", result.stderr)

    def test_published_receipt_without_deletion_field_is_cleanup_required(self) -> None:
        run = self._make_new_run("20260826-100000-a", "20260826-110000", deletion=None)

        result = self._run_open()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(run / "transcript.html"))
        self.assertIn("Remote cleanup required", result.stderr)

    def test_newer_partial_and_pre_publication_runs_are_skipped(self) -> None:
        expected = self._make_new_run("20260826-100000-a", "20260826-110000")
        self._make_new_run(
            "20260826-100000-a", "20260826-120000", omit=("transcript.md",)
        )
        self._make_new_run("20260826-100000-a", "20260826-130000", state="submitted")
        malformed = self.meetings_root / "20260826-100000-a" / "transcripts" / "20260826-140000"
        malformed.mkdir()
        (malformed / ".assemblyai.json").write_text("not json", encoding="utf-8")
        (malformed / "transcript.json").write_text("not json", encoding="utf-8")
        (malformed / "transcript.md").write_text("x", encoding="utf-8")
        (malformed / "transcript.html").write_text("x", encoding="utf-8")

        result = self._run_open()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(expected / "transcript.html"))

    def test_symlinked_artifacts_are_never_selected(self) -> None:
        expected = self._make_new_run("20260826-100000-a", "20260826-110000")
        target = self._make_new_run("20260826-100000-a", "20260826-115000")
        symlinked = self.meetings_root / "20260826-100000-a" / "transcripts" / "20260826-120000"
        symlinked.mkdir()
        (symlinked / "transcript.json").symlink_to(target / "transcript.json")
        (symlinked / "transcript.md").symlink_to(target / "transcript.md")
        (symlinked / "transcript.html").symlink_to(target / "transcript.html")
        (symlinked / ".assemblyai.json").symlink_to(target / ".assemblyai.json")

        result = self._run_open()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(target / "transcript.html"))
        _ = expected

    def test_timestamp_ties_break_deterministically_by_bundle_name(self) -> None:
        self._make_new_run("20260826-100000-alpha", "20260826-110000")
        expected = self._make_new_run("20260826-100000-beta", "20260826-110000")

        result = self._run_open()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(expected / "transcript.html"))

    def test_historical_runs_open_without_mutation(self) -> None:
        run = self._make_historical_run("20260825-100000-old", "20260825-110000")
        hashes = {path.name: file_sha(path) for path in run.iterdir()}

        result = self._run_open()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(run / "transcript.html"))
        self.assertNotIn("Remote cleanup required", result.stderr)
        for path in run.iterdir():
            self.assertEqual(file_sha(path), hashes[path.name], path.name)

    def test_assemblyai_era_historical_receipts_warn_but_are_never_rewritten(self) -> None:
        clean = self._make_historical_run(
            "20260825-100000-old",
            "20260825-110000",
            old_receipt={"state": "published", "transcript_id": "old-1",
                         "deletion": {"confirmed": True}},
        )
        result = self._run_open()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(clean / "transcript.html"))
        self.assertNotIn("Remote cleanup required", result.stderr)

        unconfirmed = self._make_historical_run(
            "20260826-100000-older2",
            "20260826-110000",
            old_receipt={"state": "published", "transcript_id": "old-2",
                         "deletion": {"confirmed": False}},
        )
        receipt_sha = file_sha(unconfirmed / "transcript.assemblyai.json")
        result = self._run_open()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._opened_path(), str(unconfirmed / "transcript.html"))
        self.assertIn("Remote cleanup required", result.stderr)
        self.assertIn("old-2", result.stderr)
        self.assertIn("AssemblyAI dashboard", result.stderr)
        self.assertNotIn("meeting cleanup ", result.stderr)
        self.assertEqual(
            file_sha(unconfirmed / "transcript.assemblyai.json"), receipt_sha
        )

    def test_no_usable_run_recommends_meeting_transcribe(self) -> None:
        result = self._run_open()

        self.assertEqual(result.returncode, 2)
        self.assertIn("No transcript found", result.stderr)
        self.assertIn("meeting transcribe", result.stderr)

    def _speakers_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "MEETING_DATA_DIR": str(self.meetings_root),
                "MEETING_TRANSCRIBER_OPEN_BIN": str(self.open_stub),
                "STUB_OPEN_ARGV": str(self.open_log),
            }
        )
        return environment

    def test_speakers_requires_a_tty(self) -> None:
        result = subprocess.run(
            ["/bin/bash", str(PROJECT_ROOT / "bin" / "meeting-speakers")],
            text=True,
            capture_output=True,
            env=self._speakers_environment(),
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires an interactive terminal", result.stderr)

    def test_speakers_refuses_historical_runs_with_an_explanation(self) -> None:
        self._make_historical_run("20260825-100000-old", "20260825-110000")

        returncode, _, stderr = run_pty(
            ["/bin/bash", str(PROJECT_ROOT / "bin" / "meeting-speakers")],
            b"",
            self._speakers_environment(),
        )

        self.assertEqual(returncode, 2, stderr)
        self.assertIn("saved HTML remains readable", stderr)
        self.assertIn("Transcribe the recording again", stderr)

    def test_speakers_warns_on_cleanup_required_and_edits_only_names(self) -> None:
        run = self._make_new_run(
            "20260826-100000-a", "20260826-110000", deletion="unconfirmed"
        )
        receipt_sha = file_sha(run / ".assemblyai.json")

        returncode, _, stderr = run_pty(
            [
                "/bin/bash",
                str(PROJECT_ROOT / "bin" / "meeting-speakers"),
                str(run / "transcript.json"),
                "--no-open",
            ],
            b"Casey\n\n",
            self._speakers_environment(),
        )

        self.assertEqual(returncode, 0, stderr)
        self.assertIn("Remote cleanup required", stderr)
        self.assertIn("job-123", stderr)
        self.assertIn("never change remote state", stderr)
        document = json.loads((run / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(document["speaker_names"], {"SPEAKER_00": "Casey"})
        self.assertEqual(file_sha(run / ".assemblyai.json"), receipt_sha)
        self.assertIn("Casey", (run / "transcript.md").read_text(encoding="utf-8"))
        self.assertIn("Casey", (run / "transcript.html").read_text(encoding="utf-8"))
        self.assertFalse(self.open_log.exists())
        self.assertFalse((run / "transcript.speakers.json").exists())


class CleanupCommandTests(unittest.TestCase):
    """The recovery-only `meeting cleanup` wrapper over the loopback server."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.meetings_root = self.root / "meetings"
        clean_run = (
            self.meetings_root / "20260826-120000-newer" / "transcripts" / "20260826-130000"
        )
        write_published_run(
            clean_run,
            deletion={"confirmed": True, "confirmed_at": "2026-08-26T13:01:00Z"},
        )
        self.pending_run = (
            self.meetings_root / "20260826-100000-older" / "transcripts" / "20260826-110000"
        )
        self.pending_json = write_published_run(
            self.pending_run, deletion={"confirmed": False, "last_error": "boom"}
        )

    def _run_cleanup(
        self, base_url: str, *arguments: str, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("ASSEMBLYAI_API_KEY", None)
        environment.update(
            {
                "MEETING_DATA_DIR": str(self.meetings_root),
                "MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE": base_url,
                "MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL": "0",
                "ASSEMBLYAI_API_KEY": "cleanup-key-sentinel",
            }
        )
        environment.update(overrides)
        for key, value in list(environment.items()):
            if value == "":
                environment.pop(key)
        return subprocess.run(
            ["/bin/bash", str(PROJECT_ROOT / "bin" / "meeting-cleanup"), *arguments],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def test_default_target_selection_transitions_the_pending_receipt(self) -> None:
        with running_server({"delete_statuses": [200]}) as (server, base_url):
            result = self._run_cleanup(base_url)
            methods = [request["method"] for request in server.requests]

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip().splitlines(), [str(self.pending_json.resolve())]
            )
            self.assertEqual(methods, ["DELETE"])
            self.assertIn("Remote cleanup complete", result.stderr)
            receipt = json.loads(
                (self.pending_run / ".assemblyai.json").read_text(encoding="utf-8")
            )
            self.assertTrue(receipt["deletion"]["confirmed"])
            self.assertNotIn("cleanup-key-sentinel", json.dumps(receipt))
            self.assertNotIn(
                "cleanup-key-sentinel", result.stdout + result.stderr
            )

            second = self._run_cleanup(base_url)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("No transcript requires cleanup.", second.stderr)
            self.assertEqual(
                [request["method"] for request in server.requests], ["DELETE"]
            )

    def test_explicit_transcript_path_beats_default_selection(self) -> None:
        # A second, older cleanup-required run: default selection would pick
        # the newer pending run, so only the explicit-path branch can clean
        # this one.
        explicit_run = (
            self.meetings_root / "20260825-090000-oldest" / "transcripts" / "20260825-100000"
        )
        explicit_json = write_published_run(
            explicit_run, deletion={"confirmed": False, "last_error": "boom"}
        )
        with running_server({"delete_statuses": [200]}) as (server, base_url):
            result = self._run_cleanup(base_url, str(explicit_json))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip().splitlines(), [str(explicit_json.resolve())]
            )
            self.assertEqual(
                [request["method"] for request in server.requests], ["DELETE"]
            )
        explicit_receipt = json.loads(
            (explicit_run / ".assemblyai.json").read_text(encoding="utf-8")
        )
        self.assertTrue(explicit_receipt["deletion"]["confirmed"])
        untouched = json.loads(
            (self.pending_run / ".assemblyai.json").read_text(encoding="utf-8")
        )
        self.assertFalse(untouched["deletion"]["confirmed"])

    def test_missing_key_stops_before_any_request_or_mutation(self) -> None:
        receipt_sha = file_sha(self.pending_run / ".assemblyai.json")
        with running_server({}) as (server, base_url):
            result = self._run_cleanup(
                base_url,
                ASSEMBLYAI_API_KEY="",
                MEETING_TRANSCRIBER_DISABLE_KEYCHAIN="1",
            )
            self.assertEqual(server.requests, [])

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("meeting setup", result.stderr)
        self.assertEqual(file_sha(self.pending_run / ".assemblyai.json"), receipt_sha)


class MenuRecorderResultTests(unittest.TestCase):
    """The no-argument menu offers upload only after a normal q stop."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.bin_dir = self.root / "bin"
        self.lib_dir = self.root / "lib"
        self.bin_dir.mkdir()
        self.lib_dir.mkdir()
        shutil.copy2(PROJECT_ROOT / "bin" / "meeting", self.bin_dir)
        shutil.copy2(PROJECT_ROOT / "lib" / "config.sh", self.lib_dir)
        shutil.copy2(PROJECT_ROOT / "lib" / "format.sh", self.lib_dir)
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "bin" / "python").symlink_to(VENV_PYTHON)
        self.recording_path = self.root / "meetings" / "bundle" / "recording.wav"
        self.transcribe_log = self.root / "transcribe-argv"
        write_executable(
            self.bin_dir / "record-meeting",
            r"""#!/usr/bin/env bash
set -euo pipefail
case "${STUB_RECORD_MODE:-stopped}" in
  fail)
    printf 'Error: recording did not finalize\n' >&2
    exit 1
    ;;
  *)
    printf '{"recording":"%s","outcome":"%s"}\n' \
      "$STUB_RECORDING_PATH" "$STUB_RECORD_MODE" \
      > "$MEETING_TRANSCRIBER_RECORD_RESULT_FILE"
    ;;
esac
""",
        )
        write_executable(
            self.bin_dir / "meeting-transcribe",
            '#!/usr/bin/env bash\nprintf \'%s\\0\' "$@" > "$STUB_MENU_TRANSCRIBE_ARGV"\n',
        )

    def _run_menu(self, mode: str, keys: bytes) -> tuple[int, str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "MEETING_DATA_DIR": str(self.root / "meetings"),
                "STUB_RECORD_MODE": mode,
                "STUB_RECORDING_PATH": str(self.recording_path),
                "STUB_MENU_TRANSCRIBE_ARGV": str(self.transcribe_log),
            }
        )
        return run_pty(
            ["/bin/bash", str(self.bin_dir / "meeting")], keys, environment
        )

    def test_normal_stop_offers_transcription(self) -> None:
        returncode, _, stderr = self._run_menu("stopped", b"1\n\ny\n")

        self.assertEqual(returncode, 0, stderr)
        self.assertIn("Transcribe this recording now?", stderr)
        arguments = [
            item.decode()
            for item in self.transcribe_log.read_bytes().split(b"\0")
            if item
        ]
        self.assertEqual(arguments, [str(self.recording_path)])

    def test_interrupted_capture_reports_the_saved_path_without_upload(self) -> None:
        returncode, _, stderr = self._run_menu("interrupted", b"1\n\n7\n")

        self.assertEqual(returncode, 0, stderr)
        self.assertNotIn("Transcribe this recording now?", stderr)
        self.assertIn(str(self.recording_path), stderr)
        self.assertFalse(self.transcribe_log.exists())

    def test_menu_offers_only_the_canonical_surface(self) -> None:
        returncode, _, stderr = self._run_menu("stopped", b"7\n")

        self.assertEqual(returncode, 0, stderr)
        for entry in (
            "1  Record a meeting",
            "2  Transcribe the latest recording",
            "3  Open the latest transcript",
            "4  Name or correct speakers",
            "5  Open the meeting folder",
            "6  Check setup",
            "7  Exit",
        ):
            self.assertIn(entry, stderr)
        self.assertNotIn("cleanup", stderr)

    def test_failed_capture_never_offers_transcription(self) -> None:
        returncode, _, stderr = self._run_menu("fail", b"1\n\n7\n")

        self.assertEqual(returncode, 0, stderr)
        self.assertNotIn("Transcribe this recording now?", stderr)
        self.assertFalse(self.transcribe_log.exists())


if __name__ == "__main__":
    unittest.main()
