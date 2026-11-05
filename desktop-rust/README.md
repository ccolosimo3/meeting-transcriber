# Meeting Transcriber Reader

This development build opens saved meetings on Apple Silicon macOS. Choose the
library folder in the app, select a meeting, and use **Find**, **Details** and
the passage timestamps to read and listen. A completed audio-only meeting can
play in the same reader. For older transcript formats, open the same library in
the Python desktop app. The Rust reader keeps the library files read-only.

## Build on macOS

Use Xcode or its Command Line Tools and Rust via rustup. The toolchain file
selects Rust 1.99.1. From this directory, install the pinned Tauri CLI into a
task-local location on PATH and build the app.

```sh
cargo install tauri-cli --version 2.12.4 --locked --root .local-artifacts/tauri-cli
export PATH="$PWD/.local-artifacts/tauri-cli/bin:$PATH"
cargo tauri build --bundles app -- --locked
```

The bundle is under `src-tauri/target/release/bundle/macos/` unless
`CARGO_TARGET_DIR` overrides it. Run focused checks with
`cargo test --release --locked --manifest-path src-tauri/Cargo.toml`.

The shared HTML, CSS and JavaScript are embedded from `desktop/assets`.
The Rust host exposes only library selection, refresh and run loading to that
window. Audio streams over a session-scoped loopback URL, and the library must
be chosen again after reopening the app.

Windows packaging is pending the private WebView2 profile implementation and
native qualification. The existing CLI and Python desktop are the entrypoints
for recording, import, transcription and summaries during development.
