from __future__ import annotations

import asyncio
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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
from urllib.request import urlopen

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

from render_transcript_html import render  # noqa: E402
from transcript_bundle import Segment  # noqa: E402


CHROME_CANDIDATES = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)


def browser_prerequisites_available() -> bool:
    return aiohttp is not None and any(
        candidate.is_file() for candidate in CHROME_CANDIDATES
    )


if os.environ.get("MEETING_REQUIRE_BROWSER") == "1" and not browser_prerequisites_available():
    raise RuntimeError(
        "MEETING_REQUIRE_BROWSER=1 but the browser gate prerequisites are missing: "
        "the dev-group aiohttp dependency and a local Google Chrome or Chromium "
        "at one of the two supported paths are required"
    )


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class CdpPage:
    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self.websocket = websocket
        self.next_id = 1

    async def request(
        self, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        request_id = self.next_id
        self.next_id += 1
        await self.websocket.send_json(
            {"id": request_id, "method": method, "params": params or {}}
        )
        while True:
            message = await self.websocket.receive_json(timeout=10)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise AssertionError(f"CDP {method} failed: {message['error']}")
            return message.get("result", {})

    async def wait_for(self, method: str) -> None:
        while True:
            message = await self.websocket.receive_json(timeout=10)
            if message.get("method") == method:
                return

    async def navigate(self, url: str) -> None:
        await self.request("Page.navigate", {"url": url})
        await self.wait_for("Page.loadEventFired")

    async def evaluate(self, expression: str) -> object:
        result = await self.request(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            raise AssertionError(f"browser expression failed: {result['exceptionDetails']}")
        return result["result"].get("value")


@unittest.skipUnless(
    browser_prerequisites_available(),
    "requires aiohttp and a local Chromium browser for rendered HTML verification",
)
class HtmlBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.chrome = next(candidate for candidate in CHROME_CANDIDATES if candidate.is_file())
        source = Path(
            "/tmp/20260826-093000-Q3-API-review/transcripts/run-1/transcript.json"
        )
        rendered = render(
            source,
            "en",
            [
                Segment(
                    id=0,
                    start=0,
                    end=75,
                    text="Review the launch plan and compatibility work.",
                    speaker="SPEAKER_00",
                ),
                Segment(
                    id=1,
                    start=76,
                    end=185,
                    text="I will own the release checklist.",
                    speaker="SPEAKER_01",
                ),
            ],
            {"SPEAKER_00": "Alex", "SPEAKER_01": "Morgan"},
        )
        (self.root / "transcript.html").write_text(rendered, encoding="utf-8")

        handler = partial(QuietHandler, directory=str(self.root))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/transcript.html"

        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            self.debug_port = port_socket.getsockname()[1]
        self.chrome_process = subprocess.Popen(
            [
                str(self.chrome),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={self.debug_port}",
                f"--user-data-dir={self.root / 'chrome-profile'}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_chrome)
        self.page_websocket = self._wait_for_page_websocket()

    def _wait_for_page_websocket(self) -> str:
        deadline = time.monotonic() + 10
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urlopen(
                    f"http://127.0.0.1:{self.debug_port}/json/list", timeout=1
                ) as response:
                    targets = json.load(response)
                page = next(target for target in targets if target["type"] == "page")
                return str(page["webSocketDebuggerUrl"])
            except Exception as error:
                last_error = error
                time.sleep(0.05)
        raise AssertionError(f"Chromium DevTools did not start: {last_error}")

    def _stop_chrome(self) -> None:
        if self.chrome_process.poll() is None:
            self.chrome_process.terminate()
            try:
                self.chrome_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.chrome_process.kill()
                self.chrome_process.wait()

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def test_rendered_interaction_responsive_contrast_and_print(self) -> None:
        result = asyncio.run(self._exercise_browser_contract())

        self.assertTrue(result["initial"]["systemDark"])
        self.assertEqual(result["initial"]["pressed"], "true")
        self.assertEqual(result["initial"]["label"], "Switch to light mode")
        self.assertGreaterEqual(result["initial"]["focusContrast"], 3)
        self.assertGreaterEqual(result["initial"]["metadataContrast"], 4.5)

        self.assertEqual(result["toggled"]["theme"], "light")
        self.assertEqual(result["toggled"]["pressed"], "false")
        self.assertEqual(result["toggled"]["label"], "Switch to dark mode")
        self.assertGreaterEqual(result["toggled"]["focusContrast"], 3)
        self.assertGreaterEqual(result["toggled"]["metadataContrast"], 4.5)

        self.assertEqual(result["persisted"]["theme"], "light")
        self.assertEqual(result["persisted"]["pressed"], "false")
        self.assertLessEqual(result["persisted"]["scrollWidth"], 390)
        self.assertGreaterEqual(result["persisted"]["toggleHeight"], 44)
        self.assertEqual(result["persisted"]["summaryColumns"], 2)
        self.assertTrue(result["persisted"]["speakerIdsBlock"])

        self.assertGreater(len(result["pdf"]), 1000)
        self.assertEqual(result["pdf"][:4], b"%PDF")
        self.assertEqual(result["print"]["theme"], "dark")
        self.assertEqual(result["print"]["toggleDisplay"], "none")
        self.assertEqual(result["print"]["bodyBackground"], "rgb(255, 255, 255)")
        self.assertEqual(result["print"]["documentBorder"], "0px")
        self.assertEqual(result["print"]["documentShadow"], "none")
        self.assertEqual(result["print"]["articleBreak"], "avoid")
        self.assertEqual(result["print"]["mainPadding"], "0px")
        self.assertGreaterEqual(result["print"]["speakerContrast"], 4.5)

    async def _exercise_browser_contract(self) -> dict[str, object]:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.page_websocket) as websocket:
                page = CdpPage(websocket)
                await page.request("Page.enable")
                await page.request("Runtime.enable")
                await page.request(
                    "Emulation.setEmulatedMedia",
                    {
                        "features": [
                            {"name": "prefers-color-scheme", "value": "dark"}
                        ]
                    },
                )
                await page.request(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": 390,
                        "height": 844,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )
                await page.navigate(self.url)
                initial = await page.evaluate(self._state_expression(focus=True))
                toggled = await page.evaluate(
                    f"(() => {{ document.getElementById('theme-toggle').click(); return {self._state_expression(focus=True)}; }})()"
                )
                await page.request("Page.reload")
                await page.wait_for("Page.loadEventFired")
                persisted = await page.evaluate(
                    """(() => ({
                      theme: document.documentElement.dataset.theme,
                      pressed: document.getElementById('theme-toggle').getAttribute('aria-pressed'),
                      scrollWidth: document.documentElement.scrollWidth,
                      toggleHeight: Math.round(document.getElementById('theme-toggle').getBoundingClientRect().height),
                      summaryColumns: getComputedStyle(document.querySelector('.summary')).gridTemplateColumns.split(' ').length,
                      speakerIdsBlock: [...document.querySelectorAll('.speaker-id')].every(
                        element => getComputedStyle(element).display === 'block'
                      )
                    }))()"""
                )
                await page.evaluate("document.getElementById('theme-toggle').click()")
                await page.request("Page.reload")
                await page.wait_for("Page.loadEventFired")
                await page.request(
                    "Emulation.setEmulatedMedia",
                    {
                        "media": "print",
                        "features": [
                            {"name": "prefers-color-scheme", "value": "dark"}
                        ],
                    },
                )
                print_state = await page.evaluate(
                    """(() => {
                      const rgb = value => value.match(/[0-9.]+/g).slice(0, 3).map(Number);
                      const luminance = value => {
                        const channels = rgb(value).map(channel => {
                          const normalized = channel / 255;
                          return normalized <= .04045
                            ? normalized / 12.92
                            : ((normalized + .055) / 1.055) ** 2.4;
                        });
                        return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
                      };
                      const contrast = (left, right) => {
                        const values = [luminance(left), luminance(right)].sort((a, b) => b - a);
                        return (values[0] + .05) / (values[1] + .05);
                      };
                      const bodyBackground = getComputedStyle(document.body).backgroundColor;
                      return {
                        theme: document.documentElement.dataset.theme,
                        toggleDisplay: getComputedStyle(document.getElementById('theme-toggle')).display,
                        bodyBackground,
                        documentBorder: getComputedStyle(document.querySelector('.document')).borderTopWidth,
                        documentShadow: getComputedStyle(document.querySelector('.document')).boxShadow,
                        articleBreak: getComputedStyle(document.querySelector('.editorial-turn')).breakInside,
                        mainPadding: getComputedStyle(document.querySelector('main')).paddingTop,
                        speakerContrast: contrast(
                          getComputedStyle(document.querySelector('.speaker')).color,
                          bodyBackground
                        )
                      };
                    })()"""
                )
                printed = await page.request(
                    "Page.printToPDF", {"printBackground": True}
                )
        return {
            "initial": initial,
            "toggled": toggled,
            "persisted": persisted,
            "pdf": base64.b64decode(printed["data"]),
            "print": print_state,
        }

    @staticmethod
    def _state_expression(*, focus: bool) -> str:
        focus_statement = "button.focus();" if focus else ""
        return f"""(() => {{
          const rgb = value => value.match(/[0-9.]+/g).slice(0, 3).map(Number);
          const luminance = value => {{
            const channels = rgb(value).map(channel => {{
              const normalized = channel / 255;
              return normalized <= .04045
                ? normalized / 12.92
                : ((normalized + .055) / 1.055) ** 2.4;
            }});
            return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
          }};
          const contrast = (left, right) => {{
            const values = [luminance(left), luminance(right)].sort((a, b) => b - a);
            return (values[0] + .05) / (values[1] + .05);
          }};
          const button = document.getElementById('theme-toggle');
          {focus_statement}
          const surface = getComputedStyle(document.querySelector('.document')).backgroundColor;
          return {{
            systemDark: matchMedia('(prefers-color-scheme: dark)').matches,
            theme: document.documentElement.dataset.theme || 'system',
            pressed: button.getAttribute('aria-pressed'),
            label: button.getAttribute('aria-label'),
            focusContrast: contrast(getComputedStyle(button).outlineColor, surface),
            metadataContrast: contrast(getComputedStyle(document.querySelector('time')).color, surface)
          }};
        }})()"""


if __name__ == "__main__":
    unittest.main()
