"""Shared local import, preflight, run and qualified remote-cleanup operations."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import io
import math
import os
from pathlib import Path
import re
import shutil
import subprocess

from private_files import Directory, identity, open_regular, publication_guard, verify_private
from transcription_config import read_key, resolve_ffprobe
from transcribe_assemblyai import _build_adapter, failure_details, resolve_cleanup_target

class OperationError(ValueError):
    def __init__(self, message, *, code='stopped', exit_code=2, details=None):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code
        self.details = details

@dataclass(frozen=True)
class Recording:
    path: Path
    destination: Directory
    file_identity: tuple

    def open(self):
        self.destination.check(self.path)
        stream = open_regular(self.path)
        try:
            info = os.fstat(stream.fileno())
            self.destination.check(self.path)
            if (identity(info), info.st_size, info.st_mtime_ns) != self.file_identity:
                raise OperationError('The recording changed. Refresh and select it again.')
            return stream
        except BaseException:
            stream.close()
            raise

    def check(self):
        with self.open() as stream:
            return os.fstat(stream.fileno())

@dataclass(frozen=True)
class Result:
    canonical_path: Path
    deletion_confirmed: bool

    @property
    def exit_code(self):
        return 0 if self.deletion_confirmed else 3

def _stamp(variable):
    value = os.environ.get(variable) or datetime.now().strftime('%Y%m%d-%H%M%S')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', value) or value in ('.', '..'):
        raise OperationError(f'{variable} contains invalid characters')
    return value

def recording_at(path, destination=None):
    path = Path(path).expanduser().absolute()
    with open_regular(path):
        pass
    path = path.parent.resolve() / path.name
    destination = destination or Directory(path.parent)
    destination.capture(path.parent)
    destination.check(path)
    with open_regular(path) as stream:
        info = os.fstat(stream.fileno())
    return Recording(path, destination, (identity(info), info.st_size, info.st_mtime_ns))

def import_recording(source, destination):
    """Copy a native/CLI selected file, without upload or tool/key requirements."""
    source = Path(source).expanduser().absolute()
    destination.check(destination.path)
    with open_regular(source) as stream:
        before = os.fstat(stream.fileno())
        slug = re.sub(r'[^A-Za-z0-9._-]+', '-', source.stem).strip('-') or 'meeting'
        bundle = destination.path / (_stamp('MEETING_TRANSCRIBER_BUNDLE_ID') + '-' + slug[:160])
        try:
            destination.mkdir(bundle)
        except FileExistsError:
            raise OperationError('The dated meeting bundle already exists. No recording was overwritten.') from None
        target = bundle / ('recording' + source.suffix)
        partial = bundle / ('recording.partial' + (source.suffix or '.import'))
        try:
            destination.check(bundle)
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                verify_private(partial)
                destination.check(partial)
                from private_files import handle_path
                if handle_path(output.fileno()) != partial or identity(os.fstat(output.fileno())) != identity(destination.check(partial)):
                    raise OperationError('The import destination changed.')
                shutil.copyfileobj(stream, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(stream.fileno())
            if (identity(before), before.st_size, before.st_mtime_ns) != (identity(after), after.st_size, after.st_mtime_ns) or identity(source.lstat()) != identity(before) or partial.stat().st_size != before.st_size:
                raise OperationError('The source changed during import. Choose the file again.')
            destination.check(partial)
            os.rename(partial, target)
            verify_private(target)
            return recording_at(target, destination)
        except BaseException:
            try:
                destination.check(bundle)
                # Only the captured, exclusively created bundle is removable.
                shutil.rmtree(bundle)
            except (OSError, ValueError):
                pass
            raise

def select_or_import(source, root):
    root = Path(root).expanduser().absolute()
    if not root.exists():
        from private_files import private_mkdir
        private_mkdir(root)
    destination = Directory(root)
    source = Path(source).expanduser().absolute()
    with open_regular(source):
        pass
    source = source.parent.resolve() / source.name
    if source.parent.parent == destination.path and (source.name == 'recording' or source.name.startswith('recording.')) and not source.name.startswith('recording.partial.'):
        return recording_at(source, destination)
    return import_recording(source, destination)

def preflight(recording):
    info = recording.check()
    if info.st_size > 2_200_000_000:
        raise OperationError('The recording is larger than the 2.2 GB AssemblyAI upload limit. Split or compress the recording before transcribing.')
    tool = resolve_ffprobe()
    environment = {key: value for key, value in os.environ.items() if key != 'ASSEMBLYAI_API_KEY'}
    result = subprocess.run([str(tool), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(recording.path)], capture_output=True, env=environment, timeout=60)
    try:
        duration = float(result.stdout.strip())
        if result.returncode or not math.isfinite(duration) or duration <= 0:
            raise ValueError()
    except ValueError:
        raise OperationError('FFprobe could not determine a valid recording duration. Choose supported, readable audio.') from None
    if duration > 36000:
        raise OperationError('The recording is longer than the 10 hours AssemblyAI accepts. Split the recording before transcribing.')
    recording.check()

def transcribe(recording, speakers=None, *, on_progress=lambda phase: None, key_override=None, stderr=None, output_root=None):
    on_progress('checking')
    if speakers is not None and (isinstance(speakers, bool) or not isinstance(speakers, int) or speakers < 1):
        raise OperationError('--speakers requires a positive integer')
    key = read_key(key_override)
    preflight(recording)
    destination = recording.destination
    parent = Path(output_root) if output_root else recording.path.parent / 'transcripts'
    # Narrow internal CLI permits an explicit output root; capture that separate
    # destination instead of letting it redirect the source capability.
    if output_root and parent.parent != recording.path.parent:
        destination = Directory(parent.parent)
    destination.check(parent.parent)
    try:
        destination.mkdir(parent)
    except FileExistsError:
        destination.capture(parent)
    run = parent / _stamp('MEETING_TRANSCRIBER_RUN_ID')
    try:
        destination.mkdir(run)
    except FileExistsError:
        raise OperationError('The output directory already exists. Earlier runs were not changed.') from None
    stub = os.environ.get('MEETING_TRANSCRIBER_ASSEMBLYAI_BIN')
    if stub:
        command = [stub, '--input', str(recording.path), '--output-dir', str(run), '--output-name', 'transcript']
        if speakers is not None:
            command += ['--speakers', str(speakers)]
        environment = dict(os.environ, ASSEMBLYAI_API_KEY=key)
        status = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL, stderr=stderr).returncode
        if status not in (0, 3):
            raise OperationError('The configured adapter stopped.', exit_code=status)
        canonical = run / 'transcript.json'
        if not canonical.is_file() or not canonical.stat().st_size:
            raise OperationError('Transcription did not create the expected JSON output.', exit_code=1)
        return Result(canonical, status == 0)
    sink = stderr if stderr is not None else io.StringIO()
    adapter = _build_adapter(key)
    adapter.stderr = sink
    adapter.on_progress = on_progress
    adapter.audio_opener = recording.open
    try:
        with publication_guard(destination.check):
            recording.check()
            canonical, confirmed = adapter.run(recording.path, run, 'transcript', speakers)
        return Result(canonical, confirmed)
    except (Exception, KeyboardInterrupt) as error:
        # Provider-selected failure distinctions remain owned by the adapter;
        # never expose the exception's raw response or suggest an automatic retry.
        details = failure_details(adapter, error)
        raise OperationError(' '.join(filter(None, (details['problem'], 'The original recording is safe.', details['remote'], details['action']))), code=details['code'], exit_code=130 if isinstance(error, KeyboardInterrupt) else 1, details=details) from None

def cleanup_published_run(canonical_path, destination=None, *, key_override=None, on_progress=lambda phase: None, stderr=None):
    path = Path(canonical_path).expanduser().absolute()
    destination = destination or Directory(path.parent)
    destination.capture(path.parent)
    for name in ('transcript.json', 'transcript.md', 'transcript.html', '.assemblyai.json'):
        destination.check(path.parent / name)
        with open_regular(path.parent / name):
            pass
    receipt_path, receipt, transcript_id = resolve_cleanup_target(path)
    adapter = _build_adapter(read_key(key_override))
    adapter.stderr = stderr if stderr is not None else io.StringIO()
    adapter.receipt_path, adapter.receipt, adapter.transcript_id = receipt_path, receipt, transcript_id
    on_progress('deleting')
    with publication_guard(destination.check):
        status = adapter.cleanup(path)
    return Result(path, status == 0)
