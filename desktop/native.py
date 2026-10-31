"""Pinned pywebview native boundary. Install before creating any window.

The adapters intentionally target pywebview 6.2.1. Navigation is stopped in the
native engine, before the framework can inject its privileged document bridge.
"""

from __future__ import annotations

import ctypes
import importlib
import importlib.abc
import importlib.metadata
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import tempfile
from urllib.parse import urlsplit


class NativeBoundaryError(RuntimeError):
    """The reader cannot establish its required native boundary."""


WEBVIEW2_REQUIRED = (
    "Microsoft Edge WebView2 Runtime is required. Install the Evergreen Runtime "
    "from https://developer.microsoft.com/microsoft-edge/webview2/ and reopen "
    "Meeting Transcriber. The legacy Internet Explorer renderer is disabled."
)


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


def _network_capability_sid() -> str:
    """Derive the named Edge stable LPAC identity through Windows itself."""
    adv, kernel, w = _windows_api()
    derive = ctypes.WinDLL("kernelbase").DeriveCapabilitySidsFromName
    derive.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD)]
    groups, capabilities = ctypes.c_void_p(), ctypes.c_void_p()
    group_count, count = w.DWORD(), w.DWORD()
    if not derive("lpacEdgeStableNetworkSandbox", ctypes.byref(groups), ctypes.byref(group_count), ctypes.byref(capabilities), ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if count.value != 1:
            raise NativeBoundaryError("Cannot establish the WebView2 network capability identity.")
        return _sid_string(ctypes.c_void_p.from_address(capabilities.value).value, adv, kernel, w)
    finally:
        for array, size in ((groups, group_count), (capabilities, count)):
            for index in range(size.value):
                kernel.LocalFree(ctypes.c_void_p.from_address(array.value + index * ctypes.sizeof(ctypes.c_void_p)).value)
            kernel.LocalFree(array)


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
            raise NativeBoundaryError("Session storage has an unrestricted Windows DACL.")
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
                raise NativeBoundaryError("Session storage has an unsupported Windows ACL entry.")
        return _sid_string(owner, adv, kernel, w), allowed
    finally:
        kernel.LocalFree(descriptor)


class Session:
    """One private, exclusively owned browser/profile/temp directory."""

    def __init__(self):
        parent = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) if sys.platform == "win32" else Path(tempfile.gettempdir())
        self.path = parent / ("MeetingTranscriber-session-" + secrets.token_hex(16))
        if sys.platform == "win32":
            _private_windows_directory(self.path)
        else:
            self.path.mkdir(mode=0o700)
        self.profile_path = self.path / "profile"
        self.temp_path = self.path / "tmp"
        # Python 3.13's Windows mode=0700 replaces the inherited user ACE with
        # OWNER RIGHTS. Inherit the explicit session DACL unchanged instead.
        child_mode = 0o777 if sys.platform == "win32" else 0o700
        self.profile_path.mkdir(mode=child_mode)
        self.temp_path.mkdir(mode=child_mode)
        self.error: Exception | None = None
        self._owner = _current_sid() if sys.platform == "win32" else None
        self._engine_capabilities: set[str] = set()
        self.renderer_pids: set[int] = set()
        self._environment = {key: os.environ.get(key) for key in ("TMP", "TEMP", "TMPDIR")}
        self._tempdir = tempfile.tempdir
        self.verify_storage()
        for key in self._environment:
            os.environ[key] = str(self.temp_path)
        tempfile.tempdir = str(self.temp_path)
        self._closed = False

    def verify_storage(self):
        allowed = {self._owner, "S-1-5-18", "S-1-5-32-544"} if sys.platform == "win32" else None
        for path in (self.path, *self.path.rglob("*")):
            try:
                info = path.lstat()
                acl = windows_acl(path) if sys.platform == "win32" else None
            except FileNotFoundError:
                if path == self.path:
                    raise
                continue  # The native engine removes its transient files concurrently.
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise NativeBoundaryError("Session storage contains a redirected path.")
            if sys.platform == "win32":
                owner, trustees = acl
                permitted = allowed | self._engine_capabilities if path != self.profile_path and path.is_relative_to(self.profile_path) else allowed
                if owner != self._owner or not trustees or not trustees <= permitted:
                    raise NativeBoundaryError("Session storage is accessible outside the current user, SYSTEM and administrators.")
            elif info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise NativeBoundaryError("Session storage must be owned by the current user and private.")

    def __enter__(self):
        return self

    def close(self):
        if self._closed:
            return
        self._closed = True
        for key, previous in self._environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        tempfile.tempdir = self._tempdir
        # Do not delete a profile while its renderer still uses it. Interruption
        # or an engine that refuses shutdown leaves the protected session intact.
        if sys.platform == "win32":
            _, kernel, w = _windows_api()
            kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            kernel.OpenProcess.restype = w.HANDLE
            kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
            for pid in self.renderer_pids:
                handle = kernel.OpenProcess(0x00100000, False, pid)
                if handle:
                    try:
                        if kernel.WaitForSingleObject(handle, 5000) != 0:
                            return
                    finally:
                        kernel.CloseHandle(handle)
        try:
            shutil.rmtree(self.path)
        except OSError:
            pass  # A locked leftover has the same private boundary.

    def __exit__(self, *_):
        self.close()


def create_session() -> Session:
    return Session()


def webview2_available() -> bool:
    """Ask the same native loader used by the pinned engine, not a browser PATH."""
    from webview.util import interop_dll_path
    import platform

    architecture = "win-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "win-x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "win-x86"
    try:
        loader = ctypes.WinDLL(interop_dll_path(architecture) + "/WebView2Loader.dll")
        probe = loader.GetAvailableCoreWebView2BrowserVersionString
        probe.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
        probe.restype = ctypes.c_long
        version = ctypes.c_void_p()
        result = probe(None, ctypes.byref(version))
        try:
            return result >= 0 and bool(version.value)
        finally:
            if version.value:
                ole = ctypes.WinDLL("ole32")
                ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
                ole.CoTaskMemFree(version)
    except OSError:
        return False


class _NoLegacyRenderer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"webview.platforms.mshtml", "webview.platforms.cef"}:
            raise NativeBoundaryError(WEBVIEW2_REQUIRED)
        return None


def _configure_windows(webview, document_url: str, session: Session):
    if not webview2_available():
        raise NativeBoundaryError(WEBVIEW2_REQUIRED)
    if "webview.platforms.mshtml" in sys.modules or "webview.platforms.cef" in sys.modules:
        raise NativeBoundaryError(WEBVIEW2_REQUIRED)
    sys.meta_path.insert(0, _NoLegacyRenderer())
    guilib = importlib.import_module("webview.guilib")
    guilib.forced_gui_ = "edgechromium"
    native = importlib.import_module("webview.platforms.winforms")
    if native.renderer != "edgechromium" or not native.is_chromium:
        raise NativeBoundaryError(WEBVIEW2_REQUIRED)
    edge = native.Chromium
    original_ready = edge.EdgeChrome.on_webview_ready
    original_completed = edge.EdgeChrome.on_navigation_completed
    original_message = edge.EdgeChrome.on_script_notify
    origin = urlsplit(document_url)

    def navigation(self, sender, args):
        args.Cancel = str(args.Uri) != document_url

    def popup(self, sender, args):
        args.Handled = True

    def download(self, sender, args):
        args.Cancel = True
        args.Handled = True

    def message(self, sender, args):
        if str(args.Source) == document_url:
            return original_message(self, sender, args)

    def completed(self, sender, args):
        if str(sender.Source) == document_url:
            return original_completed(self, sender, args)

    def ready(self, sender, args):
        if not args.IsSuccess:
            session.error = NativeBoundaryError("WebView2 could not initialize. Repair the Evergreen Runtime and reopen Meeting Transcriber.")
            self.form.Close()
            return
        try:
            core = sender.CoreWebView2
            self.webview.AllowExternalDrop = False
            self.form.AllowDrop = False
            core.FrameNavigationStarting += lambda _, event: setattr(event, "Cancel", True)
            core.Settings.AreHostObjectsAllowed = False
            core.Settings.IsGeneralAutofillEnabled = False
            core.Settings.IsPasswordAutosaveEnabled = False

            def resource(_, event):
                candidate = urlsplit(str(event.Request.Uri))
                if (
                    (candidate.scheme, candidate.netloc) != (origin.scheme, origin.netloc)
                    or event.ResourceContext == edge.CoreWebView2WebResourceContext.Document
                    and str(event.Request.Uri) != document_url
                ):
                    event.Response = core.Environment.CreateWebResourceResponse(None, 403, "Forbidden", "")

            core.AddWebResourceRequestedFilter("*", edge.CoreWebView2WebResourceContext.All)
            core.WebResourceRequested += resource
            session.renderer_pids.add(int(core.BrowserProcessId))
            if Path(str(core.Environment.UserDataFolder)).resolve() != session.profile_path.resolve():
                raise NativeBoundaryError("The native engine selected an unexpected profile directory.")
            capability = _network_capability_sid()
            # AppContainer access intersects the normal-user grant with its
            # capability grant; this engine SID does not grant other users
            # access. Keep the session/profile root free of capability grants.
            # https://learn.microsoft.com/windows/win32/secauthz/implementing-an-appcontainer
            session._engine_capabilities.add(capability)
            session.verify_storage()
            self._reader_resource_guard = resource
            original_ready(self, sender, args)  # The first app navigation occurs here.
        except Exception as error:
            session.error = error
            self.form.Close()

    edge.EdgeChrome.on_navigation_start = navigation
    edge.EdgeChrome.on_new_window_request = popup
    edge.EdgeChrome.on_download_starting = download
    edge.EdgeChrome.on_script_notify = message
    edge.EdgeChrome.on_navigation_completed = completed
    edge.EdgeChrome.on_webview_ready = ready
    return "edgechromium"


def _configure_cocoa(webview, document_url: str, session: Session):
    import objc

    cocoa = importlib.import_module("webview.platforms.cocoa")
    wk = cocoa.WebKit
    store = wk.WKWebsiteDataStore.nonPersistentDataStore()
    if store.isPersistent():
        raise NativeBoundaryError("WKWebView did not create a private data store.")

    host = cocoa.BrowserView.WebKitHost

    def initWithFrame_configuration_(self, frame, config):
        session.verify_storage()
        config.setWebsiteDataStore_(store)
        config.preferences().setValue_forKey_(False, "allowFileAccessFromFileURLs")
        view = objc.super(host, self).initWithFrame_configuration_(frame, config)
        view.unregisterDraggedTypes()
        if view.configuration().websiteDataStore().isPersistent():
            raise NativeBoundaryError("WKWebView selected persistent browser storage.")
        return view

    def performDragOperation_(self, sender):
        return False

    # Extend the existing ObjC class rather than replace its class identity:
    # its inherited mouse/key handlers explicitly refer to BrowserView.WebKitHost
    # in super(), so replacing that symbol with a subclass would recurse.
    objc.classAddMethods(host, [
        objc.selector(initWithFrame_configuration_, signature=wk.WKWebView.initWithFrame_configuration_.signature),
        objc.selector(performDragOperation_, signature=host.performDragOperation_.signature),
    ])

    class PrivateDelegate(cocoa.BrowserView.BrowserDelegate):
        def webView_decidePolicyForNavigationAction_decisionHandler_(self, view, action, handler):
            allowed = (
                action.targetFrame() is not None
                and action.targetFrame().isMainFrame()
                and str(action.request().URL().absoluteString()) == document_url
                and not action.shouldPerformDownload()
            )
            handler(wk.WKNavigationActionPolicyAllow if allowed else wk.WKNavigationActionPolicyCancel)

        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, view, config, action, features):
            return None

        def webView_decidePolicyForNavigationResponse_decisionHandler_(self, view, response, handler):
            allowed = response.canShowMIMEType() and str(response.response().URL().absoluteString()) == document_url
            handler(wk.WKNavigationResponsePolicyAllow if allowed else wk.WKNavigationResponsePolicyCancel)

        def webView_navigationAction_didBecomeDownload_(self, view, action, download):
            download.cancel_(lambda *_: None)

    original_bridge = cocoa.BrowserView.JSBridge

    class PrivateBridge(original_bridge):
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            frame = message.frameInfo()
            if frame.isMainFrame() and str(frame.request().URL().absoluteString()) == document_url:
                return objc.super(PrivateBridge, self).userContentController_didReceiveScriptMessage_(controller, message)

    # 6.2.1 also asks for defaultDataStore after constructing WKWebView. Redirect
    # only this renderer module's lookup, so it never clears the user's global
    # default store and its later cookie operations address the same private one.
    class StoreProxy:
        def defaultDataStore(self):
            return store

        def __getattr__(self, name):
            return getattr(wk.WKWebsiteDataStore, name)

    class WebKitProxy:
        WKWebsiteDataStore = StoreProxy()

        def __getattr__(self, name):
            return getattr(wk, name)

    cocoa.WebKit = WebKitProxy()
    cocoa.BrowserView.BrowserDelegate = PrivateDelegate
    cocoa.BrowserView.JSBridge = PrivateBridge
    return "cocoa"


def configure_webview(webview_module, exact_document_url: str, session: Session) -> str:
    """Install the process-local native policy before create_window/start."""
    if importlib.metadata.version("pywebview") != "6.2.1":
        raise NativeBoundaryError("This desktop build requires pywebview 6.2.1.")
    url = urlsplit(exact_document_url)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.username or url.password or url.fragment:
        raise NativeBoundaryError("The reader document must use its exact loopback session URL.")
    if webview_module.windows:
        raise NativeBoundaryError("The native boundary must be installed before creating a window.")
    session.verify_storage()
    webview_module.settings.update({
        "ALLOW_FILE_URLS": False,
        "ALLOW_DOWNLOADS": False,
        "OPEN_EXTERNAL_LINKS_IN_BROWSER": False,
        "OPEN_DEVTOOLS_IN_DEBUG": False,
        "IGNORE_SSL_ERRORS": False,
        "REMOTE_DEBUGGING_PORT": None,
        "WEBVIEW2_RUNTIME_PATH": None,
    })
    if sys.platform == "win32":
        return _configure_windows(webview_module, exact_document_url, session)
    if sys.platform == "darwin":
        return _configure_cocoa(webview_module, exact_document_url, session)
    raise NativeBoundaryError("The desktop reader supports Windows and macOS.")
