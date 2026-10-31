from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
        self.assertTrue(result['canonical']['segments'])
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
        self.assertTrue(result['canonical']['segments'])
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

    def test_mac_entry_branch_reexecutes_with_clean_process_environment(self):
        # Select the Mac bootstrap branch, then cross a real exec/process
        # boundary into a tiny observer before any GUI or library is initialized.
        script = '''
import sys,runpy
sys.platform='darwin'
sys.orig_argv=[sys.executable,'-c',"import os; print('clean' if 'ASSEMBLYAI_API_KEY' not in os.environ else 'leaked')"]
runpy.run_path('desktop/main.py',run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                env=dict(os.environ, ASSEMBLYAI_API_KEY='synthetic-sentinel'),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'clean')


if __name__ == '__main__':
    unittest.main()
