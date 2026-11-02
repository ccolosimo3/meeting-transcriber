"""Private filesystem primitives shared by CLI, desktop and publication."""
from __future__ import annotations
import ctypes
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import secrets
import stat
import sys

class PrivateFileError(ValueError):
    pass

def _windows_api():
    from ctypes import wintypes as w

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD)]
    adv.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = w.DWORD
    adv.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return adv, kernel, w


def _sid_string(sid, adv, kernel, w):
    value = w.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return value.value
    finally:
        kernel.LocalFree(value)


def _current_sid():
    adv, kernel, w = _windows_api()
    token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _sid_string(ctypes.c_void_p.from_buffer(data).value, adv, kernel, w)
    finally:
        kernel.CloseHandle(token)


def _private_windows_directory(path: Path):
    """Create with its final protected DACL atomically; never expose an empty gap."""
    adv, kernel, w = _windows_api()
    sid = _current_sid()
    descriptor = ctypes.c_void_p()
    sddl = f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]

    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    try:
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def windows_acl(path: Path) -> tuple[str, set[str]]:
    """Read the actual owner and allowed trustees, including inherited ACEs."""
    adv, kernel, w = _windows_api()
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    error = adv.GetNamedSecurityInfoW(str(path), 1, 0x5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        if not acl.value:
            raise PrivateFileError("Session storage has an unrestricted Windows DACL.")
        # ACL header: revision, reserved, size, ACE count, reserved.
        ace_count = ctypes.c_ushort.from_address(acl.value + 4).value
        allowed = set()
        for index in range(ace_count):
            ace = ctypes.c_void_p()
            if not adv.GetAce(acl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 0:  # ACCESS_ALLOWED_ACE: header + mask + SID
                allowed.add(_sid_string(ace.value + 8, adv, kernel, w))
            elif kind != 1:  # Deny ACEs cannot widen access; unknown allow forms fail closed.
                raise PrivateFileError("Session storage has an unsupported Windows ACL entry.")
        return _sid_string(owner, adv, kernel, w), allowed
    finally:
        kernel.LocalFree(descriptor)


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)

def identity(info):
    return info.st_dev, info.st_ino

def verify_private(path, *, directory=False):
    info = Path(path).lstat()
    if linked(info) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise PrivateFileError("Storage must be a regular, unlinked path.")
    if sys.platform == "win32":
        owner, allowed = windows_acl(Path(path))
        sid = _current_sid()
        if owner != sid or not allowed or not allowed <= {sid, "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}:
            raise PrivateFileError("Storage is not private to this account.")
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PrivateFileError("Storage must be owned by this account and private.")

def private_mkdir(path):
    path = Path(path)
    if sys.platform == "win32":
        _private_windows_directory(path)
    else:
        path.mkdir(mode=0o700)
    verify_private(path, directory=True)

def handle_path(descriptor):
    if os.name == "nt":
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        final_name = kernel.GetFinalPathNameByHandleW
        final_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final_name.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_name(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError("Cannot verify opened file")
        name = buffer.value
        if name.startswith("\\\\?\\UNC\\"):
            name = "\\\\" + name[8:]
        elif name.startswith("\\\\?\\"):
            name = name[4:]
        return Path(name)
    if sys.platform == "darwin":
        import fcntl
        return Path(os.fsdecode(fcntl.fcntl(descriptor, 50, bytes(1024)).split(b"\0", 1)[0]))
    return Path(os.readlink(f"/proc/self/fd/{descriptor}"))

class Directory:
    """Captured directory identity, including ancestors below the chosen root."""
    def __init__(self, path):
        lexical = Path(path).expanduser().absolute()
        if linked(lexical.lstat()):
            raise PrivateFileError("Linked destinations are not supported.")
        for ancestor in (lexical, *lexical.parents):
            if sys.platform == 'darwin' and str(ancestor) in ('/var', '/tmp', '/etc'):
                continue
            if linked(ancestor.lstat()):
                raise PrivateFileError('Linked destination ancestors are not supported.')
        self.path = lexical.resolve(strict=True)
        self.identities = {}
        self.capture(self.path)

    def check(self, path):
        path = Path(path)
        parts = path.relative_to(self.path).parts
        if any(part in ('.', '..') for part in parts):
            raise PrivateFileError('Destination is outside the selected directory.')
        current = self.path
        for part in (None, *parts):
            if part is not None:
                current /= part
            info = current.lstat()
            if linked(info) or current in self.identities and identity(info) != self.identities[current]:
                raise PrivateFileError("The selected destination changed. Choose it again.")
        return info

    def capture(self, path, *, expected=None):
        info = self.check(path)
        if expected is not None and identity(info) != expected:
            raise PrivateFileError("The selected directory changed. Refresh and select it again.")
        if not stat.S_ISDIR(info.st_mode):
            raise PrivateFileError("Destination is not a directory.")
        self.identities[Path(path)] = identity(info)

    def mkdir(self, path):
        self.check(Path(path).parent)
        private_mkdir(path)
        self.capture(path)


def open_regular(path):
    path = Path(path)
    # macOS exposes these OS-owned aliases in tempfile paths. Canonicalize
    # only those fixed prefixes; user-selected linked ancestors still fail.
    if sys.platform == 'darwin' and path.parts[1:2] in [('var',), ('tmp',), ('etc',)]:
        path = Path('/private') / path.relative_to('/')
    info = path.lstat()
    if linked(info) or not stat.S_ISREG(info.st_mode):
        raise PrivateFileError("Choose a readable regular file, not a linked file.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        actual = handle_path(fd)
        if os.path.normcase(str(actual)) != os.path.normcase(str(path)) or identity(os.fstat(fd)) != identity(path.lstat()):
            raise PrivateFileError("The selected file changed or contains a linked path.")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise

_write_guard = ContextVar("publication_guard", default=None)

@contextmanager
def publication_guard(check):
    token = _write_guard.set(check)
    try:
        yield
    finally:
        _write_guard.reset(token)

def atomic_write_private(path, content):
    path = Path(path)
    check = _write_guard.get()
    if check:
        check(path.parent)
    else:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = path.parent.resolve() / path.name
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(12) + ".tmp")
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        verify_private(temporary)
        if check:
            check(temporary)
        if os.path.normcase(str(handle_path(fd))) != os.path.normcase(str(temporary)) or identity(os.fstat(fd)) != identity(temporary.lstat()):
            raise PrivateFileError("Publication destination changed.")
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        if check:
            check(temporary)
            check(path.parent)
        os.replace(temporary, path)
        verify_private(path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            if not check or check(temporary.parent):
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
