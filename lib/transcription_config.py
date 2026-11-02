"""One per-user credential parser/store and installed FFprobe resolver."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import sys
from private_files import Directory, atomic_write_private, open_regular, publication_guard, verify_private

class SetupError(ValueError):
    pass

class MissingKeyError(SetupError):
    pass

def credential_file():
    if sys.platform == 'win32':
        return Path(os.environ['LOCALAPPDATA']) / 'MeetingTranscriber' / '.env'
    return Path.home().resolve() / 'Library' / 'Application Support' / 'MeetingTranscriber' / '.env'

def config_file():
    return Path(os.environ.get('MEETING_CONFIG_DIR', str(Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'meeting-transcriber'))) / 'config'

def read_key(override=None):
    if override:
        return override
    path = credential_file()
    try:
        verify_private(path)
        if sys.platform != 'win32' and path.lstat().st_mode & 0o777 != 0o600:
            raise SetupError('Credential mode must be 0600.')
        with open_regular(path) as stream:
            data = stream.read(16385)
    except FileNotFoundError:
        raise MissingKeyError(f'No AssemblyAI API key is configured at {path}. Run meeting setup or save a key in Transcription setup.') from None
    except (OSError, ValueError):
        raise SetupError(f'{path} must be a private regular non-symlink file (mode 0600 on Mac). Repair it manually, then run meeting doctor.') from None
    prefix = b'ASSEMBLYAI_API_KEY='
    if not data.startswith(prefix) or not data.endswith(b'\n') or data.count(b'\n') != 1 or len(data) > 16384 or not data[len(prefix):-1] or b'\0' in data or b'\r' in data:
        raise SetupError(f'{path} must contain exactly one non-empty ASSEMBLYAI_API_KEY=<value> assignment.')
    try:
        return data[len(prefix):-1].decode('utf-8')
    except UnicodeDecodeError:
        raise SetupError('The credential file is not valid UTF-8.') from None

def _create_parent(path):
    current = path
    while not current.exists():
        current = current.parent
    target = Directory(current)
    destination = target.path
    for part in path.relative_to(current).parts:
        destination /= part
        target.mkdir(destination)
    return Directory(destination)

def store_key(key):
    if not isinstance(key, str) or not key or len(key.encode('utf-8')) > 16000 or any(c in key for c in '\n\r\0'):
        raise SetupError('Enter one non-empty API key.')
    path = credential_file()
    if path.exists() or path.is_symlink():
        read_key()  # An unsafe or malformed existing file is never overwritten.
    target = _create_parent(path.parent)
    verify_private(path.parent, directory=True)
    with publication_guard(target.check):
        atomic_write_private(path, f'ASSEMBLYAI_API_KEY={key}\n')
    if read_key() != key:
        raise SetupError('The saved key could not be verified.')

def _executable(path):
    path = Path(path).expanduser().absolute()
    # Installed package-manager symlinks are ordinary executable aliases; pin
    # their actual regular target, never execute a renderer-supplied path.
    path = path.resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SetupError('Locate a regular executable FFprobe file.')
    return path

def resolve_ffprobe():
    override = os.environ.get('MEETING_TRANSCRIBER_FFPROBE_BIN')
    if override:
        return _executable(override)
    saved = None
    path = config_file()
    if path.exists():
        with open_regular(path.resolve()) as stream:
            for line in stream.read(65536).decode('utf-8').splitlines():
                if line.startswith('ffprobe='):
                    saved = line[len('ffprobe='):]
    if saved:
        try:
            return _executable(saved)
        except (OSError, ValueError):
            raise SetupError('The saved FFprobe is unavailable. Use Locate FFprobe again.') from None
    candidates = [shutil.which('ffprobe')]
    if sys.platform == 'darwin':
        candidates += ['/opt/homebrew/opt/ffmpeg@7/bin/ffprobe', '/opt/homebrew/bin/ffprobe', '/usr/local/opt/ffmpeg@7/bin/ffprobe', '/usr/local/bin/ffprobe']
    elif sys.platform == 'win32':
        candidates += [str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/WinGet/Links/ffprobe.exe'), 'C:/ffmpeg/bin/ffprobe.exe', 'C:/Program Files/ffmpeg/bin/ffprobe.exe']
    for candidate in candidates:
        if candidate:
            try:
                return _executable(candidate)
            except (OSError, ValueError):
                pass
    raise SetupError('FFprobe is missing. Install FFmpeg/FFprobe, then use Locate FFprobe.')

def store_ffprobe(chosen):
    tool = _executable(chosen)
    if any(c in str(tool) for c in '\n\r\0'):
        raise SetupError('Choose an FFprobe path without line breaks.')
    path = config_file()
    parent = _create_parent(path.parent)
    text = ''
    if path.exists() or path.is_symlink():
        verify_private(path)
        with open_regular(path) as stream:
            text = stream.read(65536).decode('utf-8')
    lines = [line for line in text.splitlines() if not line.startswith('ffprobe=')]
    lines.append('ffprobe=' + str(tool))
    path = parent.path / path.name
    with publication_guard(parent.check):
        atomic_write_private(path, '\n'.join(lines) + '\n')

def readiness():
    result = {'credential_location': str(credential_file()), 'key_ready': False, 'ffprobe_ready': False}
    try:
        read_key()
        result['key_ready'] = True
    except (OSError, ValueError) as error:
        result['key_error'] = str(error)
    try:
        result['ffprobe'] = str(resolve_ffprobe())
        result['ffprobe_ready'] = True
    except (OSError, ValueError):
        result['ffprobe_error'] = 'FFprobe is unavailable. Install it or use Locate FFprobe.'
    return result

if __name__ == '__main__':
    # Private shell seam. Secrets use stdin/stdout pipes, never argv or children.
    try:
        action = sys.argv[1]
        if action == 'path': print(credential_file())
        elif action == 'read': print(read_key(), end='')
        elif action == 'store': store_key(sys.stdin.read())
        elif action == 'ffprobe': print(resolve_ffprobe())
        else: raise SetupError('Unknown setup operation.')
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1 if isinstance(error, MissingKeyError) else 2)
