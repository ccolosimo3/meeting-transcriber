from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import transcribe_assemblyai as assemblyai  # noqa: E402
import transcript_run  # noqa: E402


def completed_response() -> dict[str, object]:
    return {
        "id": "job-123",
        "status": "completed",
        "speech_model_used": "universal-3-5-pro",
        "language_code": "en",
        "audio_url": "https://cdn.example/private-upload-token",
        "text": "First. Second exactly as returned.",
        "utterances": [
            {
                "speaker": "B",
                "start": 100,
                "end": 900,
                "text": "First.",
                "words": [
                    {
                        "speaker": "B",
                        "start": 100,
                        "end": 900,
                        "text": "First.",
                        "confidence": 0.98,
                    }
                ],
            },
            {
                "speaker": "A",
                "start": 1100,
                "end": 2600,
                "text": "Second exactly as returned.",
                "words": [
                    {
                        "speaker": "A",
                        "start": 1100,
                        "end": 1600,
                        "text": "Second",
                        "confidence": 0.91,
                    },
                    {
                        "speaker": "A",
                        "start": 1600,
                        "end": 2600,
                        "text": "exactly as returned.",
                        "confidence": 0.93,
                    },
                ],
            },
        ],
    }


class ScenarioServer(ThreadingHTTPServer):
    def __init__(self, scenario: dict[str, object]):
        super().__init__(("127.0.0.1", 0), ScenarioHandler)
        self.scenario = scenario
        self.requests: list[dict[str, object]] = []


class ScenarioHandler(BaseHTTPRequestHandler):
    server: ScenarioServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _record(self, body: bytes = b"") -> None:
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_length": self.headers.get("Content-Length"),
                "body": body,
            }
        )

    def _respond(self, status: int, value: object | None = None) -> None:
        payload = b"" if value is None else json.dumps(value).encode("utf-8")
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        if self.path == "/v2/upload":
            status = int(self.server.scenario.get("upload_status", 200))
            self._respond(status, {"upload_url": "https://upload.invalid/audio"})
            return
        status = int(self.server.scenario.get("submit_status", 200))
        self._respond(status, {"id": "job-123", "status": "queued"})

    def do_GET(self) -> None:
        self._record()
        responses = self.server.scenario.setdefault(
            "poll_responses", [completed_response()]
        )
        assert isinstance(responses, list)
        value = responses.pop(0)
        if isinstance(value, int):
            self._respond(value, {"error": "transient"})
        else:
            self._respond(200, value)

    def do_DELETE(self) -> None:
        self._record()
        statuses = self.server.scenario.setdefault("delete_statuses", [200])
        assert isinstance(statuses, list)
        status = int(statuses.pop(0) if statuses else 200)
        self._respond(status, {} if status < 300 else {"error": "delete failed"})


@contextmanager
def running_server(scenario: dict[str, object]):
    server = ScenarioServer(scenario)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_published_run(
    run_dir: Path,
    *,
    deletion: dict[str, object] | None,
    receipt_overrides: dict[str, object] | None = None,
) -> Path:
    """Create one new-schema published run fixture and return transcript.json."""
    run_dir.mkdir(parents=True)
    canonical = {
        "language": "en",
        "text": "First.",
        "segments": [
            {
                "id": 0,
                "start": 0.1,
                "end": 0.9,
                "text": "First.",
                "speaker": "SPEAKER_00",
                "words": [
                    {
                        "word": "First.",
                        "start": 0.1,
                        "end": 0.9,
                        "probability": 0.98,
                        "speaker": "SPEAKER_00",
                    }
                ],
            }
        ],
        "speaker_names": {},
    }
    json_path = run_dir / "transcript.json"
    json_path.write_text(json.dumps(canonical), encoding="utf-8")
    (run_dir / "transcript.md").write_text("# Meeting\n", encoding="utf-8")
    (run_dir / "transcript.html").write_text("<!doctype html>\n", encoding="utf-8")
    receipt: dict[str, object] = {
        "provider": "assemblyai",
        "requested_model": "universal-3-5-pro",
        "used_model": "universal-3-5-pro",
        "transcript_id": "job-123",
        "state": "published",
        "provider_status": "completed",
        "timestamps": {
            "created_at": "2026-08-26T10:00:00Z",
            "submitted_at": "2026-08-26T10:01:00Z",
            "completed_at": "2026-08-26T10:02:00Z",
            "published_at": "2026-08-26T10:02:01Z",
        },
    }
    if deletion is not None:
        receipt["deletion"] = deletion
    if receipt_overrides:
        receipt.update(receipt_overrides)
    (run_dir / ".assemblyai.json").write_text(json.dumps(receipt), encoding="utf-8")
    return json_path


class AssemblyAIAdapterTests(unittest.TestCase):
    def run_adapter(
        self,
        scenario: dict[str, object],
        *,
        speakers: int | None = None,
        audio_bytes: bytes = b"synthetic audio",
        adapter_type: type[assemblyai.AssemblyAIAdapter] = assemblyai.AssemblyAIAdapter,
    ) -> tuple[Path, bool, dict[str, object], list[dict[str, object]], str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        audio = root / "meeting.wav"
        audio.write_bytes(audio_bytes)
        output = root / "20260826-100000-adapter" / "transcripts" / "20260826-100100"
        output.mkdir(parents=True)
        stderr = io.StringIO()
        with running_server(scenario) as (server, base_url):
            adapter = adapter_type(
                api_key="test-key",
                base_url=base_url,
                poll_interval=0,
                transient_attempts=3,
                stderr=stderr,
            )
            _, confirmed = adapter.run(audio, output, "transcript", speakers)
            requests = list(server.requests)
        receipt = json.loads((output / ".assemblyai.json").read_text(encoding="utf-8"))
        return output, confirmed, receipt, requests, stderr.getvalue()

    def test_real_http_lifecycle_streams_upload_and_publishes_canonical_bundle(self) -> None:
        source_bytes = b"a" * (assemblyai.UPLOAD_CHUNK_BYTES + 37)
        scenario = {
            "poll_responses": [
                {"id": "job-123", "status": "queued"},
                {"id": "job-123", "status": "processing"},
                completed_response(),
            ]
        }
        output, confirmed, receipt, requests, progress = self.run_adapter(
            scenario, speakers=3, audio_bytes=source_bytes
        )

        self.assertTrue(confirmed)
        self.assertEqual(
            [request["method"] for request in requests],
            ["POST", "POST", "GET", "GET", "GET", "DELETE"],
        )
        self.assertEqual(requests[0]["body"], source_bytes)
        self.assertEqual(requests[0]["content_length"], str(len(source_bytes)))
        self.assertTrue(
            all(request["authorization"] == "test-key" for request in requests)
        )
        request_body = json.loads(bytes(requests[1]["body"]))
        self.assertEqual(
            request_body,
            {
                "audio_url": "https://upload.invalid/audio",
                "speech_models": ["universal-3-5-pro"],
                "speaker_labels": True,
                "punctuate": True,
                "format_text": True,
                "language_detection": True,
                "speakers_expected": 3,
            },
        )
        inventory = sorted(path.name for path in output.iterdir())
        self.assertEqual(
            inventory,
            [".assemblyai.json", "transcript.html", "transcript.json", "transcript.md"],
        )
        canonical = json.loads((output / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(canonical["text"], "First. Second exactly as returned.")
        self.assertEqual(canonical["language"], "en")
        self.assertEqual(canonical["speaker_names"], {})
        self.assertEqual([segment["id"] for segment in canonical["segments"]], [0, 1])
        self.assertEqual(
            [segment["speaker"] for segment in canonical["segments"]],
            ["SPEAKER_00", "SPEAKER_01"],
        )
        self.assertEqual(canonical["segments"][1]["words"][0]["word"], "Second")
        self.assertEqual(canonical["segments"][1]["words"][0]["probability"], 0.91)

        markdown = (output / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("# Adapter", markdown)
        self.assertIn("SPEAKER_00", markdown)
        html = (output / "transcript.html").read_text(encoding="utf-8")
        self.assertIn("Generated by Meeting Transcriber", html)
        self.assertNotIn("Generated locally", html)

        self.assertEqual(
            sorted(receipt),
            [
                "deletion",
                "provider",
                "provider_status",
                "requested_model",
                "state",
                "timestamps",
                "transcript_id",
                "used_model",
            ],
        )
        self.assertEqual(receipt["state"], "published")
        self.assertEqual(receipt["transcript_id"], "job-123")
        self.assertEqual(receipt["used_model"], "universal-3-5-pro")
        self.assertEqual(receipt["provider_status"], "completed")
        self.assertTrue(receipt["deletion"]["confirmed"])
        self.assertIn("submitted_at", receipt["timestamps"])
        self.assertIn("completed_at", receipt["timestamps"])
        serialized = json.dumps(receipt)
        self.assertNotIn("test-key", serialized)
        self.assertNotIn("private-upload-token", serialized)
        self.assertNotIn("upload.invalid", serialized)
        self.assertNotIn("First.", serialized)
        for name in (".assemblyai.json", "transcript.json", "transcript.md", "transcript.html"):
            self.assertEqual((output / name).stat().st_mode & 0o777, 0o600, name)
        self.assertIn("Uploading recording...", progress)
        self.assertIn("Transcribing and identifying speakers...", progress)
        self.assertIn("Deleting remote transcript and audio...", progress)
        self.assertIn("done", progress)

    def test_speaker_count_is_omitted_when_unknown(self) -> None:
        _, _, _, requests, _ = self.run_adapter({})
        request_body = json.loads(bytes(requests[1]["body"]))
        self.assertNotIn("speakers_expected", request_body)

    def test_terminal_and_publication_failures_cleanup_known_job(self) -> None:
        failures: list[tuple[str, dict[str, object], str]] = []
        terminal = completed_response()
        terminal.update({"status": "error", "error": "bad audio"})
        failures.append(("terminal", {"poll_responses": [terminal]}, "provider_error"))
        mismatch = completed_response()
        mismatch["speech_model_used"] = "universal-2"
        failures.append(("model", {"poll_responses": [mismatch]}, "publication_failed"))
        malformed = completed_response()
        malformed["utterances"] = []
        failures.append(("malformed", {"poll_responses": [malformed]}, "publication_failed"))

        for name, scenario, expected_state in failures:
            with self.subTest(name=name):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                audio = root / "meeting.wav"
                audio.write_bytes(b"audio")
                output = root / "output"
                output.mkdir()
                with running_server(scenario) as (server, base_url):
                    adapter = assemblyai.AssemblyAIAdapter(
                        api_key="test-key",
                        base_url=base_url,
                        poll_interval=0,
                        stderr=io.StringIO(),
                    )
                    with self.assertRaises((assemblyai.ProviderError, ValueError)):
                        adapter.run(audio, output, "transcript", None)
                    methods = [request["method"] for request in server.requests]
                receipt = json.loads(
                    (output / ".assemblyai.json").read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["state"], expected_state)
                self.assertEqual(receipt["deletion"]["confirmed"], True)
                self.assertEqual(methods[-1], "DELETE")
                self.assertFalse((output / "transcript.json").exists())
                self.assertNotIn("provider_response", receipt)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "meeting.wav"
            audio.write_bytes(b"audio")
            output = root / "output"
            output.mkdir()
            with running_server({}) as (server, base_url):
                adapter = assemblyai.AssemblyAIAdapter(
                    api_key="test-key",
                    base_url=base_url,
                    poll_interval=0,
                    stderr=io.StringIO(),
                )
                with mock.patch.object(
                    assemblyai, "save_canonical", side_effect=OSError("disk full")
                ), self.assertRaises(OSError):
                    adapter.run(audio, output, "transcript", None)
                self.assertEqual(server.requests[-1]["method"], "DELETE")
            receipt = json.loads((output / ".assemblyai.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["state"], "publication_failed")
            self.assertTrue(receipt["deletion"]["confirmed"])

    def test_pre_id_failures_stop_without_retry_or_deletion(self) -> None:
        for name, scenario, expected_state, expected_methods in (
            ("upload", {"upload_status": 400}, "upload_rejected", ["POST"]),
            (
                "submit",
                {"submit_status": 400},
                "upload_succeeded",
                ["POST", "POST"],
            ),
            (
                "upload-5xx",
                {"upload_status": 503},
                "unknown_after_upload",
                ["POST"],
            ),
            (
                "submit-5xx",
                {"submit_status": 503},
                "unknown_after_submit",
                ["POST", "POST"],
            ),
        ):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    audio = root / "meeting.wav"
                    audio.write_bytes(b"audio")
                    output = root / "output"
                    output.mkdir()
                    with running_server(scenario) as (server, base_url):
                        adapter = assemblyai.AssemblyAIAdapter(
                            api_key="test-key",
                            base_url=base_url,
                            poll_interval=0,
                            stderr=io.StringIO(),
                        )
                        with self.assertRaises(assemblyai.ProviderError):
                            adapter.run(audio, output, "transcript", None)
                        methods = [request["method"] for request in server.requests]
                    receipt = json.loads(
                        (output / ".assemblyai.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(receipt["state"], expected_state)
                    self.assertEqual(methods, expected_methods)
                    self.assertNotIn("deletion", receipt)

    def test_ambiguous_pre_id_failures_persist_residual_risk(self) -> None:
        class UploadAmbiguous(assemblyai.AssemblyAIAdapter):
            def _upload(self, audio: Path) -> dict[str, object]:
                raise assemblyai.ProviderTransportError("connection lost")

        class SubmitAmbiguous(assemblyai.AssemblyAIAdapter):
            def _upload(self, audio: Path) -> dict[str, object]:
                return {"upload_url": "https://upload.invalid/audio"}

            def _json_request(
                self,
                method: str,
                suffix: str,
                operation: str,
                body: dict[str, object] | None = None,
            ) -> dict[str, object]:
                raise assemblyai.ProviderTransportError("response lost")

        for adapter_type, expected_state in (
            (UploadAmbiguous, "unknown_after_upload"),
            (SubmitAmbiguous, "unknown_after_submit"),
        ):
            with self.subTest(state=expected_state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                audio = root / "meeting.wav"
                audio.write_bytes(b"audio")
                output = root / "output"
                output.mkdir()
                adapter = adapter_type(
                    api_key="test-key",
                    base_url="http://127.0.0.1:1",
                    poll_interval=0,
                    stderr=io.StringIO(),
                )
                with self.assertRaises(assemblyai.ProviderTransportError):
                    adapter.run(audio, output, "transcript", None)
                receipt = json.loads(
                    (output / ".assemblyai.json").read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["state"], expected_state)
                self.assertNotIn("deletion", receipt)

    def test_poll_retry_and_delete_failure_preserve_local_result(self) -> None:
        scenario = {
            "poll_responses": [503, completed_response()],
            "delete_statuses": [503, 503, 503],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "meeting.wav"
            audio.write_bytes(b"audio")
            output = root / "output"
            output.mkdir()
            stderr = io.StringIO()
            with running_server(scenario) as (server, base_url):
                adapter = assemblyai.AssemblyAIAdapter(
                    api_key="test-key",
                    base_url=base_url,
                    poll_interval=0,
                    stderr=stderr,
                )
                canonical_path, confirmed = adapter.run(audio, output, "transcript", None)
                methods = [request["method"] for request in server.requests]
            self.assertFalse(confirmed)
            self.assertEqual(canonical_path, output / "transcript.json")
            self.assertEqual(methods.count("GET"), 2)
            self.assertEqual(methods.count("DELETE"), 3)
            self.assertTrue((output / "transcript.json").is_file())
            receipt = json.loads((output / ".assemblyai.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["state"], "published")
            self.assertFalse(receipt["deletion"]["confirmed"])
            self.assertEqual(receipt["deletion"]["attempts"], 3)
            self.assertIn("last_error", receipt["deletion"])
            self.assertIn("failed", stderr.getvalue())

        exhausted_scenario = {
            "poll_responses": [
                {"id": "job-123", "status": "processing"},
                503,
                503,
                503,
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "meeting.wav"
            audio.write_bytes(b"audio")
            output = root / "output"
            output.mkdir()
            with running_server(exhausted_scenario) as (server, base_url):
                adapter = assemblyai.AssemblyAIAdapter(
                    api_key="test-key",
                    base_url=base_url,
                    poll_interval=0,
                    stderr=io.StringIO(),
                )
                with self.assertRaises(assemblyai.ProviderHTTPError):
                    adapter.run(audio, output, "transcript", None)
                methods = [request["method"] for request in server.requests]
            receipt = json.loads((output / ".assemblyai.json").read_text(encoding="utf-8"))
            self.assertEqual(methods.count("GET"), 4)
            self.assertEqual(methods[-1], "DELETE")
            self.assertEqual(receipt["state"], "poll_failed")
            self.assertEqual(receipt["provider_status"], "processing")
            self.assertTrue(receipt["deletion"]["confirmed"])

    def test_interrupt_after_submission_attempts_cleanup_and_persists_id(self) -> None:
        class InterruptedAdapter(assemblyai.AssemblyAIAdapter):
            def _poll(self) -> dict[str, object]:
                raise KeyboardInterrupt

        scenario = {"delete_statuses": [503, 503, 503]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "meeting.wav"
            audio.write_bytes(b"audio")
            output = root / "output"
            output.mkdir()
            with running_server(scenario) as (server, base_url):
                adapter = InterruptedAdapter(
                    api_key="test-key",
                    base_url=base_url,
                    poll_interval=0,
                    stderr=io.StringIO(),
                )
                with self.assertRaises(KeyboardInterrupt):
                    adapter.run(audio, output, "transcript", None)
                self.assertEqual(
                    [request["method"] for request in server.requests].count("DELETE"),
                    3,
                )
            receipt = json.loads((output / ".assemblyai.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["state"], "interrupted")
            self.assertEqual(receipt["transcript_id"], "job-123")
            self.assertFalse(receipt["deletion"]["confirmed"])

    def _run_main(
        self, scenario: dict[str, object], base_env: dict[str, str] | None = None
    ) -> tuple[int, str, str, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        audio = root / "meeting.wav"
        audio.write_bytes(b"audio")
        output = root / "output"
        output.mkdir()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with running_server(scenario) as (_, base_url):
            environment = {
                "ASSEMBLYAI_API_KEY": "test-key",
                "MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE": base_url,
                "MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL": "0",
            }
            environment.update(base_env or {})
            with (
                mock.patch.dict("os.environ", environment),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "transcribe_assemblyai.py",
                        "--input",
                        str(audio),
                        "--output-dir",
                        str(output),
                        "--output-name",
                        "transcript",
                    ],
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = assemblyai.main()
        return status, stdout.getvalue(), stderr.getvalue(), output

    def _action_rows(self, stderr: str) -> dict[str, int]:
        return {
            "retry": sum(1 for line in stderr.splitlines() if line.startswith("  Retry ")),
            "action": sum(1 for line in stderr.splitlines() if line.startswith("  Action ")),
            "cleanup": sum(1 for line in stderr.splitlines() if line.startswith("  Cleanup ")),
        }

    def test_failure_blocks_emit_exactly_one_recommended_action(self) -> None:
        terminal_error = completed_response()
        terminal_error.update({"status": "error", "error": "bad audio"})
        mismatch = completed_response()
        mismatch["speech_model_used"] = "universal-2"

        cases: list[tuple[str, dict[str, object], int, str]] = [
            ("upload-rejected", {"upload_status": 400}, 1, "retry"),
            ("upload-ambiguous", {"upload_status": 503}, 1, "action"),
            ("submit-rejected", {"submit_status": 400}, 1, "action"),
            ("provider-error-clean", {"poll_responses": [terminal_error]}, 1, "retry"),
            (
                "provider-error-unconfirmed",
                {
                    "poll_responses": [dict(terminal_error)],
                    "delete_statuses": [404, 404, 404],
                },
                1,
                "action",
            ),
            ("model-mismatch", {"poll_responses": [mismatch]}, 1, "action"),
        ]
        for name, scenario, expected_status, expected_action in cases:
            with self.subTest(name=name):
                if name == "provider-error-unconfirmed":
                    scenario["poll_responses"] = [dict(terminal_error)]
                status, stdout, stderr, _ = self._run_main(dict(scenario))
                self.assertEqual(status, expected_status, stderr)
                self.assertEqual(stdout, "")
                self.assertIn("Transcription stopped", stderr)
                self.assertIn("Recording   safe at", stderr)
                rows = self._action_rows(stderr)
                self.assertEqual(sum(rows.values()), 1, stderr)
                self.assertEqual(rows[expected_action], 1, stderr)
                self.assertNotIn("--local", stderr)

        with self.subTest(name="model-mismatch-content"):
            mismatch_again = completed_response()
            mismatch_again["speech_model_used"] = "universal-2"
            status, _, stderr, _ = self._run_main(
                {"poll_responses": [mismatch_again]}
            )
            self.assertEqual(status, 1)
            self.assertIn("universal-2", stderr)
            self.assertIn(assemblyai.SUPPORT_ACTION, stderr)
            self.assertNotIn("Retry", stderr)

    def test_cleanup_required_publication_exits_3_with_single_stdout_path(self) -> None:
        scenario = {"delete_statuses": [503, 503, 503]}
        status, stdout, stderr, output = self._run_main(scenario)
        resolved_json = output.resolve() / "transcript.json"
        self.assertEqual(status, 3)
        self.assertEqual(stdout.strip(), str(resolved_json))
        self.assertEqual(len(stdout.strip().splitlines()), 1)
        self.assertIn("Transcript saved; cleanup required", stderr)
        self.assertIn(f"meeting cleanup {resolved_json}", stderr)
        self.assertIn("Read", stderr)
        self.assertIn("Agent", stderr)
        self.assertIn("Data", stderr)
        rows = self._action_rows(stderr)
        self.assertEqual(rows["cleanup"], 1)
        self.assertEqual(rows["retry"], 0)
        self.assertEqual(rows["action"], 0)

    def test_interrupt_during_final_deletion_is_cleanup_required_not_failure(self) -> None:
        class DeleteInterrupted(assemblyai.AssemblyAIAdapter):
            def _json_request(
                self,
                method: str,
                suffix: str,
                operation: str,
                body: dict[str, object] | None = None,
            ) -> dict[str, object]:
                if method == "DELETE":
                    raise KeyboardInterrupt
                return super()._json_request(method, suffix, operation, body)

        with mock.patch.object(assemblyai, "AssemblyAIAdapter", DeleteInterrupted):
            status, stdout, stderr, output = self._run_main({})

        resolved_json = output.resolve() / "transcript.json"
        self.assertEqual(status, 3, stderr)
        self.assertEqual(stdout.strip().splitlines(), [str(resolved_json)])
        self.assertIn("Transcript saved; cleanup required", stderr)
        self.assertIn(f"meeting cleanup {resolved_json}", stderr)
        self.assertIn("Read", stderr)
        self.assertIn("Agent", stderr)
        self.assertIn("Data", stderr)
        rows = self._action_rows(stderr)
        self.assertEqual(rows, {"retry": 0, "action": 0, "cleanup": 1}, stderr)
        for name in ("transcript.json", "transcript.md", "transcript.html"):
            self.assertTrue((output / name).is_file(), name)
        receipt = json.loads((output / ".assemblyai.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "published")
        self.assertFalse(receipt["deletion"]["confirmed"])
        self.assertIn("KeyboardInterrupt", receipt["deletion"]["last_error"])
        classification = transcript_run.classify(output.resolve())
        self.assertIsNotNone(classification)
        assert classification is not None
        self.assertEqual(classification[0], "cleanup-required")

    def _cleanup_adapter(
        self, json_path: Path, base_url: str, stderr: io.StringIO
    ) -> assemblyai.AssemblyAIAdapter:
        receipt_path, receipt, transcript_id = assemblyai.resolve_cleanup_target(json_path)
        adapter = assemblyai.AssemblyAIAdapter(
            api_key="test-key", base_url=base_url, poll_interval=0, stderr=stderr
        )
        adapter.receipt_path = receipt_path
        adapter.receipt = receipt
        adapter.transcript_id = transcript_id
        return adapter

    def test_cleanup_confirms_compacts_and_clears_the_warning_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "20260826-100000-m" / "transcripts" / "20260826-100100"
            json_path = write_published_run(
                run_dir, deletion={"confirmed": False, "attempts": 3, "last_error": "boom"}
            )
            hashes_before = {
                name: file_sha(run_dir / name)
                for name in ("transcript.json", "transcript.md", "transcript.html")
            }
            self.assertEqual(
                transcript_run.classify(run_dir),
                ("cleanup-required", json_path, "job-123"),
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with running_server({"delete_statuses": [200]}) as (server, base_url):
                adapter = self._cleanup_adapter(json_path, base_url, stderr)
                with redirect_stdout(stdout):
                    status = adapter.cleanup(json_path)
                methods = [request["method"] for request in server.requests]
            self.assertEqual(status, 0)
            self.assertEqual(methods, ["DELETE"])
            self.assertEqual(stdout.getvalue().strip(), str(json_path))
            self.assertIn("Remote cleanup complete", stderr.getvalue())
            receipt = json.loads((run_dir / ".assemblyai.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["deletion"]["confirmed"])
            self.assertIn("confirmed_at", receipt["deletion"])
            self.assertNotIn("confirmed_via", receipt["deletion"])
            self.assertNotIn("last_error", receipt["deletion"])
            self.assertEqual(receipt["state"], "published")
            self.assertNotIn("test-key", json.dumps(receipt))
            for name, sha in hashes_before.items():
                self.assertEqual(file_sha(run_dir / name), sha, name)
            self.assertEqual(transcript_run.classify(run_dir), ("clean", json_path, None))

    def test_cleanup_handles_missing_deletion_field_as_cleanup_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "b" / "transcripts" / "r"
            json_path = write_published_run(run_dir, deletion=None)
            self.assertEqual(
                transcript_run.classify(run_dir),
                ("cleanup-required", json_path, "job-123"),
            )
            with running_server({"delete_statuses": [200]}) as (_, base_url):
                adapter = self._cleanup_adapter(json_path, base_url, io.StringIO())
                with redirect_stdout(io.StringIO()):
                    status = adapter.cleanup(json_path)
            self.assertEqual(status, 0)
            self.assertEqual(transcript_run.classify(run_dir), ("clean", json_path, None))

    def test_cleanup_404_confirms_only_with_lifecycle_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "q" / "transcripts" / "r1"
            json_path = write_published_run(run_dir, deletion={"confirmed": False})
            with running_server({"delete_statuses": [404]}) as (server, base_url):
                adapter = self._cleanup_adapter(json_path, base_url, io.StringIO())
                with redirect_stdout(io.StringIO()):
                    status = adapter.cleanup(json_path)
                self.assertEqual([r["method"] for r in server.requests], ["DELETE"])
            self.assertEqual(status, 0)
            receipt = json.loads((run_dir / ".assemblyai.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["deletion"]["confirmed"])
            self.assertEqual(receipt["deletion"]["confirmed_via"], "absent_after_delete")

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "q" / "transcripts" / "r2"
            json_path = write_published_run(
                run_dir,
                deletion={"confirmed": False},
                receipt_overrides={
                    "timestamps": {"created_at": "2026-08-26T10:00:00Z"}
                },
            )
            receipt_before = file_sha(run_dir / ".assemblyai.json")
            with running_server({"delete_statuses": [404]}) as (server, _):
                with self.assertRaisesRegex(
                    assemblyai.CleanupTargetError, "timestamps are incomplete"
                ):
                    assemblyai.resolve_cleanup_target(json_path)
                self.assertEqual(server.requests, [])
            self.assertEqual(file_sha(run_dir / ".assemblyai.json"), receipt_before)

    def test_cleanup_already_clean_is_idempotent_without_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "c" / "transcripts" / "r"
            json_path = write_published_run(
                run_dir,
                deletion={"confirmed": True, "confirmed_at": "2026-08-26T10:03:00Z"},
            )
            receipt_before = file_sha(run_dir / ".assemblyai.json")
            stdout = io.StringIO()
            with running_server({}) as (server, base_url):
                adapter = self._cleanup_adapter(json_path, base_url, io.StringIO())
                with redirect_stdout(stdout):
                    status = adapter.cleanup(json_path)
                self.assertEqual(server.requests, [])
            self.assertEqual(status, 0)
            self.assertEqual(stdout.getvalue().strip(), str(json_path))
            self.assertEqual(file_sha(run_dir / ".assemblyai.json"), receipt_before)

    def test_cleanup_keeps_truthful_false_state_after_exhausted_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "e" / "transcripts" / "r"
            json_path = write_published_run(run_dir, deletion={"confirmed": False})
            with running_server({"delete_statuses": [503, 503, 503]}) as (server, base_url):
                adapter = self._cleanup_adapter(json_path, base_url, io.StringIO())
                with redirect_stdout(io.StringIO()):
                    status = adapter.cleanup(json_path)
                self.assertEqual(
                    [r["method"] for r in server.requests], ["DELETE", "DELETE", "DELETE"]
                )
            self.assertEqual(status, 1)
            receipt = json.loads((run_dir / ".assemblyai.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["deletion"]["confirmed"])
            self.assertEqual(receipt["deletion"]["attempts"], 3)

    def test_cleanup_rejects_invalid_targets_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            missing = root / "missing" / "transcript.json"
            with self.assertRaises(assemblyai.CleanupTargetError):
                assemblyai.resolve_cleanup_target(missing)

            no_receipt = root / "no-receipt"
            no_receipt.mkdir()
            (no_receipt / "transcript.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(assemblyai.CleanupTargetError):
                assemblyai.resolve_cleanup_target(no_receipt / "transcript.json")

            historical = root / "historical"
            historical.mkdir()
            (historical / "transcript.json").write_text("{}", encoding="utf-8")
            (historical / "transcript.assemblyai.json").write_text(
                json.dumps({"state": "published", "transcript_id": "old"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                assemblyai.CleanupTargetError, "not managed by meeting cleanup"
            ):
                assemblyai.resolve_cleanup_target(historical / "transcript.json")

            malformed = root / "malformed"
            malformed.mkdir()
            (malformed / "transcript.json").write_text("{}", encoding="utf-8")
            (malformed / ".assemblyai.json").write_text("not json", encoding="utf-8")
            with self.assertRaises(assemblyai.CleanupTargetError):
                assemblyai.resolve_cleanup_target(malformed / "transcript.json")

            pre_publication = root / "pre" / "transcripts" / "r"
            write_published_run(
                pre_publication,
                deletion=None,
                receipt_overrides={"state": "interrupted"},
            )
            with self.assertRaisesRegex(
                assemblyai.CleanupTargetError, "never published"
            ):
                assemblyai.resolve_cleanup_target(pre_publication / "transcript.json")

            no_id = root / "noid" / "transcripts" / "r"
            write_published_run(
                no_id, deletion=None, receipt_overrides={"transcript_id": ""}
            )
            with self.assertRaisesRegex(assemblyai.CleanupTargetError, "transcript ID"):
                assemblyai.resolve_cleanup_target(no_id / "transcript.json")

            crafted = root / "crafted" / "transcripts" / "r"
            crafted_json = write_published_run(
                crafted,
                deletion={"confirmed": False},
                receipt_overrides={"provider": "other-provider"},
            )
            crafted_receipt_sha = file_sha(crafted / ".assemblyai.json")
            with self.assertRaisesRegex(assemblyai.CleanupTargetError, "provider"):
                assemblyai.resolve_cleanup_target(crafted_json)
            self.assertEqual(file_sha(crafted / ".assemblyai.json"), crafted_receipt_sha)

            crafted_id = root / "crafted-id" / "transcripts" / "r"
            crafted_id_json = write_published_run(
                crafted_id,
                deletion={"confirmed": False},
                receipt_overrides={"transcript_id": "../upload"},
            )
            with self.assertRaisesRegex(assemblyai.CleanupTargetError, "transcript ID"):
                assemblyai.resolve_cleanup_target(crafted_id_json)

            extra = root / "extra" / "transcripts" / "r"
            extra_json = write_published_run(extra, deletion={"confirmed": False})
            (extra / "crafted.txt").write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(assemblyai.CleanupTargetError, "inventory"):
                assemblyai.resolve_cleanup_target(extra_json)

            linked = root / "linked-transcript.json"
            linked.symlink_to(crafted_json)
            with self.assertRaises(assemblyai.CleanupTargetError):
                assemblyai.resolve_cleanup_target(linked)

            receipt_link_run = root / "receipt-link" / "transcripts" / "r"
            receipt_link_json = write_published_run(
                receipt_link_run, deletion={"confirmed": False}
            )
            real_receipt = root / "real-receipt.json"
            (receipt_link_run / ".assemblyai.json").replace(real_receipt)
            (receipt_link_run / ".assemblyai.json").symlink_to(real_receipt)
            with self.assertRaises(assemblyai.CleanupTargetError):
                assemblyai.resolve_cleanup_target(receipt_link_json)


if __name__ == "__main__":
    unittest.main()
