"""Served reader outcomes, using real saved records and Bridge projections."""
from __future__ import annotations

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))
from desktop.main import Bridge
from desktop.server import ReaderServer
from meeting_library import Library
from meeting_digests import record
from tests.test_assemblyai_adapter import write_published_run
from tests.test_html_browser import CHROME_CANDIDATES, CdpPage, aiohttp, browser_prerequisites_available


def sparse_table():
    return "|".join(["a"] * 257) + "\n" + "|".join(["-"] * 257) + "\n" + "a\n" * 256


@unittest.skipUnless(browser_prerequisites_available(), "requires pinned aiohttp and installed Chromium")
class SummaryBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="meeting-summary-browser-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library_root = self.root / "library"
        self.library_root.mkdir(mode=0o700)
        home = self.root / "home"
        locations = {"HOME": home, "USERPROFILE": home, "APPDATA": home / "AppData/Roaming",
                     "LOCALAPPDATA": home / "AppData/Local", "CODEX_HOME": home / ".codex",
                     "CLAUDE_CONFIG_DIR": home / ".claude"}
        for path in locations.values():
            path.mkdir(parents=True, exist_ok=True)
        self.environment = patch.dict(os.environ, {**{name: str(path) for name, path in locations.items()},
            "MEETING_DATA_DIR": str(self.library_root), "MEETING_CONFIG_DIR": str(self.root / "config")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.records = {}
        bundle = self.library_root / "20261102-100000-Rendered-cedar"
        for run in ("T1", "T2"):
            write_published_run(bundle / "transcripts" / run, deletion={"confirmed": True})
        with wave.open(str(bundle / "recording.wav"), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 16000)
        library = Library(self.library_root)
        row = library.scan()["meetings"][0]
        self.meeting_id = row["id"]
        self.runs = {r["label"]: r["id"] for r in row["runs"]}
        self.source = library.digest_source(row["id"], self.runs["T2"])
        self.skill = (ROOT / "skills/meeting/SKILL.md").read_bytes()
        self.library = library
        for label, provider, body in [
            ("codex", "codex", (ROOT / "docs/handoffs/windows-2026-11-02/live-smoke/codex-summary.md").read_text(encoding="utf-8")),
            ("sparse", "codex", sparse_table()),
            ("deep", "codex", "> " * 100 + "DISTINCT-DEEP-TAIL"),
            ("tokens", "codex", "*word* " * 3000 + "TOKEN-TAIL"),
            ("near", "codex", "n" * (128 * 1024 - 20) + "NEAR-LIMIT-TAIL"),
            ("code", "codex", "# Code\n\n`<img src=x>`\n\n```html onclick=x\n<script>literal()</script>\n" + "x" * 1000 + "\n```\n\n    indented *literal*\n\n1. outer\n   - inner **bold**\n"),
            ("claude", "claude", (ROOT / "docs/handoffs/windows-2026-11-02/live-smoke/claude-summary.md").read_text(encoding="utf-8")),
        ]:
            self.publish(label, provider, body)
        old_source = library.digest_source(row["id"], self.runs["T1"])
        library.publish_digest(old_source, record(old_source, self.skill, "claude", "synthetic", "# Other run\nT1 only.", None))
        # Existing owners derive both earlier-revision and unavailable-history
        # warnings; the renderer must preserve them, including on fallback.
        canonical = bundle / "transcripts/T2/transcript.json"
        canonical.write_bytes(canonical.read_bytes() + b"\n")
        malformed = bundle / "summaries" / ("20261101T111111000000Z-" + "a" * 32)
        malformed.mkdir(mode=0o700)
        (malformed / "summary.json").write_text("{}", encoding="utf-8")
        self.server = ReaderServer(ROOT / "desktop/assets")
        self.bridge = Bridge(self.server, lambda: None)
        self.addCleanup(self.bridge._close)
        self.addCleanup(self.server.close)
        self.job = None
        self.actions = []
        self.missing_parser = False
        self.expire_history_once = False
        original_handler = self.server.http.RequestHandlerClass
        owner = self

        class Handler(original_handler):
            def handle(handler):
                try:
                    super().handle()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    pass  # Chromium closes idle keep-alive sockets on teardown.

            def do_GET(handler):
                if owner.missing_parser and handler.path.endswith("/vendor/markdown-it.umd.min.js"):
                    handler.send_error(404)
                else:
                    super().do_GET()

            def do_POST(handler):
                if handler.path != "/" + owner.server.token + "/test-bridge":
                    return super().do_POST()
                message = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                name, args = message["name"], message["args"]
                owner.actions.append(name)
                # Transport only: all record/history/selection projections are
                # produced by the existing Bridge. Never run a provider job.
                if name in ("refresh_library", "load_run", "summary_history", "choose_library"):
                    if name == "summary_history" and owner.expire_history_once:
                        owner.expire_history_once = False
                        args[2] = "expired-fixture-id"
                    result = getattr(owner.bridge, name)(*args)
                elif name == "start_summary":
                    source = owner.bridge._library.digest_source(args[0], args[1])
                    owner.bridge._library.publish_digest(source, record(source, owner.skill, "claude", "synthetic", "# Newly saved\nCompleted projection.", None))
                    owner.job = {"id": "fixture-completed", "state": "succeeded", "meeting_id": args[0], "run_id": args[1]}
                    result = owner.job
                elif name == "summary_status":
                    result = owner.job
                else:
                    result = {"error": "Unexpected test action"}
                body = json.dumps(result).encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

        self.server.http.RequestHandlerClass = Handler
        self.server.start()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.chrome_process = subprocess.Popen([
            str(next(p for p in CHROME_CANDIDATES if p.is_file())), "--headless=new", "--disable-gpu",
            "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
            f"--remote-debugging-port={port}", f"--user-data-dir={self.root / 'chrome-profile'}", "about:blank"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_chrome)
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                    self.websocket = next(p["webSocketDebuggerUrl"] for p in json.load(response) if p["type"] == "page")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)
        self.hashes = self.saved_hashes()

    def publish(self, label, provider, body):
        value = record(self.source, self.skill, provider, "synthetic", body, None, provider_warning=provider == "codex")
        self.library.publish_digest(self.source, value)
        self.records[label] = value

    def saved_hashes(self):
        return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.library_root.rglob("*") if p.is_file()}

    def tearDown(self):
        for p, digest in self.hashes.items():
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), digest, str(p))

    def stop_chrome(self):
        self.chrome_process.terminate()
        try:
            self.chrome_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.chrome_process.kill()
            self.chrome_process.wait()

    async def page(self, websocket, events, formatted=True):
        page = CdpPage(websocket, events)
        await page.request("Page.enable")
        await page.request("Runtime.enable")
        await page.request("Network.enable")
        endpoint = self.server.origin + "/" + self.server.token + "/test-bridge"
        # This test-only adapter transports the real Bridge projections. It does
        # not reproduce Library identities, history, generation or selection.
        await page.request("Page.addScriptToEvaluateOnNewDocument", {"source": """
          window.pywebview={api:new Proxy({}, {get:(_,name)=>(...args)=>fetch(ENDPOINT,{method:'POST',body:JSON.stringify({name,args})}).then(r=>r.json())})};
          window.parserEntries=0;
          Object.defineProperty(window,'markdownit',{configurable:true,get(){return this._mdFactory},set(factory){
            this._mdFactory=(...args)=>{const parser=factory(...args);const parse=parser.parse;parser.parse=function(...values){window.parserEntries++;if(window.throwParserOnce){window.throwParserOnce=false;throw Error('owned fault injection')}const tokens=parse.apply(this,values);if(window.unsupportedOnce){window.unsupportedOnce=false;tokens.push({type:'unsupported_fixture',nesting:0,level:0})}return tokens};return parser}
          }});
        """.replace("ENDPOINT", json.dumps(endpoint))})
        await self.open_reader(page, formatted)
        return page

    async def open_reader(self, page, formatted=True):
        await page.navigate(self.server.document_url)
        await self.wait(page, "document.querySelector('#meeting-list button')")
        await page.evaluate("document.querySelector('#meeting-list button').click()")
        await self.wait(page, "document.querySelector('#summary-history').options.length>0")
        await self.select(page, "claude")
        await self.wait(page, "document.querySelector('#summary-text table')" if formatted else
                        "document.querySelector('#summary-text').textContent.includes('Cedar Lantern')")

    async def wait(self, page, predicate):
        return await page.evaluate("""new Promise((resolve,reject)=>{const until=performance.now()+4000;function check(){if(PREDICATE)return resolve(true);if(performance.now()>until)return reject(Error('reader outcome missing'));setTimeout(check,10)}check()})""".replace("PREDICATE", predicate))

    async def select(self, page, label):
        value = self.records[label]
        await page.evaluate("(()=>{const select=document.querySelector('#summary-history');select.value=" + json.dumps(value["id"]) + ";select.dispatchEvent(new Event('change'))})()")
        await self.wait(page, "document.querySelector('#summary-history').value===" + json.dumps(value["id"]) + " && document.querySelector('#summary-provenance').textContent.includes(" + json.dumps(value["generated_at"]) + ")")

    def test_structured_history_and_completed_projection(self):
        asyncio.run(self.structured())

    async def structured(self):
        async with aiohttp.ClientSession() as session, session.ws_connect(self.websocket) as websocket:
            page = await self.page(websocket, [])
            await page.request("Emulation.setDeviceMetricsOverride", {"width": 1200, "height": 820, "deviceScaleFactor": 1, "mobile": False})
            await page.evaluate("if(document.documentElement.dataset.theme!=='light')document.querySelector('#theme-toggle').click()")
            self.assertEqual(await page.evaluate("document.querySelectorAll('#summary-text tbody tr').length"), 4)
            self.assertTrue(await page.evaluate("!!document.querySelector('#summary-text h1') && !!document.querySelector('#summary-text strong')"))
            self.assertTrue(await page.evaluate("document.querySelector('#summary-text h1').getBoundingClientRect().height>0 && parseFloat(getComputedStyle(document.querySelector('#summary-text h1')).fontSize)>parseFloat(getComputedStyle(document.querySelector('#summary-text')).fontSize)"))
            await self.select(page, "codex")
            self.assertTrue(await page.evaluate("!document.querySelector('#summary-text table') && document.querySelector('#summary-text li').textContent.includes('Decision')"))
            self.assertTrue(await page.evaluate("!document.querySelector('#summary-warning').hidden"))
            self.assertTrue(await page.evaluate("document.querySelector('#summary-provenance').textContent.includes('Earlier transcript revision') && document.querySelector('#summary-warning').textContent.includes('unavailable')"))
            await self.select(page, "code")
            self.assertTrue(await page.evaluate("document.querySelectorAll('#summary-text pre code').length===2 && document.querySelector('#summary-text code').textContent==='<img src=x>' && !document.querySelector('#summary-text img') && !!document.querySelector('#summary-text ol ul')"))
            await page.request("Emulation.setDeviceMetricsOverride", {"width": 430, "height": 820, "deviceScaleFactor": 1, "mobile": False})
            await page.evaluate("if(document.documentElement.dataset.theme!=='dark')document.querySelector('#theme-toggle').click()")
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth<=innerWidth+1 && document.querySelector('#summary-text pre').scrollWidth>document.querySelector('#summary-text pre').clientWidth"))
            await self.select(page, "claude")
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth<=innerWidth+1 && document.querySelector('.summary-table-scroll').scrollWidth>document.querySelector('.summary-table-scroll').clientWidth"))
            await page.evaluate("document.querySelector('#transcript-view').click()")
            self.assertTrue(await page.evaluate("document.querySelector('#summary-panel').hidden && !document.querySelector('#passages').hidden && document.querySelector('#passages').textContent.includes('First.')"))
            await page.evaluate("document.querySelector('#summary-view').click()")
            for run, heading in [("T1", "Other run"), ("T2", "Cedar Lantern Meeting: Digest")]:
                await page.evaluate("(()=>{const s=document.querySelector('#run-select');s.value=[...s.options].find(o=>o.textContent.includes(" + json.dumps(run) + ")).value;s.dispatchEvent(new Event('change'))})()")
                await self.wait(page, "document.querySelector('#summary-text h1')?.textContent===" + json.dumps(heading))
            await page.evaluate("document.querySelector('#generate-summary').click()")
            await self.wait(page, "document.querySelector('#summary-text h1')?.textContent==='Newly saved'")
            self.assertEqual(self.actions.count("start_summary"), 1)
            await page.websocket.close()

    def test_generated_content_is_inert(self):
        hits = []

        class Observer(BaseHTTPRequestHandler):
            def do_GET(handler):
                hits.append(handler.path)
                handler.send_response(200)
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            def log_message(handler, *_):
                pass

        observer = ThreadingHTTPServer(("127.0.0.1", 0), Observer)
        thread = threading.Thread(target=observer.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(observer.server_close)
        self.addCleanup(observer.shutdown)
        destination = f"http://127.0.0.1:{observer.server_port}/generated-attempt"
        same_origin = self.server.origin + "/media/GENERATED_CAPABILITY"
        script = "window.pywebview.api.choose_library();window.generatedAction=true"
        hostile = f'''# Hostile source remains text
<script>{script}</script><style>body{{display:none}}</style>
<svg onload="{script}"></svg><math><mtext>x</mtext></math>
<iframe src="{destination}"></iframe><object data="{destination}"></object>
<form action="{destination}"><input name="x"><button>Submit</button></form>
<meta http-equiv="refresh" content="0;url={destination}">
<img id="summary-provider" src="{same_origin}" onerror="{script}">
[outside]({destination}) ![image]({destination})
[same-origin]({same_origin}) ![same-image]({same_origin})
[script](javascript:{script}) [data](data:text/html,attack) [file](file:///C:/synthetic)
[protocol-relative](//127.0.0.1:{observer.server_port}/generated-attempt)
<{destination}> <attack@example.invalid> {destination}
[reference][target]
[target]: {destination}
```javascript onclick=attack()
{script}
```
'''
        self.publish("hostile", "codex", hostile)
        asyncio.run(self.inert(hostile, destination, same_origin, hits))

    async def inert(self, hostile, destination, same_origin, hits):
        events = []
        async with aiohttp.ClientSession() as session, session.ws_connect(self.websocket) as websocket:
            page = await self.page(websocket, events)
            await self.select(page, "hostile")
            self.assertTrue(await page.evaluate("document.querySelector('#summary-text').textContent.includes(" + json.dumps(destination) + ") && document.querySelector('#summary-text').textContent.includes('[target]:')"))
            self.assertEqual(await page.evaluate("document.querySelectorAll('#summary-text a,#summary-text img,#summary-text script,#summary-text style,#summary-text svg,#summary-text math,#summary-text iframe,#summary-text object,#summary-text form,#summary-text input,#summary-text button,#summary-text meta,#summary-text [id],#summary-text [style],#summary-text [href],#summary-text [src]').length"), 0)
            await page.evaluate("document.querySelector('#summary-text').click()")
            self.assertFalse(await page.evaluate("Boolean(window.generatedAction)"))
            self.assertEqual(self.actions.count("choose_library"), 0)
            self.assertEqual(self.actions.count("start_summary"), 0)
            self.assertEqual(hits, [])
            urls = [e["params"]["request"]["url"] for e in events if e.get("method") == "Network.requestWillBeSent"]
            # Chromium's native audio controls load their own data:image icons.
            # Observe the generated destinations, not unrelated browser chrome.
            self.assertFalse(any(destination in url or same_origin in url or url.startswith(("data:text/html,attack", "file:///C:/synthetic", "javascript:")) for url in urls), urls)
            self.assertEqual(await page.evaluate("location.href"), self.server.document_url)
            await page.websocket.close()

    def test_bounded_full_source_fallback_and_recovery(self):
        asyncio.run(self.bounded())

    async def bounded(self):
        async with aiohttp.ClientSession() as session, session.ws_connect(self.websocket) as websocket:
            page = await self.page(websocket, [])
            for label in ("sparse", "deep", "tokens"):
                before = await page.evaluate("window.parserEntries")
                start = time.monotonic()
                await self.select(page, label)
                expected = self.records[label]["markdown"]
                self.assertTrue(await page.evaluate("document.querySelector('#summary-text').textContent===" + json.dumps(expected) + " && !document.querySelector('#summary-format-warning').hidden && !document.querySelector('#summary-warning').hidden"))
                after = await page.evaluate("window.parserEntries")
                self.assertEqual(after - before, 0 if label == "sparse" else 1)
                await page.evaluate("document.querySelector('#theme-toggle').click()")
                self.assertLess(time.monotonic() - start, 2, label)
            start = time.monotonic()
            await self.select(page, "near")
            self.assertTrue(await page.evaluate("document.querySelector('#summary-format-warning').hidden && document.querySelector('#summary-text').textContent.endsWith('NEAR-LIMIT-TAIL')"))
            await page.evaluate("document.querySelector('#theme-toggle').click()")
            self.assertLess(time.monotonic() - start, 2, "near-limit")
            for fault in ("throwParserOnce", "unsupportedOnce"):
                await page.evaluate("window." + fault + "=true")
                await self.select(page, "code")
                self.assertTrue(await page.evaluate("document.querySelector('#summary-text').textContent===" + json.dumps(self.records["code"]["markdown"]) + " && !document.querySelector('#summary-format-warning').hidden"))
                await self.select(page, "claude")
                self.assertTrue(await page.evaluate("!!document.querySelector('#summary-text table') && document.querySelector('#summary-format-warning').hidden"))
            # A real error projection preserves the currently readable summary.
            self.expire_history_once = True
            await page.evaluate("(()=>{const s=document.querySelector('#summary-history');s.value=" + json.dumps(self.records["codex"]["id"]) + ";s.dispatchEvent(new Event('change'))})()")
            await self.wait(page, "document.querySelector('#reader-notice').textContent.includes('unavailable')")
            self.assertTrue(await page.evaluate("!!document.querySelector('#summary-text table')"))
            self.missing_parser = True
            await self.open_reader(page, formatted=False)
            self.assertTrue(await page.evaluate("document.querySelector('#summary-text').textContent===" + json.dumps(self.records["claude"]["markdown"]) + " && !document.querySelector('#summary-format-warning').hidden && !document.querySelector('#summary-warning').hidden"))
            self.missing_parser = False
            await self.open_reader(page)
            self.assertTrue(await page.evaluate("document.querySelector('#summary-format-warning').hidden"))
            await page.websocket.close()


if __name__ == "__main__":
    unittest.main()
