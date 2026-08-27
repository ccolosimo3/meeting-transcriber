from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def completed_response() -> dict[str, object]:
    return {
        "id": "job-123",
        "status": "completed",
        "speech_model_used": "universal-3-5-pro",
        "language_code": "en",
        "audio_url": "https://cdn.example/private-upload-token",
        "text": "First.Second exactly as returned.",
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


class AssemblyAIAdapterTests(unittest.TestCase):
    def run_adapter(
        self,
        scenario: dict[str, object],
        *,
        speakers: int | None = None,
        audio_bytes: bytes = b"synthetic audio",
        adapter_type: type[assemblyai.AssemblyAIAdapter] = assemblyai.AssemblyAIAdapter,
    ) -> tuple[Path, dict[str, object], list[dict[str, object]], str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        audio = root / "meeting.wav"
        audio.write_bytes(audio_bytes)
        output = root / "output"
        output.mkdir()
        stderr = io.StringIO()
        with running_server(scenario) as (server, base_url):
            adapter = adapter_type(
                api_key="test-key",
                base_url=base_url,
                poll_interval=0,
                transient_attempts=3,
                stderr=stderr,
            )
            adapter.run(audio, output, "transcript", speakers)
            requests = list(server.requests)
        receipt = json.loads(
            (output / "transcript.assemblyai.json").read_text(encoding="utf-8")
        )
        return output, receipt, requests, stderr.getvalue()

    def test_real_http_lifecycle_streams_upload_and_publishes_canonical_bundle(self) -> None:
        source_bytes = b"a" * (assemblyai.UPLOAD_CHUNK_BYTES + 37)
        scenario = {
            "poll_responses": [
                {"id": "job-123", "status": "queued"},
                {"id": "job-123", "status": "processing"},
                completed_response(),
            ]
        }
        output, receipt, requests, progress = self.run_adapter(
            scenario, speakers=3, audio_bytes=source_bytes
        )

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
        canonical = json.loads((output / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(canonical["text"], "First.Second exactly as returned.")
        self.assertEqual(canonical["language"], "en")
        self.assertEqual([segment["id"] for segment in canonical["segments"]], [0, 1])
        self.assertEqual(
            [segment["speaker"] for segment in canonical["segments"]],
            ["SPEAKER_00", "SPEAKER_01"],
        )
        self.assertEqual(canonical["segments"][1]["words"][0]["word"], "Second")
        self.assertEqual(canonical["segments"][1]["words"][0]["probability"], 0.91)
        self.assertEqual(receipt["state"], "published")
        self.assertEqual(receipt["transcript_id"], "job-123")
        self.assertEqual(receipt["deletion"]["confirmed"], True)
        self.assertNotIn("test-key", json.dumps(receipt))
        self.assertNotIn("private-upload-token", json.dumps(receipt))
        self.assertEqual((output / "transcript.assemblyai.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("queued", progress)
        self.assertIn("processing", progress)
        self.assertIn("deleted", progress)

    def test_speaker_count_is_omitted_when_unknown(self) -> None:
        _, _, requests, _ = self.run_adapter({})
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
                        api_key="test-key", base_url=base_url, poll_interval=0
                    )
                    with self.assertRaises((assemblyai.ProviderError, ValueError)):
                        adapter.run(audio, output, "transcript", None)
                    methods = [request["method"] for request in server.requests]
                receipt = json.loads(
                    (output / "transcript.assemblyai.json").read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["state"], expected_state)
                self.assertEqual(receipt["deletion"]["confirmed"], True)
                self.assertEqual(methods[-1], "DELETE")
                self.assertFalse((output / "transcript.json").exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "meeting.wav"
            audio.write_bytes(b"audio")
            output = root / "output"
            output.mkdir()
            with running_server({}) as (server, base_url):
                adapter = assemblyai.AssemblyAIAdapter(
                    api_key="test-key", base_url=base_url, poll_interval=0
                )
                with mock.patch.object(
                    assemblyai, "write_outputs", side_effect=OSError("disk full")
                ), self.assertRaises(OSError):
                    adapter.run(audio, output, "transcript", None)
                self.assertEqual(server.requests[-1]["method"], "DELETE")
            receipt = json.loads(
                (output / "transcript.assemblyai.json").read_text(encoding="utf-8")
            )
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
                    stderr = io.StringIO()
                    with running_server(scenario) as (server, base_url):
                        adapter = assemblyai.AssemblyAIAdapter(
                            api_key="test-key",
                            base_url=base_url,
                            poll_interval=0,
                            stderr=stderr,
                        )
                        with self.assertRaises(assemblyai.ProviderError):
                            adapter.run(audio, output, "transcript", None)
                        methods = [request["method"] for request in server.requests]
                    receipt = json.loads(
                        (output / "transcript.assemblyai.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(receipt["state"], expected_state)
                    self.assertEqual(methods, expected_methods)
                    self.assertNotIn("deletion", receipt)
                    if name.endswith("5xx"):
                        self.assertIn("unknown", stderr.getvalue())

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

        for adapter_type, expected_state, warning in (
            (UploadAmbiguous, "unknown_after_upload", "partial remote audio"),
            (SubmitAmbiguous, "unknown_after_submit", "unknown billable job"),
        ):
            with self.subTest(state=expected_state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                audio = root / "meeting.wav"
                audio.write_bytes(b"audio")
                output = root / "output"
                output.mkdir()
                stderr = io.StringIO()
                adapter = adapter_type(
                    api_key="test-key",
                    base_url="http://127.0.0.1:1",
                    poll_interval=0,
                    stderr=stderr,
                )
                with self.assertRaises(assemblyai.ProviderTransportError):
                    adapter.run(audio, output, "transcript", None)
                receipt = json.loads(
                    (output / "transcript.assemblyai.json").read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["state"], expected_state)
                self.assertNotIn("deletion", receipt)
                self.assertIn(warning, stderr.getvalue())

    def test_poll_retry_and_delete_failure_preserve_local_result_and_job_warning(self) -> None:
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
                with self.assertRaises(assemblyai.ProviderError):
                    adapter.run(audio, output, "transcript", None)
                methods = [request["method"] for request in server.requests]
            self.assertEqual(methods.count("GET"), 2)
            self.assertEqual(methods.count("DELETE"), 3)
            self.assertTrue((output / "transcript.json").is_file())
            receipt = json.loads(
                (output / "transcript.assemblyai.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["state"], "published")
            self.assertFalse(receipt["deletion"]["confirmed"])
            self.assertIn("job-123", stderr.getvalue())
            self.assertIn("may remain remotely", stderr.getvalue())

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
                    api_key="test-key", base_url=base_url, poll_interval=0
                )
                with self.assertRaises(assemblyai.ProviderHTTPError):
                    adapter.run(audio, output, "transcript", None)
                methods = [request["method"] for request in server.requests]
            receipt = json.loads(
                (output / "transcript.assemblyai.json").read_text(encoding="utf-8")
            )
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
            stderr = io.StringIO()
            with running_server(scenario) as (server, base_url):
                adapter = InterruptedAdapter(
                    api_key="test-key",
                    base_url=base_url,
                    poll_interval=0,
                    stderr=stderr,
                )
                with self.assertRaises(KeyboardInterrupt):
                    adapter.run(audio, output, "transcript", None)
                self.assertEqual(
                    [request["method"] for request in server.requests].count("DELETE"),
                    3,
                )
            receipt = json.loads(
                (output / "transcript.assemblyai.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["state"], "interrupted")
            self.assertEqual(receipt["transcript_id"], "job-123")
            self.assertFalse(receipt["deletion"]["confirmed"])
            self.assertIn("job-123", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
