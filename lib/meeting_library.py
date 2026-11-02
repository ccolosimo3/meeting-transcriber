"""Session-scoped projection of the existing meeting bundle layout.

Only opaque IDs cross the desktop bridge. Files are opened and verified before
any bytes are read; callers own and close returned media/export handles.
Only explicit digest publication writes new, exclusively owned artifacts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
import sys
import threading
import tempfile
import shutil
from dataclasses import dataclass
from typing import BinaryIO

from meeting_identity import meeting_identity
from transcript_bundle import canonical_to_json, parse_canonical
from transcript_run import classify
from meeting_digests import Source, DigestError, MAX_SOURCE, MAX_RECORD, GENERATION, sha256, parse_record, verify_private


class LibraryError(ValueError):
    """A bounded, displayable error; filesystem exception text is never exposed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(error: Exception, *, library: bool = False) -> LibraryError:
    if isinstance(error, LibraryError):
        return error
    if isinstance(error, PermissionError):
        return LibraryError("access_denied", "Access denied. Choose a library or refresh.")
    if library:
        return LibraryError("unavailable_library", "Library unavailable. Choose a library or refresh.")
    return LibraryError("unavailable_source", "Source unavailable. Refresh the library.")


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _handle_path(descriptor: int) -> Path:
    """Ask the OS where an already opened handle actually points."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        final_name = kernel.GetFinalPathNameByHandleW
        final_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final_name.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_name(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError("Cannot verify opened file")
        name = buffer.value
        if name.startswith("\\\\?\\UNC\\"):
            name = "\\\\" + name[8:]
        elif name.startswith("\\\\?\\"):
            name = name[4:]
        return Path(name)
    if sys.platform == "darwin":
        import fcntl

        # macOS F_GETPATH returns the vnode's path, not a fresh path resolution.
        return Path(os.fsdecode(fcntl.fcntl(descriptor, 50, bytes(1024)).split(b"\0", 1)[0]))
    return Path(os.readlink(f"/proc/self/fd/{descriptor}"))


def _recording(name: str) -> bool:
    return (name == "recording" or name.startswith("recording.")) and not name.startswith(
        "recording.partial."
    )


@dataclass(frozen=True)
class _Run:
    path: Path
    identifier: str
    identity: tuple[object, ...]


@dataclass(frozen=True)
class _Meeting:
    path: Path
    title: str
    added: str
    runs: tuple[_Run, ...]
    identity: tuple[int, int]


class Library:
    def __init__(self, root: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._meetings: dict[str, _Meeting] = {}
        self.root: Path | None = None
        self._root_identity: tuple[int, int] | None = None
        if root is not None:
            try:
                lexical = Path(root).expanduser()
                if _linked(lexical.lstat()):
                    raise OSError('Linked library')
                self.root = lexical.resolve(strict=True)
                info = self.root.lstat()
                if not stat.S_ISDIR(info.st_mode) or _linked(info):
                    raise OSError("Not a directory")
                self._root_identity = _identity(info)
            except OSError as error:
                raise _error(error, library=True) from None

    def _check(self, path: Path) -> os.stat_result:
        if self.root is None:
            raise LibraryError("unavailable_library", "Choose a library.")
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            raise LibraryError("unsafe_path", "Source is outside the selected library.") from None
        if any(part in (".", "..") for part in parts):
            raise LibraryError("unsafe_path", "Source is outside the selected library.")
        current = self.root
        info = current.lstat()
        if _linked(info) or _identity(info) != self._root_identity:
            raise LibraryError("unsafe_path", "Library changed. Choose the library again.")
        for part in parts:
            current = current / part
            info = current.lstat()
            if _linked(info):
                raise LibraryError("unsafe_path", "Linked sources cannot be opened. Refresh the library.")
        return info

    def _open(self, path: Path) -> BinaryIO:
        self._check(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise LibraryError("unavailable_source", "Source is not a regular file.")
            actual = _handle_path(descriptor)
            # Exact equality also rejects redirected ancestors and inside-root
            # symlinks swapped in between the preflight and open operation.
            if os.path.normcase(str(actual)) != os.path.normcase(str(path)):
                raise LibraryError("unsafe_path", "Source changed. Refresh the library.")
            if _identity(self._check(path)) != _identity(info):
                raise LibraryError("unsafe_path", "Source changed. Refresh the library.")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    def _names(self, path: Path) -> list[Path]:
        info = self._check(path)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError()
        names = list(path.iterdir())
        if _identity(self._check(path)) != _identity(info):
            raise LibraryError("unsafe_path", "Source changed. Refresh the library.")
        return sorted(names, key=lambda item: (item.name.encode("utf-8"), str(item).encode("utf-8")), reverse=True)

    def _regular(self, path: Path) -> bool:
        try:
            if not stat.S_ISREG(self._check(path).st_mode):
                return False
            with self._open(path):
                return True
        except (FileNotFoundError, IsADirectoryError):
            return False

    def _media(self, meeting: _Meeting) -> list[Path]:
        return [path for path in self._names(meeting.path) if _recording(path.name) and self._regular(path)]

    def _canonical(self, path: Path, *, historical: bool) -> dict[str, object]:
        with self._open(path) as stream:
            transcript = parse_canonical(json.loads(stream.read().decode("utf-8")), require_speaker_names=not historical)
        return canonical_to_json(transcript)

    def _run(self, path: Path, *, include_text: bool = False) -> dict[str, object] | None:
        self._check(path)
        try:
            result = classify(path, opener=self._open)
        except LibraryError:
            raise
        except (ValueError, OverflowError, RecursionError):
            raise LibraryError("malformed_transcript", "Saved transcript is malformed.") from None
        if result is None:
            if include_text:
                try:
                    self._canonical(path / "transcript.json", historical=False)
                except LibraryError:
                    raise
                except (ValueError, OverflowError, RecursionError):
                    raise LibraryError("malformed_transcript", "Saved transcript is malformed.") from None
            return None
        kind, json_path, _ = result
        historical = kind.startswith("historical")
        status = "Historical transcript" if historical else "Transcript ready"
        canonical = None
        if historical or include_text:
            try:
                canonical = self._canonical(json_path, historical=historical)
            except LibraryError:
                raise
            except (ValueError, OverflowError, RecursionError):
                if not historical:
                    raise LibraryError("malformed_transcript", "Saved transcript is malformed.") from None
                status = "Saved HTML available"
        if kind in ("cleanup-required", "historical-unconfirmed"):
            status += "; remote cleanup unconfirmed"
        return {"status": status, "canonical": canonical, "cleanup_required": kind == "cleanup-required"}

    def scan(self) -> dict[str, object]:
        """Revalidate the snapshot; unchanged entries retain their session IDs."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            previous = {meeting.path: (identifier, meeting) for identifier, meeting in self._meetings.items()}
            self._meetings = {}
        result: dict[str, object] = {"library": str(self.root) if self.root else "", "meetings": [], "errors": []}
        if self.root is None:
            return result
        rows: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        snapshot: dict[str, _Meeting] = {}

        def report(error: Exception, *, library: bool = False) -> None:
            bounded = _error(error, library=library)
            if len(errors) < 100:
                errors.append({"code": bounded.code, "message": bounded.message})

        try:
            bundles = self._names(self.root)
        except (OSError, LibraryError) as error:
            report(error, library=True)
            bundles = []
        for bundle in bundles:
            try:
                if not stat.S_ISDIR(self._check(bundle).st_mode):
                    continue
                names = self._names(bundle)
                transcript_dir = bundle / "transcripts"
                has_transcripts = any(path.name == "transcripts" for path in names)
                if not has_transcripts and not any(_recording(path.name) or path.name.startswith("recording.partial.") for path in names):
                    continue
                bundle_identity = _identity(self._check(bundle))
                old_id, old_meeting = previous.get(bundle, (None, None))
                unchanged = old_meeting is not None and old_meeting.identity == bundle_identity
                identifier = old_id if unchanged else secrets.token_urlsafe(24)
                old_runs = {run.path: run for run in old_meeting.runs} if unchanged else {}
                title, added, _ = meeting_identity(transcript_dir / "run" / "transcript.json")
                runs: list[_Run] = []
                run_rows: list[dict[str, str]] = []
                if has_transcripts:
                    try:
                        candidates = self._names(transcript_dir)
                    except (OSError, LibraryError) as error:
                        report(error)
                        candidates = []
                    for run_dir in candidates:
                        try:
                            description = self._run(run_dir)
                            if description is not None:
                                fingerprint: list[object] = [_identity(self._check(run_dir))]
                                for name in ("transcript.json", "transcript.html", "transcript.md", ".assemblyai.json", "transcript.assemblyai.json"):
                                    try:
                                        info = self._check(run_dir / name)
                                        fingerprint.append((_identity(info), info.st_size, info.st_mtime_ns))
                                    except FileNotFoundError:
                                        fingerprint.append(None)
                                run_identity = tuple(fingerprint)
                                old_run = old_runs.get(run_dir)
                                run_id = old_run.identifier if old_run and old_run.identity == run_identity else secrets.token_urlsafe(24)
                                runs.append(_Run(run_dir, run_id, run_identity))
                                run_rows.append({"id": run_id, "label": run_dir.name, "status": str(description["status"])})
                        except (OSError, LibraryError) as error:
                            report(error)
                meeting = _Meeting(bundle, title, added, tuple(runs), bundle_identity)
                try:
                    media = self._media(meeting)
                    partial = any(path.name.startswith("recording.partial.") and self._regular(path) for path in names)
                except (OSError, LibraryError) as error:
                    report(error)
                    media, partial = [], False
                status = run_rows[0]["status"] if run_rows else (
                    "Transcript incomplete or unavailable" if has_transcripts else
                    "Recording saved" if media else "Unfinished recording" if partial else "Transcript incomplete or unavailable"
                )
                snapshot[identifier] = meeting
                rows.append({"id": identifier, "title": title, "added": added, "status": status,
                             "runs": run_rows, "default_run_id": run_rows[0]["id"] if run_rows else None})
            except (OSError, LibraryError) as error:
                report(error)
        with self._lock:
            self._current(generation)
            self._meetings = snapshot
        result.update(meetings=rows, errors=errors)
        return result

    def _current(self, generation: int) -> None:
        if generation != self._generation:
            raise LibraryError("stale_id", "Selection expired. Refresh and select the meeting again.")

    def _selection(self, meeting_id: str, run_id: str | None = None) -> tuple[_Meeting, _Run | None, int]:
        with self._lock:
            meeting = self._meetings.get(meeting_id) if isinstance(meeting_id, str) else None
            if meeting is None:
                raise LibraryError("stale_id", "Unknown or expired meeting. Refresh and select it again.")
            run = next((item for item in meeting.runs if item.identifier == run_id), None)
            if run_id is not None and run is None:
                raise LibraryError("stale_id", "Unknown or expired run. Refresh and select it again.")
            return meeting, run, self._generation

    def _transcription_warning(self, meeting):
        """Keep unresolved pre-publication disclosure visible after relaunch."""
        try:
            candidates = self._names(meeting.path / 'transcripts')
        except FileNotFoundError:
            return ''
        for run in candidates:
            try:
                with self._open(run / '.assemblyai.json') as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError()
                receipt = json.loads(raw)
                if not isinstance(receipt, dict):
                    raise ValueError()
                if receipt.get('state') == 'published':
                    continue
                deletion = receipt.get('deletion')
                if receipt.get('state') == 'upload_rejected' or isinstance(deletion, dict) and deletion.get('confirmed') is True:
                    continue
                return 'An earlier transcription attempt has an unresolved remote outcome. Contact AssemblyAI support with the saved .assemblyai.json receipt before transcribing again.'
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                return 'An earlier transcription receipt is unavailable or malformed. Contact AssemblyAI support before transcribing again.'
        return ''

    def load(self, meeting_id: str, run_id: str | None = None) -> dict[str, object]:
        meeting, run, generation = self._selection(meeting_id, run_id)
        try:
            self._check(meeting.path)
            media_error = None
            try:
                media = self._media(meeting)
            except (OSError, LibraryError) as error:
                media = []
                bounded = _error(error)
                media_error = {"code": bounded.code, "message": bounded.message}
            description = self._run(run.path, include_text=True) if run else None
            if run and description is None:
                raise LibraryError("unavailable_source", "Transcript incomplete or unavailable. Refresh the library.")
            status = str(description["status"]) if description else (
                "Transcript incomplete or unavailable" if any(path.name == "transcripts" for path in self._names(meeting.path)) else
                "Recording saved" if media else "Unfinished recording" if any(
                    path.name.startswith("recording.partial.") and self._regular(path) for path in self._names(meeting.path)
                ) else "Transcript incomplete or unavailable"
            )
            result = {"title": meeting.title, "added": meeting.added, "status": status,
                      "canonical": description["canonical"] if description else None,
                      "cleanup_required": bool(description and description["cleanup_required"]),
                      "transcription_warning": self._transcription_warning(meeting),
                      "media_state": "ready" if len(media) == 1 else "ambiguous" if media else "missing",
                      "media_name": media[0].name if len(media) == 1 else None,
                      "exports": {"html": run is not None}}
            if media_error:
                result["media_error"] = media_error
            with self._lock:
                self._current(generation)
            return result
        except (OSError, LibraryError) as error:
            raise _error(error) from None

    def open_media(self, meeting_id: str) -> BinaryIO:
        meeting, _, generation = self._selection(meeting_id)
        try:
            candidates = self._media(meeting)
            if len(candidates) != 1:
                raise LibraryError("unavailable_source", "Audio ambiguous." if candidates else "Audio missing.")
            stream = self._open(candidates[0])
            try:
                with self._lock:
                    self._current(generation)
                return stream
            except BaseException:
                stream.close()
                raise
        except (OSError, LibraryError) as error:
            raise _error(error) from None

    def open_export(self, meeting_id: str, run_id: str) -> BinaryIO:
        meeting, run, generation = self._selection(meeting_id, run_id)
        if run is None:
            raise LibraryError("stale_id", "Select a saved transcript run.")
        try:
            if self._run(run.path) is None:
                raise LibraryError("unavailable_source", "Saved HTML unavailable. Refresh the library.")
            stream = self._open(run.path / "transcript.html")
            try:
                with self._lock:
                    self._current(generation)
                return stream
            except BaseException:
                stream.close()
                raise
        except (OSError, LibraryError) as error:
            raise _error(error) from None

    def import_destination(self):
        from private_files import Directory
        self._check(self.root)
        destination = Directory(self.root)
        if destination.identities[destination.path] != self._root_identity:
            raise LibraryError('unsafe_path', 'Library changed. Choose it again.')
        return destination

    def recording_source(self, meeting_id):
        from transcription import recording_at
        meeting, _, generation = self._selection(meeting_id)
        if _identity(self._check(meeting.path)) != meeting.identity:
            raise LibraryError('stale_id', 'The meeting changed. Refresh and select it again.')
        candidates = self._media(meeting)
        if len(candidates) != 1:
            raise LibraryError('unavailable_source', 'Select one safe complete recording.')
        destination = self.import_destination()
        self._current(generation)
        return recording_at(candidates[0], destination)

    def cleanup_source(self, meeting_id, run_id):
        meeting, run, generation = self._selection(meeting_id, run_id)
        if run is None or _identity(self._check(meeting.path)) != meeting.identity:
            raise LibraryError('stale_id', 'The meeting changed. Refresh and select it again.')
        destination = self.import_destination()
        destination.capture(meeting.path)
        destination.capture(run.path.parent)
        destination.capture(run.path)
        for name in ('transcript.json', 'transcript.md', 'transcript.html', '.assemblyai.json'):
            with self._open(run.path / name):
                pass
        self._current(generation)
        return run.path / 'transcript.json', destination

    def result_selection(self, path):
        snapshot = self.scan()
        for row in snapshot['meetings']:
            meeting = self._meetings[row['id']]
            if path.parent == meeting.path:
                return {**snapshot, 'selected_meeting': row['id'], 'selected_run': None}
            for run in meeting.runs:
                if path.parent == run.path:
                    return {**snapshot, 'selected_meeting': row['id'], 'selected_run': run.identifier}
        raise LibraryError('unavailable_source', 'The saved result is unavailable. Refresh the original library.')

    def digest_source(self, meeting_id: str, run_id: str) -> Source:
        meeting, run, generation = self._selection(meeting_id, run_id)
        if run is None:
            raise DigestError("Select a readable saved transcript version.")
        description = self._run(run.path, include_text=True)
        if not description or description["canonical"] is None:
            raise DigestError("This saved version has no readable structured transcript.")
        with self._open(run.path / "transcript.json") as stream:
            data = stream.read(MAX_SOURCE + 1)
        if len(data) > MAX_SOURCE:
            raise DigestError("This transcript is too large to summarize in one request.")
        transcript = parse_canonical(json.loads(data), require_speaker_names=not description['status'].startswith('Historical'))
        source = Source(meeting.path, run.path, meeting.identity, _identity(self._check(run.path)),
                        meeting.title, meeting.added, run.path.name, sha256(data), transcript)
        with self._lock:
            self._current(generation)
        return source

    def _digest_target(self, source: Source) -> None:
        if (_identity(self._check(source.meeting_path)) != source.meeting_identity or
                _identity(self._check(source.run_path)) != source.run_identity):
            raise DigestError("The original summary destination changed. Refresh and try again.")

    def publish_digest(self, source: Source, value: dict) -> dict:
        """Write only to the captured meeting, with verified handles and reload."""
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        parse_record(payload)
        with self._lock:
            self._digest_target(source)
            parent = source.meeting_path / "summaries"
            try:
                parent.mkdir(mode=0o700)
            except FileExistsError:
                pass
            self._check(parent)
            verify_private(parent, directory=True)
            target = parent / value["id"]
            target.mkdir(mode=0o700)  # Exclusive ownership; never replace another generation.
            identity = _identity(self._check(target))
            temporary = None
            try:
                verify_private(target, directory=True)
                descriptor, name = tempfile.mkstemp(prefix=".summary-", suffix=".tmp", dir=target)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as stream:
                    if os.name != "nt":
                        os.fchmod(stream.fileno(), 0o600)
                    # No sensitive byte is written until the actual opened target
                    # and every ancestor have passed the Library boundary.
                    if os.path.normcase(str(_handle_path(stream.fileno()))) != os.path.normcase(str(temporary)):
                        raise DigestError("Summary destination changed.")
                    if _identity(self._check(temporary)) != _identity(os.fstat(stream.fileno())):
                        raise DigestError("Summary destination changed.")
                    self._digest_target(source)
                    verify_private(temporary, directory=False)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _identity(self._check(target)) != identity:
                    raise DigestError("Summary destination changed.")
                destination = target / "summary.json"
                os.replace(temporary, destination)
                verify_private(destination, directory=False)
                with self._open(destination) as stream:
                    saved = parse_record(stream.read(MAX_RECORD + 1))
                if saved != value:
                    raise DigestError("The saved summary could not be verified.")
                return saved
            except BaseException:
                # Only the exclusive task directory may be removed. Never follow
                # a replaced/reparse path during failure cleanup.
                try:
                    if _identity(self._check(target)) == identity:
                        shutil.rmtree(target)
                except (OSError, LibraryError):
                    pass
                raise

    def summary_history(self, meeting_id: str, run_id: str, summary_id: str | None = None) -> dict:
        meeting, run, generation = self._selection(meeting_id, run_id)
        if run is None:
            return {"summaries": [], "summary": None}
        records = []
        warning = False
        parent = meeting.path / "summaries"
        try:
            candidates = self._names(parent)
        except FileNotFoundError:
            candidates = []
        for folder in candidates:
            if not GENERATION.fullmatch(folder.name):
                continue
            try:
                with self._open(folder / "summary.json") as stream:
                    value = parse_record(stream.read(MAX_RECORD + 1))
                if value["id"] != folder.name:
                    raise DigestError("Summary identity changed.")
                if value["source_run"] == run.path.name:
                    records.append(value)
            except (OSError, ValueError):
                warning = True
        records.sort(key=lambda item: (item["generated_at"], item["id"]), reverse=True)
        selected = next((item for item in records if item["id"] == summary_id), None) if summary_id else next(iter(records), None)
        if summary_id and selected is None:
            raise DigestError("This summary is unavailable. Refresh to try again.")
        current_sha = None
        try:
            with self._open(run.path / "transcript.json") as stream:
                data = stream.read(MAX_SOURCE + 1)
            if len(data) <= MAX_SOURCE:
                current_sha = sha256(data)
        except OSError:
            pass
        if selected:
            selected = {**selected, "earlier_revision": selected["source_sha256"] != current_sha}
        with self._lock:
            self._current(generation)
        headers = [{k: item[k] for k in ("id", "generated_at", "provider", "source_run", "source_sha256")} for item in records]
        return {"summaries": headers, "summary": selected,
                "summary_warning": "Some saved summaries are unavailable." if warning else ""}
