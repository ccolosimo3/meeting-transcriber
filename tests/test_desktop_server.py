from __future__ import annotations

import http.client
import io
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
from server import ReaderServer


class ReaderServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assets = Path(self.tmp.name)
        (self.assets / 'index.html').write_text('<h1>App</h1>', encoding='utf-8')
        (self.assets / 'app.js').write_text('/* local only */', encoding='utf-8')
        self.server = ReaderServer(self.assets)
        self.server.start()
        self.payload = bytes(range(256)) * 2000
        self.source = io.BytesIO(self.payload)
        self.url = self.server.bind(self.source, 'recording.wav')

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def request(self, target=None, method='GET', headers=None):
        parsed = urlsplit(target or self.url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        conn.request(method, parsed.path, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_actual_range_bytes_and_head(self):
        for start, end in [(0, 31), (250000, 250123), (len(self.payload)-20, len(self.payload)-1)]:
            status, headers, body = self.request(headers={'Range': f'bytes={start}-{end}'})
            self.assertEqual(status, 206)
            self.assertEqual(body, self.payload[start:end+1])
            self.assertEqual(headers['Content-Range'], f'bytes {start}-{end}/{len(self.payload)}')
            self.assertEqual(headers['Cache-Control'], 'no-store')
        status, headers, body = self.request(method='HEAD')
        self.assertEqual((status, int(headers['Content-Length']), body), (200, len(self.payload), b''))
        self.assertEqual(self.request(headers={'Range':'bytes=-19'})[2], self.payload[-19:])

    def test_unsatisfiable_and_malformed_ranges(self):
        for value in ['bytes=900000-', 'bytes=12-1', 'bytes=-0', 'bytes=1-2,4-5', 'bytes=-', 'invalid']:
            with self.subTest(range=value):
                status, headers, body = self.request(headers={'Range':value})
                self.assertEqual(status, 416)
                self.assertEqual(headers['Content-Range'], f'bytes */{len(self.payload)}')
                self.assertEqual(body, b'')

    def test_unauthorized_routes_host_origin_and_methods(self):
        for path in ['/media/unknown', '/index.html', '/app.js', '/etc/passwd', '/%2e%2e/index.html']:
            self.assertEqual(self.request(self.server.origin+path)[0], 404)
        self.assertEqual(self.request(headers={'Host':'evil.invalid'})[0], 403)
        self.assertEqual(self.request(headers={'Origin':'https://evil.invalid'})[0], 403)
        self.assertEqual(self.request(headers={'Sec-Fetch-Site':'cross-site'})[0], 403)
        self.assertEqual(self.request(method='POST')[0], 405)
        self.assertEqual(self.request(headers={'Origin':self.server.origin})[2], self.payload)

    def test_assets_are_allowlisted_and_do_not_enable_filesystem_reads(self):
        (self.assets / 'private.txt').write_text('not public', encoding='utf-8')
        status, headers, body = self.request(self.server.document_url)
        self.assertEqual((status, body), (200, b'<h1>App</h1>'))
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(self.request(self.server.document_url.replace('index.html','private.txt'))[0],404)

    def test_summary_scripts_are_exact_local_assets_with_existing_security(self):
        vendor = self.assets / 'vendor'
        vendor.mkdir()
        payloads = {'summary-markdown.js': b'/* summary owner */',
                    'vendor/markdown-it.umd.min.js': b'/* pinned parser */'}
        for name, payload in payloads.items():
            (self.assets / name).write_bytes(payload)
        for name in ('markdown-it.LICENSES.txt', 'markdown-it.umd.min.js.map', 'private.js'):
            (vendor / name).write_text('not public', encoding='utf-8')
        _, app_headers, _ = self.request(self.server.document_url)
        for name, payload in payloads.items():
            url = self.server.document_url.replace('index.html', name)
            status, headers, body = self.request(url)
            self.assertEqual((status, body), (200, payload))
            self.assertIn('javascript', headers['Content-Type'])
            for key in ('Content-Security-Policy', 'Cache-Control', 'X-Content-Type-Options', 'Referrer-Policy', 'X-Frame-Options'):
                self.assertEqual(headers[key], app_headers[key])
            self.assertNotIn('Access-Control-Allow-Origin', headers)
        for path in ('vendor/markdown-it.LICENSES.txt', 'vendor/markdown-it.umd.min.js.map',
                     'vendor/private.js', 'vendor/../app.js', 'vendor/%2e%2e/app.js'):
            self.assertEqual(self.request(self.server.document_url.replace('index.html', path))[0], 404)
        self.assertEqual(self.request(self.server.origin + '/wrong/vendor/markdown-it.umd.min.js')[0], 404)

    def test_refresh_revokes_open_handle_media_and_export_capabilities(self):
        export = io.BytesIO(b'<h1>Saved &amp; unchanged</h1>')
        url = self.server.bind(export, 'transcript.html', export=True)
        self.assertEqual(self.request(url)[2], b'<h1>Saved &amp; unchanged</h1>')
        self.server.invalidate()
        self.assertTrue(self.source.closed)
        self.assertTrue(export.closed)
        self.assertEqual(self.request()[0], 404)
        self.assertEqual(self.request(url)[0], 404)


if __name__ == '__main__':
    unittest.main()
