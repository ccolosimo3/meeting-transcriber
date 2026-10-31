"""Build the native folder/app package: python -m desktop.build."""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
import platform
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    output = root / ".local-artifacts" / "desktop-build"
    parser.add_argument("--distpath", type=Path, default=output / "dist")
    args = parser.parse_args(argv)
    if importlib.metadata.version("pyinstaller") != "6.22.3":
        parser.error("Build with the pinned PyInstaller 6.22.3 desktop-build group.")
    if importlib.metadata.version("pywebview") != "6.2.1":
        parser.error("Build with the pinned pywebview 6.2.1 desktop extra.")
    if sys.platform not in {"win32", "darwin"}:
        parser.error("Build on Windows x64 or Apple Silicon macOS.")
    if sys.platform == "darwin" and platform.machine() != "arm64":
        parser.error("The Mac package must be built on Apple Silicon.")
    if sys.platform == "win32" and platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("The Windows package must be built on Windows x64.")
    from PyInstaller.__main__ import run

    options = [
        "--name", "MeetingTranscriber",
        "--onedir", "--windowed", "--noconfirm", "--clean",
        "--distpath", str(args.distpath.resolve()),
        "--workpath", str(output / "work"),
        "--specpath", str(output),
        "--paths", str(root), "--paths", str(root / "lib"),
        "--add-data", f"{root / 'desktop' / 'assets'}:desktop/assets",
        "--collect-data", "webview",
        "--copy-metadata", "pywebview",
        "--hidden-import", "desktop.native",
    ]
    if sys.platform == "win32":
        options += [
            "--hidden-import", "webview.platforms.winforms",
            "--hidden-import", "webview.platforms.edgechromium",
            "--exclude-module", "webview.platforms.mshtml",
            "--exclude-module", "webview.platforms.cef",
        ]
    else:
        options += [
            "--hidden-import", "webview.platforms.cocoa",
            "--osx-bundle-identifier", "io.meetingtranscriber.reader",
            "--target-arch", "arm64",
        ]
    run([*options, str(root / "desktop" / "main.py")])


if __name__ == "__main__":
    main()
