"""Shared operations cross the real existing HTTP adapter and reload Library."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from meeting_library import Library
from private_files import Directory
from transcription import import_recording, transcribe, cleanup_published_run, OperationError
from tests.test_assemblyai_adapter import running_server

class RecordingFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.library = self.root / 'library'
        self.library.mkdir(mode=0o700)
        self.source = self.root / 'Long synthetic planning meeting.wav'
        with wave.open(str(self.source), 'wb') as audio:
            audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 8000)
        self.original = self.source.read_bytes()

class TranscriptionTests(RecordingFixture, unittest.TestCase):
    def test_import_transcribe_reload_cleanup_at_http_boundary(self):
        phases = []
        with running_server({'delete_statuses': [403, 200]}) as (server, base), patch.dict(os.environ, {'MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE': base, 'MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL': '0'}):
            recording = import_recording(self.source, Directory(self.library))
            self.assertFalse(server.requests)  # Import is always local.
            result = transcribe(recording, 2, key_override='synthetic-only', on_progress=phases.append)
            self.assertEqual(result.exit_code, 3)
            fresh = Library(self.library)
            row = fresh.scan()['meetings'][0]
            self.assertEqual(row['title'], self.source.stem)
            loaded = fresh.load(row['id'], row['default_run_id'])
            self.assertIn('remote cleanup unconfirmed', loaded['status'])
            self.assertEqual(loaded['canonical']['text'], 'First. Second exactly as returned.')
            self.assertEqual({p.name for p in result.canonical_path.parent.iterdir()}, {'transcript.json', 'transcript.md', 'transcript.html', '.assemblyai.json'})
            self.assertEqual(phases, ['checking', 'uploading', 'transcribing', 'saving', 'deleting'])
            before = len(server.requests)
            cleaned = cleanup_published_run(result.canonical_path, recording.destination, key_override='synthetic-only')
            self.assertTrue(cleaned.deletion_confirmed)
            self.assertEqual([request['method'] for request in server.requests[before:]], ['DELETE'])
            self.assertNotIn('unconfirmed', Library(self.library).scan()['meetings'][0]['status'])
        self.assertEqual(self.source.read_bytes(), self.original)
        for path in self.library.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_import_collision_and_changed_destination_preserve_others(self):
        with patch.dict(os.environ, {'MEETING_TRANSCRIBER_BUNDLE_ID': 'fixed'}):
            destination = Directory(self.library)
            recording = import_recording(self.source, destination)
            with self.assertRaises(OperationError):
                import_recording(self.source, destination)
            moved = self.library.with_name('moved')
            self.library.rename(moved)
            self.library.mkdir()
            (self.library / 'preserve').write_text('unowned')
            with self.assertRaises(ValueError):
                recording.check()
            self.assertEqual((self.library / 'preserve').read_text(), 'unowned')

    def test_failed_copy_removes_only_exclusive_incomplete_bundle(self):
        with patch('transcription.shutil.copyfileobj', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                import_recording(self.source, Directory(self.library))
        self.assertEqual(list(self.library.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), self.original)


class CallerTests(RecordingFixture, unittest.TestCase):
    def test_public_cli_uses_same_operation_and_retains_speaker_count(self):
        import json
        import subprocess
        with running_server({}) as (server, base):
            env = {key: value for key, value in os.environ.items() if not key.startswith('MEETING_TRANSCRIBER_') and key != 'ASSEMBLYAI_API_KEY'}
            env.update(HOME=str(self.root / 'home'), MEETING_DATA_DIR=str(self.library), MEETING_NOTIFICATIONS='0', ASSEMBLYAI_API_KEY='synthetic-only', MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE=base, MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL='0')
            result = subprocess.run([str(ROOT / 'bin/meeting'), 'transcribe', str(self.source), '--speakers', '2', '--no-open', '--skip-names'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertTrue(Path(result.stdout.strip()).is_file())
            submitted = next(request for request in server.requests if request['path'] == '/v2/transcript' and request['method'] == 'POST')
            self.assertEqual(json.loads(submitted['body'])['speakers_expected'], 2)
            self.assertEqual(self.source.read_bytes(), self.original)

    def test_unknown_desktop_failure_stays_blocked_after_library_reload(self):
        import time
        from desktop.main import Bridge
        from desktop.server import ReaderServer
        from transcription_config import store_key
        from transcription import select_or_import
        recording = select_or_import(self.source, self.library)
        server = ReaderServer(ROOT / 'desktop/assets')
        server.start()
        self.addCleanup(server.close)
        with running_server({'upload_status': 500}) as (provider, base), patch.dict(os.environ, {'HOME': str(self.root / 'home'), 'MEETING_DATA_DIR': str(self.library), 'MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE': base, 'MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL': '0'}):
            store_key('synthetic-only')
            bridge = Bridge(server, lambda: None)
            self.addCleanup(bridge._close)
            row = bridge.refresh_library()['meetings'][0]
            job = bridge.start_transcription(row['id'])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = bridge.operation_status(job['id'])
                if status['state'] != 'running': break
                time.sleep(.02)
            self.assertEqual(status['state'], 'stopped')
            self.assertEqual(status['code'], 'support_before_retry')
            self.assertIn('Contact AssemblyAI support', status['action'])
            reopened = Bridge(server, lambda: None)
            self.addCleanup(reopened._close)
            row = reopened.refresh_library()['meetings'][0]
            self.assertIn('Contact AssemblyAI support', reopened.load_run(row['id'], None)['transcription_warning'])
            self.assertIn('before transcribing again', reopened.start_transcription(row['id'])['error'])
            self.assertEqual([request['method'] for request in provider.requests], ['POST'])
            self.assertTrue(recording.path.is_file())


if __name__ == '__main__':
    unittest.main()
