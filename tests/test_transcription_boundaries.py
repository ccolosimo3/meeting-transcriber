"""Filesystem selection and disclosure/recovery boundaries through real callers."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_transcription import ROOT, RecordingFixture
from tests.test_assemblyai_adapter import running_server, write_published_run, completed_response, ScenarioHandler
from desktop.main import Bridge
from desktop.server import ReaderServer
from meeting_library import Library
from transcription import select_or_import, cleanup_published_run
from transcription_config import store_key
import transcribe_assemblyai as provider


class BoundaryTests(RecordingFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.environment = patch.dict(os.environ, {'HOME': str(self.root / 'home'), 'LOCALAPPDATA': str(self.root / 'home/AppData/Local'), 'MEETING_DATA_DIR': str(self.library), 'MEETING_CONFIG_DIR': str(self.root / 'config'), 'MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL': '0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        store_key('synthetic-only')
        self.recording = select_or_import(self.source, self.library)
        self.server = ReaderServer(ROOT / 'desktop/assets')
        self.server.start()
        self.addCleanup(self.server.close)

    def bridge(self):
        bridge = Bridge(self.server, lambda: None)
        self.addCleanup(bridge._close)
        return bridge

    def terminal(self, bridge, job):
        if 'id' not in job:
            return job
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = bridge.operation_status(job['id'])
            if state['state'] != 'running':
                return state
            time.sleep(.01)
        self.fail('operation did not finish')

    def test_cleanup_rejects_replaced_selected_run_and_receipt(self):
        for when in ('before_capability', 'after_capability', 'before_delete', 'during_delete'):
            with self.subTest(when=when), running_server({}) as (http, base), patch.dict(os.environ, {'MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE': base}):
                run = self.recording.path.parent / 'transcripts' / when
                canonical = write_published_run(run, deletion={'confirmed': False})
                bridge = self.bridge()
                row = bridge.refresh_library()['meetings'][0]
                run_id = next(item['id'] for item in row['runs'] if item['label'] == when)
                def replace():
                    if when == 'before_capability':
                        run.rename(self.root / ('retired-' + when))
                        write_published_run(run, deletion={'confirmed': False}, receipt_overrides={'transcript_id': 'different-synthetic-transcript'})
                    else:
                        value = json.loads((run / '.assemblyai.json').read_text())
                        value['transcript_id'] = 'different-synthetic-transcript'
                        provider.atomic_write_json(run / '.assemblyai.json', value)
                if when == 'before_capability':
                    replace()
                    result = self.terminal(bridge, bridge.retry_remote_cleanup(row['id'], run_id))
                else:
                    def intervening(path, destination, **kwargs):
                        if when == 'after_capability':
                            replace()
                        elif when == 'before_delete':
                            original = kwargs['on_progress']
                            def progress(phase):
                                replace()
                                original(phase)
                            kwargs['on_progress'] = progress
                        return cleanup_published_run(path, destination, **kwargs)
                    original_delete = ScenarioHandler.do_DELETE
                    def deleted(handler):
                        if when == 'during_delete': replace()
                        original_delete(handler)
                    with patch('desktop.main.cleanup_published_run', side_effect=intervening), patch.object(ScenarioHandler, 'do_DELETE', deleted):
                        result = self.terminal(bridge, bridge.retry_remote_cleanup(row['id'], run_id))
                self.assertFalse(result.get('state') == 'ready', result)
                self.assertEqual([(r['method'], r['path']) for r in http.requests], [('DELETE', '/v2/transcript/job-123')] if when == 'during_delete' else [], 'A changed selection must not authorize a new target or receipt overwrite')
                receipt = json.loads((run / '.assemblyai.json').read_text())
                self.assertEqual(receipt['transcript_id'], 'different-synthetic-transcript')
                self.assertFalse(receipt['deletion']['confirmed'])

    def test_meeting_replaced_during_media_capture_cannot_upload_new_audio(self):
        bridge = self.bridge()
        row = bridge.refresh_library()['meetings'][0]
        original = bridge._library._media
        calls = 0
        def swapped(meeting):
            nonlocal calls
            candidates = original(meeting)
            calls += 1
            if calls == 2:  # First is load; second is the recording capability.
                meeting.path.rename(self.root / 'original-meeting')
                meeting.path.mkdir(mode=0o700)
                (meeting.path / 'recording.wav').write_bytes(self.original + b'replacement')
            return candidates
        with running_server({}) as (http, base), patch.dict(os.environ, {'MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE': base}), patch.object(bridge._library, '_media', side_effect=swapped):
            result = self.terminal(bridge, bridge.start_transcription(row['id']))
            self.assertFalse(result.get('state') == 'ready', result)
            self.assertEqual(len(http.requests), 0, 'Replacement audio must not be disclosed')

    def test_receipt_write_failure_preserves_pre_disclosure_uncertainty(self):
        for when in ('initial', 'during_upload', 'after_upload'):
            with self.subTest(when=when), running_server({}) as (http, base), patch.dict(os.environ, {'MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE': base, 'MEETING_TRANSCRIBER_RUN_ID': when}):
                bridge = self.bridge()
                row = bridge.refresh_library()['meetings'][0]
                original_write = provider.atomic_write_json
                original_send = provider.AssemblyAIAdapter._send_stream
                original_record = ScenarioHandler._record
                original_respond = ScenarioHandler._respond
                uploaded = threading.Event()
                fail = False
                def observed(handler, body=b''):
                    original_record(handler, body)
                    if handler.path == '/v2/upload': uploaded.set()
                def respond(handler, *args):
                    try:
                        original_respond(handler, *args)
                    except BrokenPipeError:
                        if when != 'during_upload': raise
                def send(connection, stream):
                    nonlocal fail
                    original_send(connection, stream)
                    if when == 'during_upload':
                        self.assertTrue(uploaded.wait(5))
                        fail = True
                        raise provider.ProviderTransportError('synthetic interrupted upload')
                def write(path, value):
                    if when == 'initial' or fail or when == 'after_upload' and value.get('state') == 'upload_succeeded':
                        raise OSError('synthetic receipt write failure')
                    return original_write(path, value)
                with patch.object(provider, 'atomic_write_json', side_effect=write), patch.object(provider.AssemblyAIAdapter, '_send_stream', side_effect=send), patch.object(ScenarioHandler, '_record', observed), patch.object(ScenarioHandler, '_respond', respond):
                    status = self.terminal(bridge, bridge.start_transcription(row['id']))
                self.assertEqual(status['state'], 'stopped')
                if when == 'initial':
                    self.assertEqual(len(http.requests), 0, 'A receipt must persist before disclosure')
                else:
                    self.assertEqual(status['code'], 'support_before_retry')
                    reopened = self.bridge()
                    row = reopened.refresh_library()['meetings'][0]
                    self.assertIn('before transcribing again', reopened.load_run(row['id'], None)['transcription_warning'])
                    self.assertIn('error', reopened.start_transcription(row['id']))
                    self.assertEqual([request['path'] for request in http.requests], ['/v2/upload'])
                # Keep each subcase independent while retaining its actual outcome assertion.
                import shutil
                shutil.rmtree(self.recording.path.parent / 'transcripts')

    def test_model_mismatch_remains_support_before_retry_after_reload(self):
        response = completed_response()
        response['speech_model_used'] = 'different-model'
        with running_server({'poll_responses': [response]}) as (http, base), patch.dict(os.environ, {'MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE': base}):
            bridge = self.bridge()
            row = bridge.refresh_library()['meetings'][0]
            status = self.terminal(bridge, bridge.start_transcription(row['id']))
            self.assertEqual(status['code'], 'support_before_retry')
            reopened = self.bridge()
            row = reopened.refresh_library()['meetings'][0]
            warning = reopened.load_run(row['id'], None)['transcription_warning']
            self.assertIn('unexpected AssemblyAI model', warning)
            self.assertIn('before transcribing again', warning)
            self.assertIn('error', reopened.start_transcription(row['id']))
            self.assertEqual(len([r for r in http.requests if r['path'] == '/v2/upload']), 1)

    def test_internal_shell_accepts_relative_output_root(self):
        with running_server({}) as (http, base):
            environment = dict(os.environ, MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE=base, ASSEMBLYAI_API_KEY='synthetic-only')
            result = subprocess.run([str(ROOT / 'bin/transcribe-meeting'), str(self.source), 'relative output'], cwd=self.root, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            canonical = Path(result.stdout.strip())
            self.assertTrue(canonical.is_relative_to(self.root / 'relative output'))
            self.assertTrue(canonical.is_file())
            self.assertTrue(http.requests)

if __name__ == '__main__': unittest.main()
