"""One in-memory import/transcription/cleanup operation; no cancellation/resume."""
from __future__ import annotations
import secrets
import threading
from transcription import OperationError

class TranscriptionJob:
    def __init__(self):
        self._lock = threading.RLock()
        self._job = None
        self._closed = False
        self.library = None
        self.result_path = None

    def busy(self):
        with self._lock:
            return bool(self._job and self._job['state'] == 'running')

    def start(self, kind, target, library, work):
        with self._lock:
            if self._closed:
                return {'error': 'The app is closing.'}
            if self.busy():
                return {'error': 'An import, transcription or cleanup is already in progress.', 'code': 'busy'}
            self.library = library
            self.result_path = None
            self._job = {'id': secrets.token_urlsafe(24), 'kind': kind, 'target': target, 'state': 'running', 'phase': 'importing' if kind == 'import' else 'checking'}
            identifier = self._job['id']
            def progress(phase):
                with self._lock:
                    self._job['phase'] = phase
            def run():
                try:
                    result = work(progress)
                    with self._lock:
                        self.result_path = result.path if kind == 'import' else result.canonical_path
                        self._job.update(state='ready', phase='ready', cleanup_required=kind != 'import' and not result.deletion_confirmed)
                except Exception as error:
                    with self._lock:
                        message = str(error) if isinstance(error, (OperationError, ValueError)) else 'The operation stopped. The original recording is safe. Check setup and refresh the library.'
                        self._job.update(state='stopped', phase='stopped', error=message[:2400])
            threading.Thread(target=run, name='meeting-' + kind, daemon=False).start()
            return dict(self._job)

    def status(self, identifier):
        with self._lock:
            if not self._job or identifier != self._job['id']:
                return {'error': 'Unknown or expired operation.'}
            return dict(self._job)

    def close_allowed(self):
        with self._lock:
            if self.busy():
                label = {'import': 'Import', 'transcribe': 'Transcription', 'cleanup': 'Remote cleanup'}[self._job['kind']]
                self._job['close_warning'] = f'{label} in progress — keep this window open'
                return False
            self._closed = True
            return True
