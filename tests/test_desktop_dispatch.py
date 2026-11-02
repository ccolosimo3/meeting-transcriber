"""Exercise the pinned real dispatcher behind the native-message authorization."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.main import Bridge
from desktop.native import _dispatch
from desktop.server import ReaderServer
from meeting_library import Library
from tests.test_assemblyai_adapter import write_published_run


class Window:
    def __init__(self, api):
        self._js_api = api
        self._functions = {}
        self._callbacks = {}
        self.finished = threading.Event()

    def evaluate_js(self, _):
        self.finished.set()


@unittest.skipUnless(importlib.util.find_spec('webview'), 'install the desktop extra for real bridge dispatch')
class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'library'
        bundle = self.library / '20261031-090000-review'
        write_published_run(bundle / 'transcripts' / 'run-1', deletion={'confirmed': True})
        (bundle / 'recording.wav').write_bytes(b'synthetic audio')
        self.server = ReaderServer(Path(__file__).resolve().parents[1] / 'desktop' / 'assets')
        self.addCleanup(self.server.invalidate)
        self.addCleanup(self.server.http.server_close)
        self.opened = []
        self.picks = []

        def picker():
            self.picks.append(True)
            return str(self.library)

        self.bridge = Bridge(self.server, picker, lambda url: self.opened.append(url) or True)
        self.bridge._library = Library(self.library)
        self.window = Window(self.bridge)

    def dispatch(self, name, params):
        self.window.finished.clear()
        _dispatch(self.window, name, params, '12345')
        self.assertTrue(self.window.finished.wait(5), name)

    def test_four_authorized_actions_reach_actual_owners(self):
        self.dispatch('choose_library', [])
        self.assertEqual(self.picks, [True])
        row = self.bridge._library.scan()['meetings'][0]
        media = (self.library / '20261031-090000-review' / 'recording.wav').open('rb')
        self.server.bind(media, 'recording.wav')
        self.dispatch('refresh_library', [])
        self.assertTrue(media.closed)
        self.dispatch('load_run', [row['id'], row['default_run_id']])
        self.assertTrue(self.server.resources)
        self.dispatch('open_saved_html', [row['id'], row['default_run_id']])
        self.assertEqual(len(self.opened), 1)

    def test_private_nested_and_forged_internal_actions_cannot_mutate_or_open(self):
        outside = self.root / 'outside.txt'
        outside.write_text('preserve', encoding='utf-8')
        for name, params in [
            ('_library.root.__class__.unlink', [str(outside)]),
            ('_library.__init__', [str(self.root)]),
            ('_opener', ['https://example.invalid']),
            ('pywebviewStateUpdate', {'key':'injected','value':'data'}),
            ('pywebviewMoveWindow', [1, 2]),
            ('pywebviewAsyncCallback', 'null'),
            ('load_run', [str(outside), None]),
            ('start_transcription', [str(outside)]),
            ('retry_remote_cleanup', ['safe-id', str(outside)]),
            ('save_transcription_key', ['line\nbreak']),
            ('save_transcription_key', [{'path': str(outside)}]),
            ('import_audio', [str(outside)]),
            ('locate_ffprobe', [str(outside)]),
        ]:
            _dispatch(self.window, name, params, '12345')
        _dispatch(self.window, 'refresh_library', [], 'x"];injected()//')
        self.assertEqual(outside.read_text(encoding='utf-8'), 'preserve')
        self.assertEqual(self.bridge._library.root, self.library.resolve())
        self.assertFalse(self.opened)
        self.assertFalse(self.window.finished.is_set())


if __name__ == '__main__':
    unittest.main()
