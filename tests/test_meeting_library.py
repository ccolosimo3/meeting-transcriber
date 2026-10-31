from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import meeting_library  # noqa: E402
from meeting_library import Library, LibraryError  # noqa: E402
from transcript_run import classify  # noqa: E402
from transcribe_assemblyai import AssemblyAIAdapter  # noqa: E402
from tests.test_assemblyai_adapter import completed_response, write_published_run  # noqa: E402


class LocalPublication(AssemblyAIAdapter):
    """Keep publication real; replace only the provider transport in this fixture."""

    def _upload(self, audio):
        return {"upload_url": "https://example.invalid/synthetic"}

    def _json_request(self, *args, **kwargs):
        return {"id": "job-123", "status": "queued"}

    def _poll(self):
        return completed_response()

    def _delete(self):
        return False


class MeetingLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "library"
        self.root.mkdir()

    def bundle(self, name="20261031-090000-planning", *, media=True):
        bundle = self.root / name
        bundle.mkdir()
        if media:
            (bundle / "recording.wav").write_bytes(b"synthetic-source-audio")
        return bundle

    def published_run(self, bundle, label="20261031-100000", *, confirmed=True):
        source = write_published_run(bundle / "transcripts" / label, deletion={"confirmed": confirmed})
        return source.parent

    def row(self, library):
        return library.scan()["meetings"][0]

    def load(self, library, row):
        return library.load(row["id"], row["default_run_id"])

    def hashes(self, root):
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()}

    def link_directory(self, target, link):
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            link.symlink_to(target, target_is_directory=True)

    def unlink_directory(self, link):
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()

    def test_producer_publication_loads_without_mutating_and_copy_has_no_original_path_dependency(self):
        bundle = self.bundle()
        output = bundle / "transcripts" / "20261031-100000"
        output.mkdir(parents=True)
        adapter = LocalPublication(api_key="synthetic-fixture", stderr=io.StringIO())
        adapter.run(bundle / "recording.wav", output, "transcript", None)
        before = self.hashes(self.root)
        library = Library(self.root)
        row = self.row(library)
        loaded = self.load(library, row)
        self.assertEqual(loaded["status"], "Transcript ready; remote cleanup unconfirmed")
        self.assertEqual(loaded["canonical"]["text"], completed_response()["text"])
        self.assertEqual(row["title"], "Planning")
        self.assertIn("October 31, 2026", row["added"])
        with library.open_media(row["id"]) as stream:
            self.assertEqual(stream.read(), b"synthetic-source-audio")
        with library.open_export(row["id"], row["default_run_id"]) as stream:
            self.assertEqual(stream.read(), (output / "transcript.html").read_bytes())
        self.assertEqual(before, self.hashes(self.root))
        copied_root = self.base / "other-library"
        shutil.copytree(self.root, copied_root)
        bundle.rename(self.base / "moved-original")
        copied = Library(copied_root)
        copied_row = self.row(copied)
        self.assertEqual(self.load(copied, copied_row)["canonical"], loaded["canonical"])
        with copied.open_media(copied_row["id"]) as stream:
            stream.seek(10)
            self.assertEqual(stream.read(), b"source-audio")
        self.assertEqual(before, self.hashes(copied_root))

    def test_classification_order_fallback_and_sibling_isolation(self):
        bundle = self.bundle()
        old = self.published_run(bundle, "9-old", confirmed=True)
        current = self.published_run(bundle, "z-current", confirmed=False)
        unpublished = self.published_run(bundle, "x-unpublished")
        receipt = unpublished / ".assemblyai.json"
        unpublished_receipt = json.loads(receipt.read_text(encoding="utf-8"))
        unpublished_receipt["state"] = "submitted"
        receipt.write_text(json.dumps(unpublished_receipt), encoding="utf-8")
        wrong_encoding = self.published_run(bundle, "w-utf16")
        json_path = wrong_encoding / "transcript.json"
        json_path.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-16")
        historical = self.published_run(bundle, "é-historical")
        (historical / ".assemblyai.json").unlink()
        (historical / "transcript.json").write_text("malformed json", encoding="utf-8")
        (historical / "transcript.assemblyai.json").write_text(
            json.dumps({"deletion": {"confirmed": False}, "transcript_id": "private-receipt-id"}), encoding="utf-8")
        broken = self.bundle("20261031-100000-broken")
        broken_run = self.published_run(broken)
        (broken_run / "transcript.json").write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        (self.root / "unrelated-empty").mkdir()
        library = Library(self.root)
        for run in (old, current, unpublished, wrong_encoding, historical):
            self.assertEqual(classify(run), classify(run, opener=library._open))
        result = library.scan()
        self.assertEqual([row["title"] for row in result["meetings"]], ["Broken", "Planning"])
        row = result["meetings"][1]
        self.assertEqual([run["label"] for run in row["runs"]], ["é-historical", "z-current", "9-old"])
        self.assertEqual(row["default_run_id"], row["runs"][0]["id"])
        historical_view = self.load(library, row)
        self.assertEqual(historical_view["status"], "Saved HTML available; remote cleanup unconfirmed")
        self.assertIsNone(historical_view["canonical"])
        self.assertEqual(historical_view["exports"], {"html": True})
        self.assertEqual(library.load(row["id"], row["runs"][-1]["id"])["status"], "Transcript ready")
        self.assertEqual(classify(old)[0], "clean")
        self.assertEqual(result["meetings"][0]["status"], "Transcript incomplete or unavailable")
        self.assertNotIn("private-receipt-id", json.dumps(result))
        self.assertNotIn("private-receipt-id", json.dumps(historical_view))

    def test_recording_only_partial_missing_and_ambiguous_media(self):
        partial = self.bundle("20261031-080000-partial", media=False)
        (partial / "recording.partial.wav").write_bytes(b"unfinished")
        recording = self.bundle("20261031-090000-recording")
        missing = self.bundle("20261031-100000-missing", media=False)
        self.published_run(missing)
        ambiguous = self.bundle("20261031-110000-ambiguous")
        (ambiguous / "recording").write_bytes(b"second candidate")
        self.published_run(ambiguous)
        library = Library(self.root)
        rows = {row["title"]: row for row in library.scan()["meetings"]}
        self.assertEqual(rows["Partial"]["status"], "Unfinished recording")
        self.assertEqual(rows["Recording"]["status"], "Recording saved")
        loaded = self.load(library, rows["Recording"])
        self.assertIsNone(loaded["canonical"])
        self.assertEqual(loaded["media_state"], "ready")
        self.assertEqual(loaded["media_name"], "recording.wav")
        self.assertEqual(self.load(library, rows["Missing"])["media_state"], "missing")
        self.assertEqual(self.load(library, rows["Ambiguous"])["media_state"], "ambiguous")
        for title in ("Partial", "Missing", "Ambiguous"):
            with self.assertRaises(LibraryError):
                library.open_media(rows[title]["id"])
        self.assertEqual(Library().scan(), {"library": "", "meetings": [], "errors": []})

    def test_canonical_order_duplicate_ids_gaps_and_historical_names_are_preserved(self):
        bundle = self.bundle()
        run = self.published_run(bundle)
        payload = json.loads((run / "transcript.json").read_text(encoding="utf-8"))
        segments = []
        for identifier, start, end in ((7, 3, 4), (7, 0, 1), (1, 0.5, 2)):
            segment = copy.deepcopy(payload["segments"][0])
            segment.update(id=identifier, start=start, end=end)
            segments.append(segment)
        payload["segments"] = segments
        del payload["speaker_names"]
        (run / ".assemblyai.json").unlink()
        (run / "transcript.json").write_text(json.dumps(payload), encoding="utf-8")
        library = Library(self.root)
        loaded = self.load(library, self.row(library))
        self.assertEqual(loaded["status"], "Historical transcript")
        self.assertEqual(loaded["canonical"]["segments"], segments)
        self.assertEqual(loaded["canonical"]["speaker_names"], {})

    def test_ids_revalidate_without_falling_back_to_another_meeting_or_run(self):
        first = self.bundle()
        first_run = self.published_run(first)
        second = self.bundle("20261031-100000-second")
        self.published_run(second)
        library = Library(self.root)
        rows = library.scan()["meetings"]
        first_row, second_row = rows[1], rows[0]
        with self.assertRaises(LibraryError):
            library.load(first_row["id"], second_row["default_run_id"])
        with self.assertRaises(LibraryError):
            library.load(first_row["id"], "../../transcript.json")
        self.assertEqual(library.scan()["meetings"][1]["id"], first_row["id"])
        self.assertIsNotNone(self.load(library, first_row)["canonical"])
        replacement = first_run / "replacement.json"
        replacement.write_bytes((first_run / "transcript.json").read_bytes())
        replacement.replace(first_run / "transcript.json")
        changed = library.scan()["meetings"][1]
        self.assertEqual(changed["id"], first_row["id"])
        self.assertNotEqual(changed["default_run_id"], first_row["default_run_id"])
        with self.assertRaises(LibraryError):
            self.load(library, first_row)
        other_session = Library(self.root)
        other_session.scan()
        with self.assertRaises(LibraryError):
            other_session.load(changed["id"], changed["default_run_id"])
        shutil.rmtree(first)
        library.scan()
        with self.assertRaises(LibraryError):
            library.load(changed["id"])

    def test_malformed_or_removed_selected_run_does_not_fall_back_to_an_older_run(self):
        bundle = self.bundle()
        self.published_run(bundle, "older")
        selected = self.published_run(bundle, "selected")
        library = Library(self.root)
        row = self.row(library)
        (selected / "transcript.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(LibraryError) as malformed:
            self.load(library, row)
        self.assertEqual(malformed.exception.code, "malformed_transcript")
        shutil.rmtree(selected)
        with self.assertRaises(LibraryError) as missing:
            self.load(library, row)
        self.assertEqual(missing.exception.code, "unavailable_source")
        self.assertNotIn(str(selected), str(missing.exception))

    def test_linked_bundle_transcripts_run_and_artifacts_never_load_outside_content(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "recording.wav").write_bytes(b"outside-secret")
        write_published_run(outside / "transcripts" / "run", deletion={"confirmed": True})
        for boundary in ("bundle", "transcripts", "run"):
            with self.subTest(boundary=boundary):
                bundle = self.bundle(f"20261031-090000-{boundary}")
                run = self.published_run(bundle)
                library = Library(self.root)
                row = next(row for row in library.scan()["meetings"] if row["title"] == boundary.capitalize())
                original = {"bundle": bundle, "transcripts": bundle / "transcripts", "run": run}[boundary]
                target = {"bundle": outside, "transcripts": outside / "transcripts", "run": outside / "transcripts" / "run"}[boundary]
                backup = original.with_name(original.name + "-backup")
                original.rename(backup)
                self.link_directory(target, original)
                try:
                    with self.assertRaises(LibraryError):
                        self.load(library, row)
                    with self.assertRaises(LibraryError):
                        library.open_export(row["id"], row["default_run_id"])
                finally:
                    self.unlink_directory(original)
                    backup.rename(original)
    def test_linked_artifact_is_rejected_and_linked_media_preserves_readable_text(self):
        outside = self.base / "outside"
        write_published_run(outside / "transcripts" / "run", deletion={"confirmed": True})
        bundle = self.bundle()
        run = self.published_run(bundle)
        library = Library(self.root)
        row = self.row(library)
        artifact = run / "transcript.json"
        original = artifact.read_bytes()
        artifact.unlink()
        try:
            artifact.symlink_to(outside / "transcripts" / "run" / "transcript.json")
        except OSError:
            self.skipTest("Host does not permit symbolic-link creation; directory junction boundaries are tested separately.")
        with self.assertRaises(LibraryError):
            self.load(library, row)
        artifact.unlink()
        artifact.write_bytes(original)
        outside_audio = outside / "recording.wav"
        outside_audio.write_bytes(b"outside-secret")
        media = bundle / "recording.wav"
        media.unlink()
        media.symlink_to(outside_audio)
        loaded = self.load(library, row)
        self.assertIsNotNone(loaded["canonical"])
        self.assertEqual(loaded["media_state"], "missing")
        self.assertEqual(loaded["media_error"]["code"], "unsafe_path")
        with self.assertRaises(LibraryError):
            library.open_media(row["id"])

    def test_replacement_during_open_is_rejected_before_outside_bytes_are_returned(self):
        bundle = self.bundle()
        library = Library(self.root)
        row = self.row(library)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "recording.wav").write_bytes(b"outside-secret")
        backup = self.root / "original-backup"
        real_open = os.open
        swapped = False

        def swap_then_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path) == bundle / "recording.wav":
                swapped = True
                bundle.rename(backup)
                self.link_directory(outside, bundle)
            return real_open(path, flags, *args, **kwargs)

        try:
            with mock.patch.object(meeting_library.os, "open", side_effect=swap_then_open):
                with self.assertRaises(LibraryError) as caught:
                    library.open_media(row["id"])
            self.assertEqual(caught.exception.code, "unsafe_path")
            self.assertTrue(swapped)
        finally:
            if swapped:
                self.unlink_directory(bundle)
                backup.rename(bundle)

    def test_disappearance_and_access_errors_are_bounded_and_not_reported_as_empty_success(self):
        bundle = self.bundle()
        library = Library(self.root)
        row = self.row(library)
        (bundle / "recording.wav").unlink()
        self.assertEqual(self.load(library, row)["media_state"], "missing")
        with mock.patch.object(library, "_names", side_effect=PermissionError("private/path/raw-error")):
            denied = library.scan()
        self.assertEqual(denied["meetings"], [])
        self.assertEqual(denied["errors"][0]["code"], "access_denied")
        self.assertNotIn("private/path/raw-error", json.dumps(denied))
        self.root.rename(self.base / "moved-library")
        missing = library.scan()
        self.assertEqual(missing["errors"][0]["code"], "unavailable_library")


if __name__ == "__main__":
    unittest.main()
