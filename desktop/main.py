"""Read-only meeting desktop entry point. No provider code or credential store."""
from __future__ import annotations

# Scrub before importing any renderer or launching children. This slice has no key consumer.
import os
os.environ.pop('ASSEMBLYAI_API_KEY', None)

from pathlib import Path
import sys
import threading
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'desktop'))

from meeting_library import Library, LibraryError
from desktop.server import ReaderServer
from desktop.native import create_session, configure_webview


class Bridge:
    def __init__(self, server, picker, opener=webbrowser.open):
        self._server, self._picker, self._opener = server, picker, opener
        self._lock = threading.RLock()
        root = Path.home() / 'Transcriptions'
        self._library = Library(root) if root.is_dir() else None

    def choose_library(self):
        chosen = self._picker()
        if not chosen:
            return {'cancelled': True}
        with self._lock:
            self._server.invalidate()
            try:
                self._library = Library(Path(chosen))
                return self._library.scan()
            except LibraryError as error:
                return {'error': error.message, 'code': error.code}
            except (OSError, ValueError):
                return {'error': 'The selected library cannot be read.', 'code': 'access_denied'}

    def refresh_library(self):
        with self._lock:
            self._server.invalidate()
            if self._library is None:
                return {'library': '', 'meetings': [], 'errors': []}
            try:
                return self._library.scan()
            except LibraryError as error:
                return {'error': error.message, 'code': error.code}
            except (OSError, ValueError):
                return {'error': 'The library is unavailable. Choose a library or try Refresh.', 'code': 'access_denied'}

    def load_run(self, meeting_id, run_id_or_null):
        with self._lock:
            if self._library is None:
                return {'error': 'Choose a library first.', 'code': 'unknown_id'}
            self._server.invalidate(media_only=True)
            try:
                result = self._library.load(meeting_id, run_id_or_null)
                if result.get('media_state') == 'ready':
                    try:
                        result['media_url'] = self._server.bind(self._library.open_media(meeting_id), result.get('media_name', 'recording.wav'))
                    except (LibraryError, OSError):
                        result['media_state'] = 'missing'
                        result['media_error'] = {'code': 'unavailable_source', 'message': 'Audio changed or is unavailable. Refresh to try again.'}
                return result
            except LibraryError as error:
                return {'error': error.message, 'code': error.code}
            except (OSError, ValueError):
                return {'error': 'This meeting is unavailable or has changed. Refresh the library.', 'code': 'unavailable'}

    def open_saved_html(self, meeting_id, run_id):
        with self._lock:
            if self._library is None:
                return {'opened': False, 'error': 'Choose a library first.'}
            try:
                source = self._library.open_export(meeting_id, run_id)
                url = self._server.bind(source, 'transcript.html', export=True)
                return {'opened': bool(self._opener(url))}
            except (OSError, ValueError):
                return {'opened': False, 'error': 'The saved HTML is unavailable. Refresh the library.'}


def main():
    import webview
    with create_session() as session:
        assets = Path(sys._MEIPASS) / 'desktop' / 'assets' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent / 'assets'
        server = ReaderServer(assets)
        gui = configure_webview(webview, server.document_url, session)
        window = None

        def picker():
            selected = window.create_file_dialog(webview.FileDialog.FOLDER)
            return selected[0] if selected else None

        bridge = Bridge(server, picker)
        window = webview.create_window('Meeting Transcriber', server.document_url, js_api=bridge,
                                       width=1200, height=820, min_size=(400, 500), text_select=True)
        server.start()
        try:
            webview.start(gui=gui, private_mode=True, storage_path=str(session.profile_path), debug=False)
            if session.error:
                raise session.error
        finally:
            server.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # A launch failure must not silently select a legacy renderer.
        message = 'Meeting Transcriber could not start. ' + str(error)
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'Meeting Transcriber', 0x10)
        else:
            print(message, file=sys.stderr)
        raise SystemExit(1)
