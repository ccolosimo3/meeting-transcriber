from __future__ import annotations

import argparse
import http.client
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from render_transcript_html import render_html
from render_transcript_markdown import render_markdown
from transcript_bundle import (
    CanonicalTranscript,
    CanonicalTranscriptError,
    Segment,
    TimedWord,
    save_canonical,
)
from transcript_run import ManagedRunError, validate_managed_run


API_BASE_URL = "https://api.assemblyai.com"
REQUESTED_MODEL = "universal-3-5-pro"
RECEIPT_NAME = ".assemblyai.json"
HISTORICAL_RECEIPT_NAME = "transcript.assemblyai.json"
UPLOAD_CHUNK_BYTES = 1024 * 1024
TRANSIENT_ATTEMPTS = 3
SUPPORT_ACTION = "Contact AssemblyAI support"

COMPACT_TIMESTAMP_KEYS = ("created_at", "submitted_at", "completed_at", "published_at")


class ProviderError(RuntimeError):
    pass


class ProviderHTTPError(ProviderError):
    def __init__(self, operation: str, status: int, detail: str):
        super().__init__(f"AssemblyAI {operation} failed with HTTP {status}: {detail}")
        self.status = status


class ProviderTransportError(ProviderError):
    pass


class ProviderProtocolError(ProviderError):
    pass


class ModelMismatchError(ValueError):
    def __init__(self, reported: object):
        super().__init__(
            f"AssemblyAI reported model {reported!r}, expected {REQUESTED_MODEL}"
        )
        self.reported = reported


class CleanupTargetError(RuntimeError):
    """The cleanup target is invalid; nothing was mutated."""


def utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remainder_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remainder_seconds:02d}"
    return f"{minutes:02d}:{remainder_seconds:02d}"


def atomic_write_json(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise
    with path.open(encoding="utf-8") as stream:
        loaded = json.load(stream)
    if not isinstance(loaded, dict):
        raise ValueError(f"provider receipt did not reload as an object: {path}")
    return loaded


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"AssemblyAI completion has invalid {field}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"AssemblyAI completion has invalid {field}")
    return number


def convert_response(response: dict[str, Any]) -> CanonicalTranscript:
    if response.get("speech_model_used") != REQUESTED_MODEL:
        raise ModelMismatchError(response.get("speech_model_used"))
    text = response.get("text")
    language = response.get("language_code")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("AssemblyAI completion has no top-level text")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("AssemblyAI completion has no language code")
    utterances = response.get("utterances")
    if not isinstance(utterances, list):
        raise ValueError("AssemblyAI completion has no utterance list")

    speaker_map: dict[str, str] = {}
    usable_utterances: list[dict[str, Any]] = []
    for utterance in utterances:
        if not isinstance(utterance, dict) or not str(utterance.get("text", "")).strip():
            continue
        provider_speaker = utterance.get("speaker")
        if not isinstance(provider_speaker, str) or not provider_speaker:
            raise ValueError("AssemblyAI utterance has no speaker label")
        if provider_speaker not in speaker_map:
            speaker_map[provider_speaker] = f"SPEAKER_{len(speaker_map):02d}"
        usable_utterances.append(utterance)
    if not usable_utterances:
        raise ValueError("AssemblyAI completion has no nonempty utterances")

    timed_word_count = 0
    segments: list[Segment] = []
    for index, utterance in enumerate(usable_utterances):
        start_ms = _finite_number(utterance.get("start"), "utterance start")
        end_ms = _finite_number(utterance.get("end"), "utterance end")
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("AssemblyAI completion has invalid utterance timestamps")
        provider_speaker = str(utterance["speaker"])
        words_value = utterance.get("words")
        if not isinstance(words_value, list):
            raise ValueError("AssemblyAI utterance has no word list")
        words: list[TimedWord] = []
        for provider_word in words_value:
            if not isinstance(provider_word, dict):
                raise ValueError("AssemblyAI completion has an invalid word")
            word_text = provider_word.get("text")
            if not isinstance(word_text, str) or not word_text:
                raise ValueError("AssemblyAI completion has a word without text")
            word_speaker = provider_word.get("speaker")
            if not isinstance(word_speaker, str):
                raise ValueError("AssemblyAI word has no speaker label")
            if word_speaker not in speaker_map:
                raise ValueError(
                    f"AssemblyAI word uses unknown speaker label: {word_speaker!r}"
                )
            word_start_ms = _finite_number(provider_word.get("start"), "word start")
            word_end_ms = _finite_number(provider_word.get("end"), "word end")
            confidence = _finite_number(provider_word.get("confidence"), "word confidence")
            if word_start_ms < 0 or word_end_ms < word_start_ms:
                raise ValueError("AssemblyAI completion has invalid word timestamps")
            words.append(
                TimedWord(
                    word=word_text,
                    start=word_start_ms / 1000,
                    end=word_end_ms / 1000,
                    probability=confidence,
                    speaker=speaker_map[str(word_speaker)],
                )
            )
            timed_word_count += 1
        segments.append(
            Segment(
                id=index,
                start=start_ms / 1000,
                end=end_ms / 1000,
                text=str(utterance["text"]),
                speaker=speaker_map[provider_speaker],
                words=words,
            )
        )
    if timed_word_count == 0:
        raise ValueError("AssemblyAI completion has no timed words")
    return CanonicalTranscript(
        language=language, text=text, segments=segments, speaker_names={}
    )


class AssemblyAIAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = API_BASE_URL,
        poll_interval: float = 3.0,
        transient_attempts: int = TRANSIENT_ATTEMPTS,
        stderr: Any = sys.stderr,
    ) -> None:
        if not api_key:
            raise ValueError("ASSEMBLYAI_API_KEY is required")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid AssemblyAI API base URL")
        self.api_key = api_key
        self.parsed_base = parsed
        self.poll_interval = poll_interval
        self.transient_attempts = transient_attempts
        self.stderr = stderr
        self.phase_started = time.monotonic()
        self.receipt_path: Path | None = None
        self.receipt: dict[str, Any] = {}
        self.transcript_id: str | None = None

    def _start_phase(self) -> None:
        self.phase_started = time.monotonic()

    def _finish_phase(self, message: str, suffix: str | None = None) -> None:
        if suffix is None:
            suffix = format_elapsed(time.monotonic() - self.phase_started)
        line = f"  {message:<45}  {suffix}".rstrip()
        print(line, file=self.stderr, flush=True)

    def _note(self, message: str) -> None:
        print(f"  {message}", file=self.stderr, flush=True)

    def _connection(self) -> http.client.HTTPConnection:
        port = self.parsed_base.port
        if self.parsed_base.scheme == "https":
            return http.client.HTTPSConnection(self.parsed_base.hostname, port, timeout=60)
        return http.client.HTTPConnection(self.parsed_base.hostname, port, timeout=60)

    def _path(self, suffix: str) -> str:
        prefix = self.parsed_base.path.rstrip("/")
        return f"{prefix}{suffix}"

    def _decode_response(
        self, response: http.client.HTTPResponse, operation: str
    ) -> dict[str, Any]:
        payload = response.read()
        detail = payload.decode("utf-8", errors="replace")
        if not 200 <= response.status < 300:
            raise ProviderHTTPError(operation, response.status, detail[:500] or "no detail")
        if not payload:
            return {}
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ProviderProtocolError(
                f"AssemblyAI {operation} returned invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise ProviderProtocolError(
                f"AssemblyAI {operation} returned a non-object response"
            )
        return value

    def _json_request(
        self, method: str, suffix: str, operation: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        connection = self._connection()
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": self.api_key}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, self._path(suffix), body=encoded, headers=headers)
            return self._decode_response(connection.getresponse(), operation)
        except ProviderError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise ProviderTransportError(
                f"AssemblyAI {operation} transport failed: {error}"
            ) from error
        finally:
            connection.close()

    def _upload(self, audio: Path) -> dict[str, Any]:
        connection = self._connection()
        try:
            connection.putrequest("POST", self._path("/v2/upload"))
            connection.putheader("Authorization", self.api_key)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(audio.stat().st_size))
            connection.endheaders()
            with audio.open("rb") as stream:
                self._send_stream(connection, stream)
            return self._decode_response(connection.getresponse(), "upload")
        except ProviderError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise ProviderTransportError(
                f"AssemblyAI upload transport failed: {error}"
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _send_stream(connection: http.client.HTTPConnection, stream: BinaryIO) -> None:
        while chunk := stream.read(UPLOAD_CHUNK_BYTES):
            connection.send(chunk)

    def _save(self, state: str, **updates: Any) -> dict[str, Any]:
        if self.receipt_path is None:
            raise RuntimeError("receipt path is not configured")
        timestamps = dict(self.receipt.get("timestamps", {}))
        timestamps.setdefault("created_at", utc_now())
        timestamps[f"{state}_at"] = utc_now()
        receipt = {
            **self.receipt,
            "provider": "assemblyai",
            "requested_model": REQUESTED_MODEL,
            "state": state,
            "timestamps": timestamps,
            **updates,
        }
        self.receipt = atomic_write_json(self.receipt_path, receipt)
        return self.receipt

    def _save_published(self, used_model: str) -> None:
        """Compact the receipt down to durable lifecycle evidence."""
        if self.receipt_path is None:
            raise RuntimeError("receipt path is not configured")
        timestamps = dict(self.receipt.get("timestamps", {}))
        timestamps["published_at"] = utc_now()
        receipt = {
            "provider": "assemblyai",
            "requested_model": REQUESTED_MODEL,
            "used_model": used_model,
            "transcript_id": self.transcript_id,
            "state": "published",
            "provider_status": "completed",
            "timestamps": {
                key: timestamps[key]
                for key in COMPACT_TIMESTAMP_KEYS
                if key in timestamps
            },
        }
        self.receipt = atomic_write_json(self.receipt_path, receipt)

    def _update_deletion(self, **fields: Any) -> None:
        if self.receipt_path is None:
            raise RuntimeError("receipt path is not configured")
        receipt = {**self.receipt, "deletion": fields}
        self.receipt = atomic_write_json(self.receipt_path, receipt)

    @staticmethod
    def _transient(error: ProviderError) -> bool:
        return isinstance(error, ProviderTransportError) or (
            isinstance(error, ProviderHTTPError)
            and (error.status == 429 or error.status >= 500)
        )

    def _poll(self) -> dict[str, Any]:
        assert self.transcript_id is not None
        transient_count = 0
        last_status = "submitted"
        while True:
            try:
                response = self._json_request(
                    "GET",
                    f"/v2/transcript/{self.transcript_id}",
                    "poll",
                )
                transient_count = 0
            except ProviderError as error:
                if not self._transient(error) or transient_count >= self.transient_attempts - 1:
                    raise
                transient_count += 1
                self._note(
                    f"retrying transcript status check "
                    f"({transient_count} of {self.transient_attempts - 1})"
                )
                time.sleep(self.poll_interval)
                continue
            status = response.get("status")
            if status in {"queued", "processing"}:
                if status != last_status:
                    self._save(
                        "submitted",
                        transcript_id=self.transcript_id,
                        provider_status=status,
                    )
                    last_status = str(status)
                time.sleep(self.poll_interval)
                continue
            if status in {"completed", "error"}:
                return response
            raise ProviderError(f"AssemblyAI poll returned unknown status: {status!r}")

    def _delete(self) -> bool:
        assert self.transcript_id is not None
        self._start_phase()
        last_error = "unknown deletion failure"
        attempts = 0
        for attempt in range(1, self.transient_attempts + 1):
            attempts = attempt
            try:
                self._json_request(
                    "DELETE",
                    f"/v2/transcript/{self.transcript_id}",
                    "delete",
                )
                self._update_deletion(confirmed=True, confirmed_at=utc_now())
                self._finish_phase(
                    "Deleting remote transcript and audio...", suffix="done"
                )
                return True
            except ProviderError as error:
                last_error = str(error)
                if not self._transient(error) or attempt == self.transient_attempts:
                    break
                self._note(
                    f"retrying remote deletion ({attempt} of {self.transient_attempts - 1})"
                )
                time.sleep(self.poll_interval)
        self._update_deletion(
            confirmed=False,
            attempts=attempts,
            last_error=last_error,
            last_attempt_at=utc_now(),
        )
        self._finish_phase("Deleting remote transcript and audio...", suffix="failed")
        return False

    def run(
        self, audio: Path, output_dir: Path, output_name: str, speakers: int | None
    ) -> tuple[Path, bool]:
        """Transcribe audio and return (canonical JSON path, deletion confirmed)."""
        self.receipt_path = output_dir / RECEIPT_NAME
        self._start_phase()
        try:
            upload_response = self._upload(audio)
        except (ProviderTransportError, ProviderProtocolError) as error:
            self._save("unknown_after_upload", error=str(error))
            raise
        except ProviderHTTPError as error:
            if error.status >= 500:
                self._save("unknown_after_upload", error=str(error))
            else:
                self._save("upload_rejected", error=str(error))
            raise
        upload_url = upload_response.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url:
            self._save("unknown_after_upload", error="upload response had no upload_url")
            raise ProviderError("AssemblyAI upload response had no upload_url")
        self._save("upload_succeeded")
        self._finish_phase("Uploading recording...")

        request: dict[str, Any] = {
            "audio_url": upload_url,
            "speech_models": [REQUESTED_MODEL],
            "speaker_labels": True,
            "punctuate": True,
            "format_text": True,
            "language_detection": True,
        }
        if speakers is not None:
            request["speakers_expected"] = speakers
        self._start_phase()
        try:
            submission = self._json_request(
                "POST", "/v2/transcript", "submission", request
            )
        except (ProviderTransportError, ProviderProtocolError) as error:
            self._save("unknown_after_submit", error=str(error))
            raise
        except ProviderHTTPError as error:
            if error.status >= 500:
                self._save("unknown_after_submit", error=str(error))
            else:
                self._save("upload_succeeded", submission_error=str(error))
            raise
        transcript_id = submission.get("id")
        if not isinstance(transcript_id, str) or not transcript_id:
            self._save(
                "unknown_after_submit", error="submission response had no transcript ID"
            )
            raise ProviderError("AssemblyAI submission response had no transcript ID")
        self.transcript_id = transcript_id
        try:
            self._save(
                "submitted",
                transcript_id=transcript_id,
                provider_status=str(submission.get("status") or "submitted"),
            )
            completed = self._poll()
            provider_status = completed.get("status")
            if provider_status == "error":
                self._save(
                    "provider_error",
                    provider_status="error",
                    provider_error=str(completed.get("error", "unknown error"))[:500],
                )
                raise ProviderError(
                    f"AssemblyAI transcription failed: {completed.get('error', 'unknown error')}"
                )
            self._finish_phase("Transcribing and identifying speakers...")
            used_model = str(completed.get("speech_model_used") or "unknown")
            self._save("completed", provider_status="completed", used_model=used_model)
            try:
                canonical = convert_response(completed)
                canonical_path = output_dir / f"{output_name}.json"
                save_canonical(canonical_path, canonical)
                markdown_path = render_markdown(canonical_path)
                html_path = render_html(canonical_path)
                for view in (markdown_path, html_path):
                    if not view.is_file() or view.stat().st_size == 0:
                        raise ValueError(f"transcript view was not saved: {view}")
                self._save_published(used_model)
                self._finish_phase("Saving local transcript...", suffix="")
            except BaseException as error:
                self._save(
                    "publication_failed",
                    publication_error=f"{type(error).__name__}: {error}"[:500],
                )
                raise
        except BaseException as error:
            try:
                if self.receipt.get("state") in {"upload_succeeded", "submitted", "completed"}:
                    if isinstance(error, KeyboardInterrupt):
                        self._save(
                            "interrupted",
                            transcript_id=self.transcript_id,
                            lifecycle_error="KeyboardInterrupt",
                        )
                    else:
                        self._save(
                            "poll_failed",
                            transcript_id=self.transcript_id,
                            lifecycle_error=f"{type(error).__name__}: {error}"[:500],
                        )
            except Exception as receipt_error:
                print(
                    f"Warning: could not persist the latest AssemblyAI lifecycle "
                    f"evidence for {self.transcript_id}: {receipt_error}",
                    file=self.stderr,
                )
            try:
                self._delete()
            except Exception as cleanup_error:
                print(
                    f"Warning: AssemblyAI cleanup could not be recorded for transcript "
                    f"{self.transcript_id}: {cleanup_error}.",
                    file=self.stderr,
                )
            raise

        # After publication the transcript is usable no matter what happens to
        # remote deletion: any escape from the final DELETE (including Ctrl-C)
        # is the cleanup-required outcome, never a hard failure.
        try:
            confirmed = self._delete()
        except BaseException as error:
            confirmed = False
            try:
                self._update_deletion(
                    confirmed=False,
                    last_error=f"{type(error).__name__}: {error}"[:500],
                    last_attempt_at=utc_now(),
                )
                self._finish_phase(
                    "Deleting remote transcript and audio...", suffix="failed"
                )
            except Exception:
                pass
        return output_dir / f"{output_name}.json", confirmed

    def _qualified_absent_after_delete(self) -> bool:
        """A 404 confirms cleanup only with independent submit+terminal evidence."""
        timestamps = self.receipt.get("timestamps")
        if not isinstance(timestamps, dict):
            return False
        return (
            isinstance(self.receipt.get("transcript_id"), str)
            and isinstance(timestamps.get("submitted_at"), str)
            and self.receipt.get("provider_status") == "completed"
            and isinstance(timestamps.get("completed_at"), str)
        )

    def _compact_cleanup_confirmed(self, attempts: int, via_absent: bool) -> None:
        if self.receipt_path is None:
            raise RuntimeError("receipt path is not configured")
        timestamps = self.receipt.get("timestamps")
        deletion: dict[str, Any] = {
            "confirmed": True,
            "confirmed_at": utc_now(),
            "attempts": attempts,
        }
        if via_absent:
            deletion["confirmed_via"] = "absent_after_delete"
        receipt = {
            "provider": "assemblyai",
            "requested_model": self.receipt.get("requested_model", REQUESTED_MODEL),
            "used_model": self.receipt.get("used_model"),
            "transcript_id": self.transcript_id,
            "state": "published",
            "provider_status": self.receipt.get("provider_status"),
            "timestamps": {
                key: timestamps[key]
                for key in COMPACT_TIMESTAMP_KEYS
                if isinstance(timestamps, dict) and key in timestamps
            },
            "deletion": deletion,
        }
        self.receipt = atomic_write_json(self.receipt_path, receipt)
        if not isinstance(self.receipt.get("deletion"), dict) or (
            self.receipt["deletion"].get("confirmed") is not True
        ):
            raise ProviderError(
                f"cleanup receipt did not reload as confirmed: {self.receipt_path}"
            )

    def cleanup(self, canonical_path: Path) -> int:
        """Recovery-only remote deletion for one published run. Never uploads."""
        assert self.receipt_path is not None and self.transcript_id is not None
        deletion = self.receipt.get("deletion")
        if isinstance(deletion, dict) and deletion.get("confirmed") is True:
            print(
                "Remote cleanup is already confirmed; nothing to do.",
                file=self.stderr,
            )
            print(canonical_path)
            return 0

        last_error = "unknown deletion failure"
        attempts = 0
        confirmed = False
        via_absent = False
        for attempt in range(1, self.transient_attempts + 1):
            attempts = attempt
            try:
                self._json_request(
                    "DELETE",
                    f"/v2/transcript/{self.transcript_id}",
                    "delete",
                )
                confirmed = True
                break
            except ProviderHTTPError as error:
                last_error = str(error)
                if error.status == 404:
                    if self._qualified_absent_after_delete():
                        confirmed = True
                        via_absent = True
                    else:
                        last_error = (
                            f"AssemblyAI delete returned HTTP 404 without receipt "
                            f"evidence that transcript {self.transcript_id} existed"
                        )
                    break
                if not self._transient(error) or attempt == self.transient_attempts:
                    break
                self._note(
                    f"retrying remote deletion ({attempt} of {self.transient_attempts - 1})"
                )
                time.sleep(self.poll_interval)
            except ProviderError as error:
                last_error = str(error)
                if not self._transient(error) or attempt == self.transient_attempts:
                    break
                self._note(
                    f"retrying remote deletion ({attempt} of {self.transient_attempts - 1})"
                )
                time.sleep(self.poll_interval)

        if confirmed:
            self._compact_cleanup_confirmed(attempts, via_absent)
            print("Remote cleanup complete", file=self.stderr)
            print(canonical_path)
            return 0

        self._update_deletion(
            confirmed=False,
            attempts=attempts,
            last_error=last_error[:500],
            last_attempt_at=utc_now(),
        )
        created_at = ""
        timestamps = self.receipt.get("timestamps")
        if isinstance(timestamps, dict) and isinstance(timestamps.get("created_at"), str):
            created_at = timestamps["created_at"]
        print("", file=self.stderr)
        print("Remote cleanup failed", file=self.stderr)
        print(
            f"  Remote data deletion remains unconfirmed for transcript "
            f"{self.transcript_id}",
            file=self.stderr,
        )
        print(
            f"  Action      {SUPPORT_ACTION} with transcript {self.transcript_id} "
            f"and receipt timestamp {created_at or 'unavailable'}",
            file=self.stderr,
        )
        return 1


def resolve_cleanup_target(canonical_path: Path) -> tuple[Path, dict[str, Any], str]:
    """Validate a cleanup target without mutating anything."""
    if not canonical_path.is_file() or canonical_path.is_symlink():
        raise CleanupTargetError(f"transcript JSON does not exist: {canonical_path}")
    run_dir = canonical_path.parent
    receipt_path = run_dir / RECEIPT_NAME
    if not receipt_path.is_file() or receipt_path.is_symlink():
        if (run_dir / HISTORICAL_RECEIPT_NAME).is_file():
            raise CleanupTargetError(
                "this transcript predates the current app; its old receipt is kept "
                "as-is and is not managed by meeting cleanup"
            )
        raise CleanupTargetError(
            f"no AssemblyAI receipt was found next to: {canonical_path}"
        )
    if canonical_path.name != "transcript.json":
        raise CleanupTargetError("meeting cleanup requires the canonical transcript.json")
    try:
        managed_json, receipt_path, receipt, transcript_id = validate_managed_run(run_dir)
    except ManagedRunError as error:
        raise CleanupTargetError(str(error)) from error
    if managed_json != canonical_path:
        raise CleanupTargetError("the cleanup target is not the managed canonical transcript")
    return receipt_path, receipt, transcript_id


def _receipt_created_at(receipt: dict[str, Any]) -> str:
    timestamps = receipt.get("timestamps")
    if isinstance(timestamps, dict) and isinstance(timestamps.get("created_at"), str):
        return timestamps["created_at"]
    return "unavailable"


def failure_block(
    adapter: AssemblyAIAdapter, audio: Path, error: BaseException
) -> list[str]:
    """One failure block, one recommended action, chosen by remote state."""
    receipt = adapter.receipt
    state = receipt.get("state")
    deletion = receipt.get("deletion")
    deletion_confirmed = isinstance(deletion, dict) and deletion.get("confirmed") is True
    transcript_id = adapter.transcript_id
    created_at = _receipt_created_at(receipt)

    if isinstance(error, KeyboardInterrupt):
        problem = "Transcription was interrupted"
    elif isinstance(error, ModelMismatchError):
        problem = (
            f"AssemblyAI used model {error.reported!r} instead of {REQUESTED_MODEL}"
        )
    elif state == "upload_rejected":
        problem = "AssemblyAI rejected the audio upload"
    elif state == "unknown_after_upload":
        problem = "The audio upload outcome is unknown"
    elif state == "upload_succeeded" and "submission_error" in receipt:
        problem = "AssemblyAI rejected the transcription request"
    elif state == "unknown_after_submit":
        problem = "The transcription request outcome is unknown"
    elif state == "provider_error":
        problem = "AssemblyAI could not transcribe the recording"
    elif state == "publication_failed":
        problem = "The transcript could not be saved locally"
    else:
        problem = "AssemblyAI did not finish the transcript"

    lines = ["", "Transcription stopped", f"  Problem     {problem}"]
    lines.append(f"  Recording   safe at {audio}")

    if isinstance(error, ModelMismatchError):
        if deletion_confirmed:
            lines.append("  Remote transcript and audio deletion is confirmed")
        elif transcript_id:
            lines.append(
                f"  Remote data deletion unconfirmed for transcript {transcript_id}"
            )
        lines.append(
            f"  Action      {SUPPORT_ACTION} with receipt timestamp {created_at} "
            f"and reported model {error.reported!r}"
        )
        return lines

    if state == "upload_rejected":
        lines.append("  No audio or transcript was stored remotely")
        lines.append(f"  Retry       meeting transcribe {audio}")
        return lines

    if transcript_id:
        if deletion_confirmed:
            lines.append("  Remote transcript and audio deletion is confirmed")
            lines.append(f"  Retry       meeting transcribe {audio}")
        else:
            lines.append(
                f"  Remote data deletion unconfirmed for transcript {transcript_id}"
            )
            lines.append(
                f"  Action      {SUPPORT_ACTION} with transcript {transcript_id} "
                f"and receipt timestamp {created_at} before retrying"
            )
        return lines

    lines.append("  Uploaded audio may remain remotely; no transcript ID is known")
    lines.append(
        f"  Action      {SUPPORT_ACTION} with receipt timestamp {created_at} "
        f"before retrying"
    )
    return lines


def cleanup_required_block(
    audio: Path, canonical_path: Path, transcript_id: str | None
) -> list[str]:
    run_dir = canonical_path.parent
    identifier = transcript_id or "unknown"
    return [
        "",
        "Transcript saved; cleanup required",
        f"  Recording   safe at {audio}",
        f"  Remote data deletion unconfirmed for transcript {identifier}",
        f"  Read        {run_dir / 'transcript.html'}",
        f"  Agent       {run_dir / 'transcript.md'}",
        f"  Data        {canonical_path}",
        f"  Cleanup     meeting cleanup {canonical_path}",
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe prerecorded audio with AssemblyAI Universal-3.5 Pro."
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--output-name")
    parser.add_argument("--speakers", type=int)
    parser.add_argument(
        "--cleanup",
        type=Path,
        metavar="TRANSCRIPT_JSON",
        help="recovery-only remote deletion for one published run",
    )
    return parser.parse_args()


def _build_adapter(api_key: str) -> AssemblyAIAdapter:
    base_url = os.environ.get("MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE", API_BASE_URL)
    poll_interval = float(os.environ.get("MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL", "3"))
    return AssemblyAIAdapter(api_key=api_key, base_url=base_url, poll_interval=poll_interval)


def main() -> int:
    args = parse_args()
    os.umask(0o077)
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        print("Error: ASSEMBLYAI_API_KEY is required", file=sys.stderr)
        return 2

    if args.cleanup is not None:
        if args.input or args.output_dir or args.output_name or args.speakers:
            print("Error: --cleanup does not accept other arguments", file=sys.stderr)
            return 2
        # Reject a symlink at the caller-supplied final component before
        # canonicalizing parent-directory aliases such as macOS /var.
        lexical_path = args.cleanup.expanduser().absolute()
        if lexical_path.is_symlink():
            print("Error: cleanup transcript must be a regular non-symlink file", file=sys.stderr)
            return 2
        canonical_path = lexical_path.resolve()
        try:
            receipt_path, receipt, transcript_id = resolve_cleanup_target(canonical_path)
        except CleanupTargetError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 2
        adapter = _build_adapter(api_key)
        adapter.receipt_path = receipt_path
        adapter.receipt = receipt
        adapter.transcript_id = transcript_id
        try:
            return adapter.cleanup(canonical_path)
        except KeyboardInterrupt:
            print("Error: cleanup interrupted", file=sys.stderr)
            return 130
        except (ProviderError, ValueError, OSError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

    if not args.input or not args.output_dir or not args.output_name:
        print(
            "Error: --input, --output-dir, and --output-name are required",
            file=sys.stderr,
        )
        return 2
    audio = args.input.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not audio.is_file() or not os.access(audio, os.R_OK):
        print(f"Error: input is not a readable file: {audio}", file=sys.stderr)
        return 2
    if not output_dir.is_dir() or not os.access(output_dir, os.W_OK):
        print(f"Error: output directory is not writable: {output_dir}", file=sys.stderr)
        return 2
    if args.speakers is not None and args.speakers < 1:
        print("Error: --speakers must be positive", file=sys.stderr)
        return 2
    if "/" in args.output_name or args.output_name in {"", ".", ".."}:
        print("Error: --output-name must be a plain filename", file=sys.stderr)
        return 2
    adapter = _build_adapter(api_key)
    try:
        canonical_path, deletion_confirmed = adapter.run(
            audio, output_dir, args.output_name, args.speakers
        )
    except KeyboardInterrupt as error:
        for line in failure_block(adapter, audio, error):
            print(line, file=sys.stderr)
        return 130
    except (ProviderError, CanonicalTranscriptError, ValueError, OSError) as error:
        for line in failure_block(adapter, audio, error):
            print(line, file=sys.stderr)
        return 1
    if deletion_confirmed:
        print(canonical_path)
        return 0
    for line in cleanup_required_block(audio, canonical_path, adapter.transcript_id):
        print(line, file=sys.stderr)
    print(canonical_path)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
