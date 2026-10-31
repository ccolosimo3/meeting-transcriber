"""Session-scoped loopback assets and handle-backed media; no filesystem URL API."""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
import re
import secrets
import sys
import threading
from typing import BinaryIO
from urllib.parse import urlsplit


@dataclass
class Resource:
    stream: BinaryIO
    content_type: str
    lock: threading.Lock = field(default_factory=threading.Lock)


class ReaderServer:
    def __init__(self, assets: Path):
        self.assets = assets
        self.token = secrets.token_urlsafe(32)
        self.resources: dict[str, Resource] = {}
        self.lock = threading.RLock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass  # Requests contain private session capabilities.

            def do_HEAD(self):
                owner.serve(self, head=True)

            def do_GET(self):
                owner.serve(self)

            def do_POST(self):
                self.send_error(405)

            do_PUT = do_DELETE = do_OPTIONS = do_POST

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        self.origin = f'http://127.0.0.1:{self.http.server_port}'
        self.document_url = f'{self.origin}/{self.token}/index.html'
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def bind(self, stream: BinaryIO, name: str, *, export=False) -> str:
        capability = secrets.token_urlsafe(32)
        key = f'/{"export" if export else "media"}/{capability}'
        mime = 'text/html; charset=utf-8' if export else (mimetypes.guess_type(name)[0] or 'application/octet-stream')
        with self.lock:
            self.resources[key] = Resource(stream, mime)
        return self.origin + key

    def invalidate(self, *, media_only=False):
        with self.lock:
            keys = [key for key in self.resources if not media_only or key.startswith('/media/')]
            resources = [self.resources.pop(key) for key in keys]
        for resource in resources:
            with resource.lock:
                resource.stream.close()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)
        self.invalidate()

    def serve(self, handler: BaseHTTPRequestHandler, *, head=False):
        if (handler.headers.get('Host') != urlsplit(self.origin).netloc
                or handler.headers.get('Origin') not in (None, self.origin)
                or handler.headers.get('Sec-Fetch-Site') == 'cross-site'):
            handler.send_error(403)
            return
        path = urlsplit(handler.path)
        if path.query or path.fragment or '%' in path.path:
            handler.send_error(404)
            return
        # Assets are an explicit allowlist, never a directory root exposed to HTTP.
        asset = path.path.removeprefix('/' + self.token + '/')
        if path.path == '/' + self.token + '/' + asset and asset in ('index.html', 'app.js', 'app.css'):
            try:
                payload = (self.assets / asset).read_bytes()
            except OSError:
                handler.send_error(404)
                return
            handler.send_response(200)
            handler.send_header('Content-Type', mimetypes.guess_type(asset)[0] or 'text/plain')
            handler.send_header('Content-Length', str(len(payload)))
            self.headers(handler, app=True)
            handler.end_headers()
            if not head:
                handler.wfile.write(payload)
            return
        with self.lock:
            resource = self.resources.get(path.path)
        if resource is None:
            handler.send_error(404)
            return
        with resource.lock:
            stream = resource.stream
            if stream.closed:
                handler.send_error(404)
                return
            stream.seek(0, 2)
            size = stream.tell()
            start, end, status = 0, size - 1, 200
            requested = handler.headers.get('Range')
            if requested:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                try:
                    if not match or not any(match.groups()):
                        raise ValueError
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    else:
                        suffix = int(last)
                        if suffix <= 0:
                            raise ValueError
                        start = max(0, size - suffix)
                    if start >= size or start > end:
                        raise ValueError
                except ValueError:
                    handler.send_response(416)
                    handler.send_header('Content-Range', f'bytes */{size}')
                    handler.send_header('Content-Length', '0')
                    self.headers(handler)
                    handler.end_headers()
                    return
                status = 206
            handler.send_response(status)
            handler.send_header('Content-Type', resource.content_type)
            handler.send_header('Accept-Ranges', 'bytes')
            handler.send_header('Content-Length', str(max(0, end - start + 1)))
            if status == 206:
                handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.headers(handler)
            handler.end_headers()
            if not head:
                stream.seek(start)
                remaining = end - start + 1
                try:
                    while remaining > 0:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                    pass  # Media engines routinely abort old range requests on seek.

    def headers(self, handler, *, app=False):
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Referrer-Policy', 'no-referrer')
        handler.send_header('X-Frame-Options', 'DENY')
        if app:
            # Pinned pywebview creates its four API wrappers with new Function.
            # WKWebView enforces CSP for that native-injected bridge code.
            # Keep inline/remote scripts forbidden; only Mac needs this eval.
            bridge_eval = " 'unsafe-eval'" if sys.platform == 'darwin' else ''
            handler.send_header('Content-Security-Policy', f"default-src 'none'; script-src 'self'{bridge_eval}; style-src 'self'; media-src 'self'; connect-src 'self'; img-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
