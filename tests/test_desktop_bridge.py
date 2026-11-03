from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop.main import Bridge
from desktop.server import ReaderServer
from meeting_library import Library
from tests.test_assemblyai_adapter import write_published_run


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {'MEETING_DATA_DIR': str(self.root),
            'HOME': str(self.root / 'home'), 'USERPROFILE': str(self.root / 'home'),
            'LOCALAPPDATA': str(self.root / 'home/local'), 'APPDATA': str(self.root / 'home/roaming'),
            'MEETING_CONFIG_DIR': str(self.root / 'config'), 'CODEX_HOME': str(self.root / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude')})
        environment.start()
        self.addCleanup(environment.stop)
        bundle = self.root / '20261031-090000-review'
        self.source = write_published_run(bundle / 'transcripts' / 'run-1', deletion={'confirmed': True})
        self.audio = bundle / 'recording.wav'
        self.audio.write_bytes(b'synthetic source bytes')
        self.server = ReaderServer(ROOT / 'desktop' / 'assets')
        self.server.start()
        self.addCleanup(self.server.close)
        self.opened = []
        self.bridge = Bridge(self.server, lambda: None, lambda url: self.opened.append(url) or True)
        self.bridge._library = Library(self.root)

    def read(self, url):
        parsed = urlsplit(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        conn.request('GET', parsed.path)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_real_bridge_load_media_export_cancel_and_refresh(self):
        row = self.bridge.refresh_library()['meetings'][0]
        result = self.bridge.load_run(row['id'], row['default_run_id'])
        self.assertTrue(result['reader_transcript']['passages'])
        self.assertEqual(self.read(result['media_url']), (200, self.audio.read_bytes()))
        self.assertTrue(self.bridge.open_saved_html(row['id'], row['default_run_id'])['opened'])
        self.assertEqual(self.read(self.opened[0]), (200, self.source.with_suffix('.html').read_bytes()))
        self.assertEqual(self.bridge.choose_library(), {'cancelled': True})
        self.assertEqual(self.read(result['media_url'])[0], 200)
        self.bridge.load_run(row['id'], row['default_run_id'])
        self.assertEqual(self.read(result['media_url'])[0], 404)
        self.assertEqual(self.read(self.opened[0])[0], 200)
        self.bridge.refresh_library()
        self.assertEqual(self.read(self.opened[0])[0], 404)

    def test_reader_projection_preserves_full_source_and_reduces_serialized_bytes(self):
        original = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        row = self.bridge.refresh_library()['meetings'][0]
        full = self.bridge._library.load(row['id'], row['default_run_id'])['canonical']
        result = self.bridge.load_run(row['id'], row['default_run_id'])
        self.assertEqual(result['reader_transcript'], {'language': 'en', 'speaker_names': {},
            'passages': [{'start': 0.1, 'end': 0.9, 'text': 'First.', 'speaker': 'SPEAKER_00'}]})
        self.assertNotIn('canonical', result)
        legacy = {**result, 'canonical': full}
        del legacy['reader_transcript']
        self.assertLess(len(json.dumps(result).encode()), len(json.dumps(legacy).encode()))
        self.assertEqual(self.bridge._library.load(row['id'], row['default_run_id'])['canonical'],
                         json.loads(original[self.source]))
        self.assertEqual({p: p.read_bytes() for p in original}, original)

    def test_unused_word_corruption_is_rejected_before_display_projection(self):
        row = self.bridge.refresh_library()['meetings'][0]
        value = json.loads(self.source.read_bytes())
        value['segments'][0]['words'][0]['probability'] = 2
        self.source.write_text(json.dumps(value), encoding='utf-8')
        result = self.bridge.load_run(row['id'], row['default_run_id'])
        self.assertEqual(result['code'], 'malformed_transcript')
        self.assertNotIn('reader_transcript', result)

    def test_historical_names_and_html_fallback_keep_their_display_states(self):
        (self.source.parent / '.assemblyai.json').unlink()
        value = json.loads(self.source.read_bytes())
        del value['speaker_names']
        self.source.write_text(json.dumps(value), encoding='utf-8')
        row = self.bridge.refresh_library()['meetings'][0]
        result = self.bridge.load_run(row['id'], row['default_run_id'])
        self.assertEqual(result['reader_transcript']['speaker_names'], {})
        self.assertEqual(result['reader_transcript']['passages'][0]['text'], 'First.')
        self.source.write_text('{}', encoding='utf-8')
        result = self.bridge.load_run(row['id'], row['default_run_id'])
        self.assertIsNone(result['reader_transcript'])
        self.assertEqual(result['status'], 'Saved HTML available')
        self.assertTrue(self.bridge.open_saved_html(row['id'], row['default_run_id'])['opened'])

    def test_stale_ids_and_disappearing_root_are_bounded_errors(self):
        self.assertEqual(self.bridge.load_run('arbitrary','path')['code'], 'stale_id')
        self.bridge.refresh_library()
        renamed = self.root.with_name(self.root.name+'-moved')
        self.root.rename(renamed)
        try:
            result = self.bridge.refresh_library()
            self.assertEqual(result['errors'][0]['code'], 'unavailable_library')
            self.assertFalse(result['meetings'])
        finally:
            renamed.rename(self.root)

    def test_media_removed_after_loading_text_keeps_canonical_readable(self):
        row = self.bridge.refresh_library()['meetings'][0]
        original = self.bridge._library.open_media

        def removed(meeting_id):
            self.audio.unlink()
            return original(meeting_id)

        with patch.object(self.bridge._library, 'open_media', side_effect=removed):
            result = self.bridge.load_run(row['id'], row['default_run_id'])
        self.assertTrue(result['reader_transcript']['passages'])
        self.assertEqual(result['media_state'], 'missing')
        self.assertNotIn('media_url', result)

    def test_entry_scrubs_sentinel_before_any_opener_child(self):
        script = '''
import os,sys,subprocess,json
assert os.environ.get('ASSEMBLYAI_API_KEY')=='synthetic-sentinel'
from desktop.main import Bridge
from desktop.server import ReaderServer
from meeting_library import Library
from pathlib import Path
server=ReaderServer(Path('desktop/assets'))
def opener(url):
    p=subprocess.run([sys.executable,'-c',"import os;print('ASSEMBLYAI_API_KEY' in os.environ)"],capture_output=True,text=True,check=True)
    assert p.stdout.strip()=='False'
    return True
b=Bridge(server,lambda:None,opener);b._library=Library(Path(sys.argv[1]))
row=b.refresh_library()['meetings'][0]
assert b.open_saved_html(row['id'],row['default_run_id'])['opened']
assert 'ASSEMBLYAI_API_KEY' not in os.environ
server.invalidate()
print('sentinel absent in desktop and opener child')
'''
        env = dict(os.environ, ASSEMBLYAI_API_KEY='synthetic-sentinel')
        result = subprocess.run([sys.executable, '-c', script, str(self.root)], cwd=ROOT,
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sentinel absent', result.stdout)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS exec/process-environment boundary')
    def test_mac_entry_branch_reexecutes_with_clean_process_environment(self):
        # Cross the actual Mac exec boundary before GUI or library initialization.
        observed = self.root / 'exec-observed.txt'
        observer = "import os; from pathlib import Path; Path(" + repr(str(observed)) + ").write_text('clean' if 'ASSEMBLYAI_API_KEY' not in os.environ else 'leaked')"
        script = '''
import sys,runpy
sys.orig_argv=[sys.executable,'-c',OBSERVER]
runpy.run_path('desktop/main.py',run_name='__main__')
'''.replace('OBSERVER', repr(observer))
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                env=dict(os.environ, ASSEMBLYAI_API_KEY='synthetic-sentinel'),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 5
        while not observed.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(observed.read_text(), 'clean')



class TranscriptionBridgeTests(unittest.TestCase):
    def test_explicit_send_busy_navigation_close_and_result_reload(self):
        import threading
        from tests.test_assemblyai_adapter import running_server, ScenarioHandler
        from transcription_config import store_key
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            library = root / 'first'
            library.mkdir(mode=0o700)
            second = root / 'second'
            second.mkdir()
            source = root / 'synthetic.wav'
            import wave
            with wave.open(str(source), 'wb') as audio:
                audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\0\0' * 8000)
            server = ReaderServer(ROOT / 'desktop/assets')
            server.start()
            entered, release = threading.Event(), threading.Event()
            original = ScenarioHandler.do_GET
            def held(handler):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('Fixture release missing')
                original(handler)
            with running_server({}) as (provider, base), patch.dict(os.environ, {'HOME': str(root / 'home'), 'LOCALAPPDATA': str(root / 'home/AppData/Local'), 'MEETING_DATA_DIR': str(library), 'MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE': base, 'MEETING_TRANSCRIBER_ASSEMBLYAI_POLL_INTERVAL': '0'}), patch.object(ScenarioHandler, 'do_GET', held):
                bridge = Bridge(server, lambda: str(second), audio_picker=lambda: str(source))
                try:
                    imported = bridge.import_audio()
                    def terminal(job):
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            state = bridge.operation_status(job['id'])
                            if state['state'] != 'running': return state
                            time.sleep(.02)
                        self.fail('operation did not finish')
                    self.assertEqual(terminal(imported)['state'], 'ready')
                    self.assertFalse(provider.requests)
                    store_key('synthetic-only')
                    self.assertTrue(bridge.transcription_setup()['key_ready'])
                    self.assertFalse(provider.requests)
                    row = bridge.refresh_library()['meetings'][0]
                    job = bridge.start_transcription(row['id'])
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(bridge.start_transcription(row['id'])['code'], 'busy')
                    self.assertFalse(bridge._can_close())
                    self.assertIn('keep this window open', bridge.operation_status(job['id'])['close_warning'])
                    self.assertEqual(bridge.choose_library()['library'], str(second))
                    self.assertFalse(bridge.refresh_library()['meetings'])
                    release.set()
                    self.assertEqual(terminal(job)['state'], 'ready')
                    self.assertTrue(bridge._can_close())
                    self.assertFalse(bridge.refresh_library()['meetings'])  # No selection steal.
                    result = bridge.open_operation_result(job['id'])
                    self.assertEqual(result['library'], str(library))
                    self.assertTrue(result['selected_run'])
                    self.assertTrue(Library(library).scan()['meetings'][0]['runs'])
                    self.assertEqual(len([r for r in provider.requests if r['path'] == '/v2/transcript' and r['method'] == 'POST']), 1)
                finally:
                    release.set()
                    bridge._close()
                    server.close()

if __name__ == '__main__':
    unittest.main()
