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

from transcript_bundle import write_outputs


API_BASE_URL = "https://api.assemblyai.com"
REQUESTED_MODEL = "universal-3-5-pro"
UPLOAD_CHUNK_BYTES = 1024 * 1024
TRANSIENT_ATTEMPTS = 3


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


def utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def convert_response(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("speech_model_used") != REQUESTED_MODEL:
        reported = response.get("speech_model_used")
        raise ValueError(
            f"AssemblyAI reported model {reported!r}, expected {REQUESTED_MODEL}; "
            "retry with --local if the managed model is unavailable"
        )
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
    segments: list[dict[str, Any]] = []
    for index, utterance in enumerate(usable_utterances):
        start_ms = _finite_number(utterance.get("start"), "utterance start")
        end_ms = _finite_number(utterance.get("end"), "utterance end")
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("AssemblyAI completion has invalid utterance timestamps")
        provider_speaker = str(utterance["speaker"])
        words_value = utterance.get("words")
        if not isinstance(words_value, list):
            raise ValueError("AssemblyAI utterance has no word list")
        words: list[dict[str, Any]] = []
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
                {
                    "word": word_text,
                    "start": word_start_ms / 1000,
                    "end": word_end_ms / 1000,
                    "probability": confidence,
                    "speaker": speaker_map[str(word_speaker)],
                }
            )
            timed_word_count += 1
        segments.append(
            {
                "id": index,
                "start": start_ms / 1000,
                "end": end_ms / 1000,
                "text": str(utterance["text"]),
                "speaker": speaker_map[provider_speaker],
                "words": words,
            }
        )
    if timed_word_count == 0:
        raise ValueError("AssemblyAI completion has no timed words")
    return {"language": language, "text": text, "segments": segments}


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
        self.started = time.monotonic()
        self.receipt_path: Path | None = None
        self.receipt: dict[str, Any] = {}
        self.transcript_id: str | None = None

    def _progress(self, message: str) -> None:
        elapsed = int(time.monotonic() - self.started)
        print(f"AssemblyAI: {message} (elapsed {elapsed}s)", file=self.stderr, flush=True)

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
                self._progress(f"poll retry {transient_count}/{self.transient_attempts - 1}")
                time.sleep(self.poll_interval)
                continue
            status = response.get("status")
            if status in {"queued", "processing"}:
                if status != last_status:
                    self._progress(str(status))
                    last_status = str(status)
                time.sleep(self.poll_interval)
                continue
            if status in {"completed", "error"}:
                return response
            raise ProviderError(f"AssemblyAI poll returned unknown status: {status!r}")

    def _delete(self) -> bool:
        assert self.transcript_id is not None
        last_error = "unknown deletion failure"
        for attempt in range(1, self.transient_attempts + 1):
            try:
                self._json_request(
                    "DELETE",
                    f"/v2/transcript/{self.transcript_id}",
                    "delete",
                )
                self._save(
                    self.receipt.get("state", "unknown"),
                    transcript_id=self.transcript_id,
                    deletion={"confirmed": True, "confirmed_at": utc_now()},
                )
                self._progress("remote transcript and uploaded audio deleted")
                return True
            except ProviderError as error:
                last_error = str(error)
                if not self._transient(error) or attempt == self.transient_attempts:
                    break
                self._progress(f"delete retry {attempt}/{self.transient_attempts - 1}")
                time.sleep(self.poll_interval)
        self._save(
            self.receipt.get("state", "unknown"),
            transcript_id=self.transcript_id,
            deletion={"confirmed": False, "last_error": last_error},
        )
        print(
            f"Warning: AssemblyAI deletion is unconfirmed for transcript "
            f"{self.transcript_id}; transcript/audio may remain remotely. "
            "Delete it in the AssemblyAI dashboard.",
            file=self.stderr,
        )
        return False

    def run(
        self, audio: Path, output_dir: Path, output_name: str, speakers: int | None
    ) -> Path:
        self.receipt_path = output_dir / f"{output_name}.assemblyai.json"
        self._progress(f"uploading {audio.name}")
        try:
            upload_response = self._upload(audio)
        except (ProviderTransportError, ProviderProtocolError) as error:
            self._save("unknown_after_upload", error=str(error))
            print(
                "Warning: the upload outcome is unknown; partial remote audio may exist.",
                file=self.stderr,
            )
            raise
        except ProviderError as error:
            self._save("upload_rejected", error=str(error))
            raise
        upload_url = upload_response.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url:
            self._save("unknown_after_upload", error="upload response had no upload_url")
            print(
                "Warning: upload succeeded but its reference was malformed; remote audio may exist.",
                file=self.stderr,
            )
            raise ProviderError("AssemblyAI upload response had no upload_url")
        self._save("upload_succeeded")
        self._progress("upload complete; submitting one transcription job")

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
        try:
            submission = self._json_request(
                "POST", "/v2/transcript", "submission", request
            )
        except (ProviderTransportError, ProviderProtocolError) as error:
            self._save("unknown_after_submit", error=str(error))
            print(
                "Warning: submission outcome is unknown; uploaded audio and an unknown "
                "billable job may remain. The submission was not retried.",
                file=self.stderr,
            )
            raise
        except ProviderError as error:
            self._save("upload_succeeded", submission_error=str(error))
            print(
                "Warning: submission was rejected; uploaded audio may remain for the "
                "provider retention window.",
                file=self.stderr,
            )
            raise
        transcript_id = submission.get("id")
        if not isinstance(transcript_id, str) or not transcript_id:
            self._save(
                "unknown_after_submit", error="submission response had no transcript ID"
            )
            print(
                "Warning: submission returned no ID; uploaded audio and an unknown "
                "billable job may remain.",
                file=self.stderr,
            )
            raise ProviderError("AssemblyAI submission response had no transcript ID")
        self.transcript_id = transcript_id
        try:
            self._save(
                "submitted",
                transcript_id=transcript_id,
                provider_status=submission.get("status"),
            )
            self._progress(f"submitted transcript {transcript_id}")
            completed = self._poll()
            provider_status = completed.get("status")
            if provider_status == "error":
                self._save(
                    "provider_error",
                    provider_status=provider_status,
                    provider_response=completed,
                )
                raise ProviderError(
                    f"AssemblyAI transcription failed: {completed.get('error', 'unknown error')}"
                )
            self._save(
                "completed",
                provider_status=provider_status,
                provider_response=completed,
            )
            try:
                canonical = convert_response(completed)
                write_outputs(canonical, output_dir, output_name)
                canonical_path = output_dir / f"{output_name}.json"
                with canonical_path.open(encoding="utf-8") as stream:
                    reloaded = json.load(stream)
                if reloaded != canonical:
                    raise ValueError("canonical transcript did not reload identically")
                self._save("published", provider_response=completed)
            except BaseException as error:
                self._save(
                    "publication_failed",
                    provider_response=completed,
                    publication_error=f"{type(error).__name__}: {error}",
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
                            lifecycle_error=f"{type(error).__name__}: {error}",
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
                    f"{self.transcript_id}: {cleanup_error}. Check the AssemblyAI dashboard.",
                    file=self.stderr,
                )
            raise

        if not self._delete():
            raise ProviderError(
                f"AssemblyAI deletion is unconfirmed for transcript {self.transcript_id}"
            )
        return output_dir / f"{output_name}.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe prerecorded audio with AssemblyAI Universal-3.5 Pro."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--output-name", required=True)
    parser.add_argument("--speakers", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.umask(0o077)
    audio = args.input.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not audio.is_file() or not os.access(audio, os.R_OK):
        raise SystemExit(f"input is not a readable file: {audio}")
    if not output_dir.is_dir() or not os.access(output_dir, os.W_OK):
        raise SystemExit(f"output directory is not writable: {output_dir}")
    if args.speakers is not None and args.speakers < 1:
        raise SystemExit("--speakers must be positive")
    if "/" in args.output_name or args.output_name in {"", ".", ".."}:
        raise SystemExit("--output-name must be a plain filename")
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        raise SystemExit("ASSEMBLYAI_API_KEY is required")
    base_url = os.environ.get("MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE", API_BASE_URL)
    poll_interval = float(os.environ.get("MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL", "3"))
    adapter = AssemblyAIAdapter(
        api_key=api_key, base_url=base_url, poll_interval=poll_interval
    )
    try:
        canonical_path = adapter.run(audio, output_dir, args.output_name, args.speakers)
    except KeyboardInterrupt:
        print("Error: AssemblyAI transcription interrupted", file=sys.stderr)
        return 130
    except (ProviderError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(canonical_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
