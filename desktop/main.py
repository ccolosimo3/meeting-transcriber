"""Meeting import, explicit transcription, reader and subscription-CLI summaries."""
from __future__ import annotations

# Scrub before importing any renderer or launching children.
import os
import sys
_had_inherited_key = 'ASSEMBLYAI_API_KEY' in os.environ
os.environ.pop('ASSEMBLYAI_API_KEY', None)
if _had_inherited_key and sys.platform == 'darwin' and __name__ == '__main__':
    # macOS retains the initial environment for process inspection after
    # unsetenv. Replace the real entry process before any content or children.
    args = [sys.executable, *sys.argv[1:]] if getattr(sys, 'frozen', False) else [sys.executable, *sys.orig_argv[1:]]
    os.execv(sys.executable, args)
del _had_inherited_key

if __name__ == '__main__' and sys.argv[1:] == ['--digest-child']:
    from desktop.digest_child import main as digest_child_main
    digest_child_main()

from pathlib import Path
import threading
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'desktop'))

from meeting_library import Library, LibraryError
from desktop.server import ReaderServer
from desktop.native import create_session, configure_webview
from desktop.digest_job import DigestJob
from meeting_digests import DigestError
from desktop.transcription_job import TranscriptionJob
from transcription import import_recording, transcribe, cleanup_published_run
from transcription_config import readiness, store_key, store_ffprobe
from private_files import private_mkdir


class Bridge:
    def __init__(self, server, picker, opener=webbrowser.open, audio_picker=lambda: None, tool_picker=lambda: None):
        self._server, self._picker, self._opener = server, picker, opener
        self._audio_picker, self._tool_picker = audio_picker, tool_picker
        self._operation = TranscriptionJob()
        self._lock = threading.RLock()
        root = Path(os.environ.get('MEETING_DATA_DIR', str(Path.home() / 'Transcriptions')))
        self._library = Library(root) if root.is_dir() else None
        skill_root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else ROOT
        self._digests = DigestJob(skill_root / 'skills' / 'meeting' / 'SKILL.md')

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
                if run_id_or_null:
                    try:
                        result.update(self._library.summary_history(meeting_id, run_id_or_null))
                    except (OSError, ValueError):
                        result.update(summaries=[], summary=None, summary_warning='Saved summaries are unavailable. Refresh to try again.')
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

    def start_summary(self, meeting_id, run_id, provider):
        with self._lock:
            try:
                if self._library is None:
                    raise DigestError('Choose a library first.')
                library = self._library
                source = library.digest_source(meeting_id, run_id)
                return self._digests.start(source, library.publish_digest, provider, meeting_id, run_id)
            except (OSError, ValueError) as error:
                return {'error': str(error) if isinstance(error, DigestError) else 'This transcript is unavailable. Refresh to try again.'}

    def summary_status(self, job_id):
        try:
            return self._digests.status(job_id)
        except DigestError as error:
            return {'error': str(error)}

    def cancel_summary(self, job_id):
        try:
            return self._digests.cancel(job_id)
        except DigestError as error:
            return {'error': str(error)}

    def summary_history(self, meeting_id, run_id, summary_id):
        with self._lock:
            try:
                if self._library is None:
                    raise DigestError('Choose a library first.')
                return self._library.summary_history(meeting_id, run_id, summary_id)
            except (OSError, ValueError):
                return {'error': 'Saved summaries are unavailable. Refresh to try again.'}

    def transcription_setup(self):
        return readiness()

    def save_transcription_key(self, key):
        try:
            store_key(key)
            return readiness()
        except (OSError, ValueError):
            return {'error': 'The key could not be saved safely. Check the credential location and repair any existing unsafe or malformed file manually.'}

    def locate_ffprobe(self):
        chosen = self._tool_picker()
        if not chosen:
            return {'cancelled': True}
        try:
            store_ffprobe(chosen)
            return readiness()
        except (OSError, ValueError):
            return {'error': 'Choose a regular executable FFprobe file. The existing setting was preserved.'}

    def import_audio(self):
        with self._lock:
            if self._operation.busy():
                return {'error': 'An operation is already in progress.', 'code': 'busy'}
            # Capture the destination before opening the native picker.
            try:
                library = self._library
                root = library.root if library else Path(os.environ.get('MEETING_DATA_DIR', str(Path.home() / 'Transcriptions')))
                destination = None
                if library:
                    destination = library.import_destination()
                chosen = self._audio_picker()
                if not chosen:
                    return {'cancelled': True}
                if library is None:
                    if not root.exists():
                        private_mkdir(root)
                    library = Library(root)
                    destination = library.import_destination()
                    self._library = library
                return self._operation.start('import', Path(chosen).name, library,
                    lambda progress: import_recording(chosen, destination))
            except (OSError, ValueError):
                return {'error': 'Audio could not be imported. Choose a readable regular file and an available library.'}

    def start_transcription(self, meeting_id):
        with self._lock:
            if self._operation.busy():
                return {'error': 'An operation is already in progress.', 'code': 'busy'}
            try:
                if self._library is None:
                    raise ValueError()
                warning = self._library.load(meeting_id).get('transcription_warning')
                if warning:
                    return {'error': warning}
                recording = self._library.recording_source(meeting_id)
                return self._operation.start('transcribe', recording.path.parent.name, self._library,
                    lambda progress: transcribe(recording, on_progress=progress))
            except (OSError, ValueError):
                return {'error': 'This recording changed or is unavailable. Refresh and select it again.'}

    def retry_remote_cleanup(self, meeting_id, run_id):
        with self._lock:
            if self._operation.busy():
                return {'error': 'An operation is already in progress.', 'code': 'busy'}
            try:
                if self._library is None:
                    raise ValueError()
                path, destination = self._library.cleanup_source(meeting_id, run_id)
                return self._operation.start('cleanup', path.parent.parent.parent.name, self._library,
                    lambda progress: cleanup_published_run(path, destination, on_progress=progress))
            except (OSError, ValueError):
                return {'error': 'This saved run is unavailable for qualified remote cleanup. Refresh and select it again.'}

    def operation_status(self, operation_id):
        return self._operation.status(operation_id)

    def open_operation_result(self, operation_id):
        with self._lock:
            state = self._operation.status(operation_id)
            if state.get('state') != 'ready' or self._operation.result_path is None:
                return {'error': 'The result is not ready.'}
            try:
                library = self._operation.library
                result = library.result_selection(self._operation.result_path)
                self._server.invalidate()
                self._library = library
                return result
            except (OSError, ValueError):
                return {'error': 'The original result is unavailable. Choose the original library and refresh.'}

    def _can_close(self):
        return self._operation.close_allowed()

    def _close(self):
        self._digests.close()


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

        def audio_picker():
            selected = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=('Audio (*.wav;*.mp3;*.m4a;*.mp4;*.flac;*.ogg;*.aac;*.opus;*.aiff;*.aif)', 'All files (*.*)'))
            return selected[0] if selected else None

        def tool_picker():
            selected = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False)
            return selected[0] if selected else None

        bridge = Bridge(server, picker, audio_picker=audio_picker, tool_picker=tool_picker)
        window = webview.create_window('Meeting Transcriber', server.document_url, js_api=bridge,
                                       width=1200, height=820, min_size=(400, 500), text_select=True)
        window.events.closing += bridge._can_close
        window.events.closed += bridge._close
        server.start()
        try:
            webview.start(gui=gui, private_mode=True, storage_path=str(session.profile_path), debug=False)
            if session.error:
                raise session.error
        finally:
            try:
                bridge._close()
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
