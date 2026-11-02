from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from desktop import native


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows DACL boundary")
class WindowsStorageTests(unittest.TestCase):
    def test_storage_is_private_and_normal_exit_removes_only_session(self):
        session = native.create_session()
        path = session.path
        try:
            (session.profile_path / "synthetic.txt").write_text("synthetic", encoding="utf-8")
            session.verify_storage()
            owner, trustees = native.windows_acl(path)
            self.assertEqual(owner, native._current_sid())
            self.assertEqual(trustees, {owner, "S-1-5-18", "S-1-5-32-544"})
        finally:
            session.close()
        self.assertFalse(path.exists())

    def test_actual_broad_acl_is_rejected(self):
        with native.create_session() as session:
            result = subprocess.run(
                ["icacls", str(session.profile_path), "/grant", "*S-1-5-32-545:(OI)(CI)(RX)"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.assertRaises(native.NativeBoundaryError):
                session.verify_storage()

    def test_interrupted_session_remains_private(self):
        environment = dict(os.environ)
        environment.pop("ASSEMBLYAI_API_KEY", None)
        result = subprocess.run(
            [sys.executable, "-c", "from desktop.native import create_session; import os; s=create_session(); (s.profile_path/'synthetic.txt').write_text('synthetic'); print(s.path,flush=True); os._exit(0)"],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10, check=True,
        )
        path = Path(result.stdout.strip()).resolve()
        # Packaged desktop hosts can virtualize LOCALAPPDATA in child processes.
        self.assertTrue(path.is_relative_to((Path.home() / "AppData" / "Local").resolve()))
        self.assertTrue(path.name.startswith("MeetingTranscriber-session-"))
        try:
            for entry in (path, *path.rglob("*")):
                owner, trustees = native.windows_acl(entry)
                self.assertEqual(owner, native._current_sid())
                self.assertTrue(trustees <= {owner, "S-1-5-18", "S-1-5-32-544"})
        finally:
            shutil.rmtree(path)


@unittest.skipUnless(sys.platform == "win32" and importlib.util.find_spec("webview"), "Windows desktop extra")
class WindowsRuntimeTests(unittest.TestCase):
    def test_missing_runtime_stops_native_setup_before_window_creation(self):
        import webview

        with native.create_session() as session, patch.object(native, "webview2_available", return_value=False):
            with self.assertRaisesRegex(native.NativeBoundaryError, "WebView2 Runtime is required"):
                native.configure_webview(webview, "http://127.0.0.1:54321/session/app", session)
            self.assertFalse(webview.windows)
            self.assertNotIn("webview.platforms.mshtml", sys.modules)

    @unittest.skipUnless(os.environ.get("MEETING_REQUIRE_NATIVE") == "1", "set MEETING_REQUIRE_NATIVE=1 for actual WebView2")
    def test_native_engine_blocks_navigation_popup_and_frames_before_request(self):
        environment = dict(os.environ)
        environment.pop("ASSEMBLYAI_API_KEY", None)
        result = subprocess.run(
            [sys.executable, "-m", "tests.test_desktop_native", "--native-probe"],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"blocked": true', result.stdout)


def _native_probe():
    # This deliberately serves no CSP: the native engine must enforce the
    # document boundary independently of the shipped page's additional policy.
    import webview

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b"<!doctype html><title>reader probe</title><p>synthetic reader</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/app"
    errors = []
    with native.create_session() as session:
        gui = native.configure_webview(webview, url, session)
        forbidden = session.path / "untrusted.html"
        forbidden.write_text("<title>UNTRUSTED</title>", encoding="utf-8")
        from desktop.main import Bridge
        from desktop.server import ReaderServer
        from meeting_library import Library
        library_root = session.path / 'library'
        library_root.mkdir()
        outside = session.path / 'outside-library.txt'
        outside.write_text('preserve', encoding='utf-8')
        auxiliary = ReaderServer(ROOT / 'desktop' / 'assets')
        bridge = Bridge(auxiliary, lambda: None, lambda _: False)
        bridge._library = Library(library_root)
        window = webview.create_window("Native boundary probe", url=url, js_api=bridge, hidden=True)

        def exercise():
            try:
                assert window.events.loaded.wait(15), str(session.error or "App document did not load")
                assert window.evaluate_js("document.title") == "reader probe"
                payload = ['_library.root.__class__.unlink', json.dumps([str(outside)]), '12345']
                window.evaluate_js('window.chrome.webview.postMessage(' + json.dumps(payload) + ')')
                time.sleep(0.2)
                assert outside.read_text(encoding='utf-8') == 'preserve', 'Raw private dispatch changed an outside file'
                assert bridge._library.root == library_root.resolve(), 'Raw private dispatch changed the selected library'
                for target in (url + "/untrusted", forbidden.as_uri(), "https://example.invalid/"):
                    window.evaluate_js(f"location.href={json.dumps(target)};")
                    time.sleep(0.2)
                    assert window.evaluate_js("location.href") == url
                window.evaluate_js(f"window.open({json.dumps(url + '/popup')}, '_blank');")
                window.evaluate_js(f"let frame=document.createElement('iframe'); frame.src={json.dumps(url + '/frame')}; document.body.append(frame);")
                time.sleep(0.3)
                assert window.evaluate_js("location.href") == url
                from System import Boolean, Func
                assert not window.native.Invoke(Func[Boolean](lambda: window.native.webview.AllowExternalDrop))
                assert not any(path in requests for path in ("/app/untrusted", "/app/popup", "/app/frame")), requests
                session.verify_storage()
            except BaseException as error:
                errors.append(str(error))
            finally:
                window.destroy()

        webview.start(exercise, gui=gui, private_mode=True, storage_path=str(session.profile_path))
        if session.error:
            errors.append(str(session.error))
        auxiliary.http.server_close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    if errors:
        raise AssertionError(errors)
    print(json.dumps({"blocked": True, "private_storage": True, "normal_cleanup": not session.path.exists()}))


if __name__ == "__main__":
    if "--native-probe" in sys.argv:
        _native_probe()
    else:
        unittest.main()
