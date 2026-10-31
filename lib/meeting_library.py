"""Read-only, session-scoped projection of the existing meeting bundle layout.

Only opaque IDs cross the desktop bridge. Files are opened and verified before
any bytes are read; callers own and close returned media/export handles.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
import sys
import threading
from dataclasses import dataclass
from typing import BinaryIO

from meeting_identity import meeting_identity
from transcript_bundle import canonical_to_json, parse_canonical
from transcript_run import classify


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
                self.root = Path(root).expanduser().resolve(strict=True)
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
        return {"status": status, "canonical": canonical}

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
